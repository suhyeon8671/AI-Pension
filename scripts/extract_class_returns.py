"""
연금 Agent 과제 - 클래스별 수익률(투자실적추이) 좌표 기반 추출

products의 "투자실적추이(연평균수익률)" 표는 클래스마다 3행 구조를 쓴다:
    {클래스명}   {최초설정일}  {최근1년} {최근2년} {최근3년} {최근5년} {설정일이후}
    비교지수      -            {최근1년} {최근2년} {최근3년} {최근5년} {설정일이후}
    수익률\n변동성 {최초설정일}  {최근1년} {최근2년} {최근3년} {최근5년} {설정일이후}

총보수 표(extract_class_fees.py)와 마찬가지로 pdfplumber extract_tables()가
이 표를 셀 뭉침으로 깨뜨리는 경우가 많고(비교지수/수익률변동성 키워드 +
날짜 2개 이상 + 소수 6개 이상이 한 셀에 뭉쳐 있으면 깨진 것으로 판별),
같은 페이지 안에 정상 추출된 버전이 같이 있는 경우도 있다. 좌표 기반
재구성이 정상 페이지에도 동일하게 정확히 동작한다는 걸 총보수 표에서
검증했으므로, 여기서도 "최근"+"설정일" 언급된 페이지는 깨졌든 아니든
전부 좌표로 재구성한다.

주의: 같은 페이지에 "운용전문인력"(운용역/운용사 실적) 표가 비슷한 헤더
문구("최근1년/최근2년")를 쓰는 경우가 있어 혼동하기 쉽다 - 이 표는 클래스
행이 아니라 사람 이름 행이라 무시해야 한다. 데이터 행 판별 시 값 개수
(3~5개, 그것도 클래스 표는 %라 보통 두 자리 소수)로 최대한 걸러내되,
100% 걸러진다는 보장은 없어 evidence를 반드시 같이 남긴다.

사용법:
    python scripts/extract_class_returns.py
"""

import argparse
import glob
import json
import os
import re

import pdfplumber

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data", "products")
EXTRACTED_DIR = os.path.join(REPO_ROOT, "extracted", "products")
DEFAULT_OUTPUT = os.path.join(REPO_ROOT, "class_returns.json")

NUM_RE = re.compile(r"^-?\d[\d,]*\.?\d*$")
DECIMAL_RE = re.compile(r"^-?\d+\.\d+$")
CLASS_CODE_RE = re.compile(r"\(([A-Za-z0-9\-]{1,8})\)")
PERIOD_LABELS = ["1y", "2y", "3y", "5y", "since_inception"]


def cluster_lines(words, tol=2.5):
    words = sorted(words, key=lambda w: w["top"])
    lines = []
    for w in words:
        if lines and abs(w["top"] - lines[-1][0]["top"]) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    for line in lines:
        line.sort(key=lambda w: w["x0"])
    return lines


def row_kind(pre_text, prev_line_text="", next_line_text=""):
    # 폰트 문제로 글자가 한 자씩 떨어져 나오는 문서에서는 "비교지수"가
    # "비 교 지 수"처럼 공백 낀 상태로 들어오기도 해서, 공백을 지우고 비교한다.
    normalized = re.sub(r"\s+", "", pre_text)
    if "비교지수" in normalized:
        return "benchmark"
    if "변동성" in normalized:
        return "volatility"
    # "수익률\n변동성"라벨이 데이터 줄 위아래로 걸쳐 있는 경우가 있다
    # ("수익률"이 이전 줄, "변동성"이 다음 줄, 데이터 줄 자체엔 라벨이 거의
    # 없음). class_code 검색과 달리 "비교지수"/"변동성"은 클래스 이름과
    # 겹칠 일이 없는 행 유형 라벨이라, 이전/다음 줄을 같이 봐도 다른 클래스
    # 정보를 잘못 가져올 위험이 낮다.
    around = re.sub(r"\s+", "", prev_line_text) + re.sub(r"\s+", "", next_line_text)
    if "변동성" in around and "비교지수" not in around:
        return "volatility"
    return "class_return"


def find_return_rows_on_page(page, page_num):
    # x_tolerance=2(기본)로는 일부 문서에서 폰트 문제로 글자가 한 자씩 떨어져
    # 나오는 케이스(예: "4 .2 1")가 있어 숫자 인식이 아예 안 된다. 5로 올리면
    # 그 문제가 해결되면서도(검증 완료) 다른 문서의 값이 잘못 합쳐지진 않았다.
    words = page.extract_words(x_tolerance=5, keep_blank_chars=False)
    lines = cluster_lines(words)
    rows = []
    for i, line in enumerate(lines):
        decimals = [w for w in line if DECIMAL_RE.match(w["text"])]
        if len(decimals) < 3 or len(decimals) > 5:
            continue
        # 운용전문인력 표(성명/생년/직위 등)와 구분: 그 표는 억원 단위 정수(운용규모)나
        # 4자리 연도(생년) 같은 게 섞여 있고, 클래스 수익률 표는 전부 소수 % 값이다.
        # 값들이 전부 "%" 스타일(대체로 두 자리 이하 정수부)인지로 대충 거른다.
        if any(abs(float(d["text"])) > 100 for d in decimals):
            continue

        pre_text_words = [w for w in line if w["x0"] < decimals[0]["x0"]]
        pre_text = " ".join(w["text"] for w in pre_text_words)

        # 클래스명이 인접 줄로 이어질 수 있어 다음 줄까지 확인 (총보수 표에서
        # 검증된 대로 - "이전 줄"은 다른 행 것일 위험이 있어 보지 않는다)
        next_line_text = " ".join(w["text"] for w in lines[i + 1]) if i + 1 < len(lines) else ""
        label_search_text = pre_text + " " + next_line_text
        # 폰트 문제로 글자가 한 자씩 떨어져 나오는 문서(예: "비 교 지 수")에서도
        # 키워드 검사가 되도록, 공백 제거한 버전을 만들어서 모든 문구 검사에 쓴다.
        norm_pre = re.sub(r"\s+", "", pre_text)
        norm_label = re.sub(r"\s+", "", label_search_text)

        # 총보수 표 행이 같은 페이지(같은 뭉친 블록)에 섞여 있다가 잘못 걸리는
        # 걸 제외한다. 두 가지 신호로 구분: (1) 판매수수료 문구("납입금액의"/
        # "없음")를 라벨로 쓰는 건 총보수 표뿐이고, (2) 총보수 표는 소수(%) 뒤에
        # 정수(비용예시, 천원 단위)가 같은 줄에 더 붙어 있는데 수익률 표는
        # 소수(%)만 있고 정수가 안 붙는다.
        if "납입금액의" in norm_label or "없음" in norm_pre:
            continue
        trailing_int_like = [
            w for w in line
            if w["x0"] > decimals[-1]["x0"] and re.match(r"^\d{1,4}$", w["text"])
        ]
        if trailing_int_like:
            continue

        # 운용전문인력(운용역) 표 행도 같은 블록에 섞여 있을 수 있다 - "생년(19xx)"이나
        # "운용규모(1,234억원 - 콤마 있는 큰 수)"가 라벨 자리에 있으면 그 표로 본다.
        # 단, 수익률 표의 설정일("2001.01.31")도 4자리 숫자로 시작하니 뒤에 "."이나
        # "-"가 붙어 날짜로 보이면(생년은 그냥 단독 숫자) 제외 대상에서 뺀다.
        if re.search(r"\b(19|20)\d{2}\b(?![.\-])", norm_pre) or re.search(r"\d{1,3},\d{3}", norm_pre):
            continue

        # row_kind는 "다음 줄"까지 보면 안 된다 - 다음 줄은 보통 다음 행(비교지수/
        # 수익률변동성 등) 자신의 라벨이라, 그걸 지금 행 것으로 잘못 가져오게 된다.
        # (검증 중 실제로 이 버그로 3행이 전부 "benchmark"로 잘못 나온 걸 확인함)
        prev_line_text = " ".join(w["text"] for w in lines[i - 1]) if i - 1 >= 0 else ""
        kind = row_kind(pre_text, prev_line_text, next_line_text)
        class_code = None
        m = CLASS_CODE_RE.search(norm_label)
        if m:
            class_code = m.group(1)

        values = {PERIOD_LABELS[idx]: d["text"] for idx, d in enumerate(decimals)}

        rows.append({
            "row_kind": kind,
            "class_code": class_code,
            "values": values,
            "page": page_num,
            "evidence": " ".join(w["text"] for w in line),
            "method": "coordinate_reconstruction",
            "confidence": 1.0 if (class_code or kind != "class_return") else 0.5,
        })
    return rows


def candidate_pages_for_doc(doc_id, max_page):
    """"최근"+"설정일"만으로 범위를 잡으면 문서 뒤쪽 상세 부속서류(제2부 등)의
    모펀드 관련 반복 섹션까지 걸려서 범위가 너무 넓어진다 (KR5113420012에서
    확인: 51/52페이지에 모펀드용 표가 또 있음). "투자실적추이"는 요약정보
    섹션에만 붙는 라벨이라 훨씬 정확한 스코핑 기준이다."""
    fp = os.path.join(EXTRACTED_DIR, f"{doc_id}_tables.json")
    if not os.path.exists(fp):
        return []
    with open(fp, "r", encoding="utf-8") as f:
        tables = json.load(f)

    pages = set()
    for t in tables:
        flat = "".join(c for row in t["data"] for c in row if c)
        if "투자실적" in flat:
            pages.add(t["page"])
            if t["page"] + 1 <= max_page:
                pages.add(t["page"] + 1)
    return sorted(pages)


def process_doc(doc_id):
    pdf_candidates = glob.glob(os.path.join(DATA_DIR, doc_id, "*.pdf"))
    if not pdf_candidates:
        return []

    results = []
    with pdfplumber.open(pdf_candidates[0]) as pdf:
        pages = candidate_pages_for_doc(doc_id, len(pdf.pages))
        if not pages:
            return []
        for page_num in pages:
            if page_num < 1 or page_num > len(pdf.pages):
                continue
            page = pdf.pages[page_num - 1]
            rows = find_return_rows_on_page(page, page_num)
            for r in rows:
                r["product_code"] = doc_id
                results.append(r)

    # 페이지 후보를 넓게 잡다 보니(다음 페이지도 포함) 같은 행이 중복될 수 있다.
    # (row_kind, class_code, page, values의 1y값) 조합으로 단순 중복 제거.
    seen = set()
    deduped = []
    for r in results:
        key = (r["row_kind"], r["class_code"], r["values"].get("1y"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped


def main():
    parser = argparse.ArgumentParser(description="클래스별 수익률 좌표 기반 추출")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    doc_ids = sorted(
        os.path.basename(p).replace("_tables.json", "")
        for p in glob.glob(os.path.join(EXTRACTED_DIR, "*_tables.json"))
    )

    all_rows = []
    docs_with_hits = 0
    for doc_id in doc_ids:
        rows = process_doc(doc_id)
        if rows:
            docs_with_hits += 1
        all_rows.extend(rows)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(all_rows, f, ensure_ascii=False, indent=2)

    class_rows = [r for r in all_rows if r["row_kind"] == "class_return"]
    labeled = sum(1 for r in class_rows if r["class_code"])
    print(f"{len(all_rows)}개 행 ({docs_with_hits}개 문서) → {args.output}")
    print(f"  class_return 행: {len(class_rows)}건, 클래스코드 인식: {labeled}건")
    print(f"  benchmark(비교지수)/volatility(변동성) 행: {len(all_rows) - len(class_rows)}건")


if __name__ == "__main__":
    main()
