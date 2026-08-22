"""
연금 Agent 과제 - 클래스별 총보수 추출 (좌표 기반 재구성)

products 표 중 "클래스 종류 + 총보수" 수수료표가 pdfplumber extract_tables()로
뽑을 때 셀이 뭉쳐지는(줄바꿈만으로 구분된 텍스트 블록이 되는) 경우가 많다는
걸 확인했다 (194개 표, 83개 문서 - README 참고). extract_tables()의
table_settings(strategy, tolerance)만 조정해서는 열 분리가 안 됐고, 원인은
pdfplumber의 표 셀 경계 인식 실패였다.

그래서 이 스크립트는 표 재인식을 시도하는 대신, 페이지의 각 단어의 실제 좌표
(page.extract_words())를 직접 읽어서:
  1. top(y좌표)이 비슷한 단어들을 "한 줄"로 묶고
  2. 소수 3~4개(총보수/판매보수/동종유형총보수/총보수·비용) + 정수 4개 이상
     (1/2/3/5/10년 비용예시)이 있는 줄을 "데이터 행"으로 판별하고
  3. 그 줄에서 x좌표가 가장 왼쪽인 소수를 총보수로, 그 앞의 텍스트에서
     클래스 코드(괄호 안 알파벳/숫자, 예: A2, C1, Ae, C-E)를 찾는다

KR5120420039(정상 추출된 표)로 방법을 검증(A2=0.3195 등 4개 클래스 전부 일치)
했고, KR5111420047(깨진 표)에도 적용해 원본 이미지와 6개 클래스 전부 일치함을
육안으로 확인했다.

범위: 이번 1차는 "총보수 표"만 대상으로 한다 (수익률/AUM 표는 컬럼 구조가
달라서 별도 스크립트가 필요 - 다음 단계).

사용법:
    python scripts/extract_class_fees.py
    python scripts/extract_class_fees.py --output class_fees.json
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
DEFAULT_OUTPUT = os.path.join(REPO_ROOT, "class_fees.json")

NUM_RE = re.compile(r"^\d[\d,]*\.?\d*$")
# 일부 운용사 서식(신영자산운용 등)은 총보수 % 값을 "1.18%"처럼 %가 붙은 한
# 토큰으로 낸다(공백 없이 붙어 있어 pdfplumber가 한 단어로 묶음). %가 없는
# 문서와 똑같이 처리하기 위해 optional %를 허용하고, 저장할 때는 벗겨낸다.
DECIMAL_RE = re.compile(r"^\d+\.\d+%?$")
DECIMAL_FINDALL_RE = re.compile(r"\d+\.\d+")  # 앵커 없이 텍스트 뭉치 안에서 찾을 때
CLASS_CODE_RE = re.compile(r"\(([A-Za-z0-9\-]{1,8})\)")
# 판매수수료 칸은 숫자가 아니라 정형화된 문구("없음" 또는 "납입금액의 N%[ ]이내")인데,
# "납입금액의"와 "N%이내"가 셀 줄바꿈 때문에 서로 다른 줄(그 사이에 다른 칸 텍스트가
# 끼어든 상태)로 떨어져 있는 경우가 많아 하나의 정규식으로는 못 잡는다. "이내"까지
#3줄로 쪼개지는 경우도 있어("납입금" / "액의 N%" / "이내") 퍼센트 숫자만 여기서
# 찾고, "이내"가 바로 붙어 있을 필요는 없다고 본다 (이 좁은 윈도우 안의 "%"는
# 사실상 판매수수료율 말고는 나올 데가 없다).
SALES_COMMISSION_PCT_RE = re.compile(r"([\d.]+)\s*%")


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


def find_fee_rows_on_page(page, page_num):
    words = page.extract_words(x_tolerance=2, keep_blank_chars=False)
    lines = cluster_lines(words)
    rows = []
    for i, line in enumerate(lines):
        # decimals를 NUM_RE로 거른 뒤 다시 추리면 "1.18%"처럼 %가 붙은 토큰이
        # NUM_RE(퍼센트 미허용)에 애초에 안 걸려 통째로 빠진다 - line에서 직접
        # 따로 찾는다.
        decimals = [w for w in line if DECIMAL_RE.match(w["text"])]
        int_like = [w for w in line if NUM_RE.match(w["text"]) and w not in decimals]
        if len(decimals) < 3 or len(int_like) < 4:
            continue

        # 열 순서: [클래스종류] [판매수수료] 총보수 판매보수 동종유형총보수 총보수·비용
        #          1년 2년 3년 5년 10년  (동종유형총보수는 '-'로 빠질 수 있어 소수 3개까지 허용)
        has_peer_avg = len(decimals) >= 4
        total_fee, distribution_fee = decimals[0], decimals[1]
        peer_avg_fee = decimals[2] if has_peer_avg else None
        total_fee_and_cost = decimals[3] if has_peer_avg else decimals[2]
        cost_years = ["1y", "2y", "3y", "5y", "10y"]
        cost_projection = {
            y: int_like[idx]["text"] for idx, y in enumerate(cost_years) if idx < len(int_like)
        }

        pre_text_words = [w for w in line if w["x0"] < total_fee["x0"]]
        class_part1 = " ".join(w["text"] for w in pre_text_words)

        # 클래스 코드와 판매수수료 문구는 이 줄 또는 인접한 줄(줄바꿈으로 나뉜 셀)에
        # 걸쳐 있을 수 있어서, 이 줄 기준 앞뒤 한 줄까지 창을 넓혀서 찾는다.
        window_lines = [lines[j] for j in (i - 1, i, i + 1) if 0 <= j < len(lines)]
        window_text = " ".join(" ".join(w["text"] for w in wl) for wl in window_lines)

        # 판매수수료 문구가 "납입금"/"액의 N%"/"이내" 3줄로 나뉘어 데이터 줄
        # 앞뒤로 2줄까지 걸치는 경우가 있었다(KR510902511M) - 그렇다고 무작정
        # ±2로 넓히면 바로 옆 클래스 행의 판매수수료를 잘못 가져오는 더 나쁜
        # 문제가 생긴다(실측: C 클래스가 A 클래스의 "0.10%이내"를 잘못 가져옴).
        # "틀린 값 < 없는 값"이므로 안전한 ±1만 쓰고, 이 케이스는 놓치는 쪽을
        # 택한다(sales_commission_desc는 null로 남음, total_fee 등 핵심 값은
        # 영향 없음).
        # %가 숫자에 바로 붙는 서식(위 DECIMAL_RE 참고)에서는 총보수/판매보수
        # 값 자체도 "0.145%"처럼 "%"를 달고 있어서, 판매수수료 % 탐색에 이
        # 값들이 같이 걸려 있으면(예: 진짜 판매수수료 "0.1%"보다 총보수
        # "0.145%"가 텍스트상 먼저 나옴) 엉뚱한 숫자를 판매수수료로 오인한다.
        # 이 행 자신의 총보수류 값 토큰은 검색 대상에서 제외한다.
        decimal_ids = {id(w) for w in decimals}
        wide_text = " ".join(
            w["text"] for wl in window_lines for w in wl if id(w) not in decimal_ids
        )
        # "-"는 판매수수료가 "없음"이라는 뜻으로 쓰이기도 하는데, 클래스명 자체에
        # 하이픈("수수료선취-오프라인")이 들어있어 그냥 문자열에 "-"가 있는지만
        # 보면 항상 참이 된다. 그래서 "-"가 단독 토큰(그 칸에 딱 "-"만 있는 경우)
        # 으로 존재하는지를 봐야 한다.
        has_standalone_dash = any(
            w["text"] == "-" for wl in window_lines for w in wl
        )

        # 클래스 코드(괄호 안 텍스트)는 실측 사례들에서 항상 "이 줄" 또는 "다음 줄"에서만
        # 나타났다 - "이전 줄"은 위쪽 행(다른 클래스)의 이름 꼬리일 수 있어서 잘못
        # 가져다 쓸 위험이 있다 (KR514X450008에서 확인: 이전 줄의 클래스코드를 엉뚱하게
        # 가져와서 실제로는 다른 클래스인 행에 잘못 붙인 사례). 그래서 클래스 코드는
        # "이 줄 + 다음 줄"까지만 보고, 이전 줄은 보지 않는다.
        next_line_text = " ".join(w["text"] for w in lines[i + 1]) if i + 1 < len(lines) else ""
        class_code_search_text = class_part1 + " " + next_line_text

        class_code = None
        m = CLASS_CODE_RE.search(class_code_search_text)
        if m:
            class_code = m.group(1)

        # "납입금액의"가 3줄로 쪼개지는 경우도 있다("납입금" / 데이터 줄에 낀
        # "액의 1%" / "이내" - 사이에 클래스명 등 다른 텍스트가 끼어 있어서
        # "납입금액의"를 하나의 이어붙은 문자열로 찾으면 놓친다). "납입금"이라는
        # 조각만으로도 판매수수료 문구라는 걸 충분히 특정할 수 있어 그걸로 판별한다.
        sales_commission_desc = None
        pct_m = SALES_COMMISSION_PCT_RE.search(wide_text)
        if "납입금" in wide_text and pct_m:
            sales_commission_desc = f"납입금액의 {pct_m.group(1)}%이내"
        elif "없음" in window_text or has_standalone_dash:
            sales_commission_desc = "없음"

        rows.append({
            "class_code": class_code,
            "sales_commission_desc": sales_commission_desc,
            "total_fee": total_fee["text"].rstrip("%"),
            "distribution_fee": distribution_fee["text"].rstrip("%"),
            "peer_avg_fee": peer_avg_fee["text"].rstrip("%") if peer_avg_fee else None,
            "total_fee_and_cost": total_fee_and_cost["text"].rstrip("%"),
            "cost_projection_per_10m": cost_projection,
            "page": page_num,
            # 이 행 자신의 줄만 담는다 - total_fee 등 숫자 필드들의 근거로는
            # 이걸로 충분하다("납입금액의 1%이내"처럼 데이터 줄 앞뒤로 라벨이
            # 쪼개진 경우, 이 줄만 보면 "액의 1%"처럼 일부만 보이는 게
            # 정상이다 - 실제 판정에 쓴 범위는 sales_commission_evidence 참고).
            "evidence": " ".join(w["text"] for w in line),
            # sales_commission_desc는 이 행 자신의 줄이 아니라 앞뒤 1줄까지
            # 포함한 window_text로 판정한다 - 그 판정 근거를 그대로 남겨서
            # "액의 1%"만 보고 "납입금액의 1%이내"가 어디서 나왔는지 못
            # 알아보는 문제(사용자가 직접 대조하다 발견)를 없앤다.
            "sales_commission_evidence": window_text,
            "method": "coordinate_reconstruction",
            "confidence": 1.0 if class_code else 0.5,
        })
    return rows


def candidate_pages_for_doc(doc_id, max_page):
    """처음엔 "블롭"(뭉쳐서 깨진) 페이지만 대상으로 삼았는데, 그러면 표가 여러
    페이지에 걸쳐 있을 때(예: 클래스 일부는 정상 추출된 페이지에, 나머지는 깨진
    페이지에) 정상 페이지 쪽 클래스를 통째로 놓치는 버그가 있었다(KR514X450008
    사례로 확인). 좌표 기반 재구성은 이미 정상 추출된 페이지에도 똑같이 정확하게
    동작한다는 걸 검증했으므로(KR5120420039), "클래스"+"총보수"가 언급된 페이지는
    깨졌든 안 깨졌든 전부 대상으로 삼고, 표가 다음 페이지로 이어질 수 있으니
    바로 다음 페이지도 같이 포함한다."""
    fp = os.path.join(EXTRACTED_DIR, f"{doc_id}_tables.json")
    if not os.path.exists(fp):
        return []
    with open(fp, "r", encoding="utf-8") as f:
        tables = json.load(f)

    pages = set()
    for t in tables:
        flat = " ".join(c for row in t["data"] for c in row if c)
        if "클래스" in flat and "총보수" in flat:
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
            rows = find_fee_rows_on_page(page, page_num)
            for r in rows:
                r["product_code"] = doc_id
                results.append(r)

    # 표가 여러 페이지에 걸쳐 있어서 다음 페이지도 후보로 넣다 보니, 같은
    # 클래스가 두 페이지 모두에서 뽑힐 수 있다 (예: 클래스 헤더 페이지의
    # 마지막 줄이 다음 페이지 처음 줄과 겹쳐 인식되는 경우). class_code가
    # 있는 것끼리는 (product_code, class_code) 기준으로 중복 제거하고,
    # confidence가 더 높은(=class_code를 더 명확히 찾은) 쪽을 남긴다.
    dedup = {}
    unlabeled = []
    for r in results:
        if r["class_code"] is None:
            unlabeled.append(r)
            continue
        key = r["class_code"]
        if key not in dedup or r["confidence"] > dedup[key]["confidence"]:
            dedup[key] = r
    return list(dedup.values()) + unlabeled


def main():
    parser = argparse.ArgumentParser(description="클래스별 총보수 좌표 기반 추출 (1차: 총보수 표만)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    doc_ids = sorted(
        os.path.basename(p).replace("_tables.json", "")
        for p in glob.glob(os.path.join(EXTRACTED_DIR, "*_tables.json"))
    )

    all_rows = []
    docs_with_hits = 0
    docs_with_missing_class_code = 0
    for doc_id in doc_ids:
        rows = process_doc(doc_id)
        if rows:
            docs_with_hits += 1
            if any(r["confidence"] < 0.7 for r in rows):
                docs_with_missing_class_code += 1
        all_rows.extend(rows)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(all_rows, f, ensure_ascii=False, indent=2)

    print(f"{len(all_rows)}개 클래스 레코드 ({docs_with_hits}개 문서) → {args.output}")
    print(f"클래스 코드 인식 실패(confidence<0.7): {docs_with_missing_class_code}개 문서")


if __name__ == "__main__":
    main()
