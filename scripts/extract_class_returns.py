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
# 아직 수익률이 없는(전액 "-") 클래스 행도 있다 (예: 설정된 지 얼마 안 된 클래스).
# 값이 없다는 사실 자체와 class_code/설정일은 여전히 의미가 있어 버리지 않는다.
DASH_RE = re.compile(r"^-+$")
CLASS_CODE_RE = re.compile(r"\(([A-Za-z0-9\-]{1,8})\)")
# 일부 문서는 클래스명을 "(A2)"처럼 괄호로 안 감싸고 "ClassA2"처럼 그대로 붙여 쓴다
# (예: KR5120420039). 괄호 형식이 안 잡히면 이 패턴으로 한 번 더 시도한다.
CLASS_CODE_NOPAREN_RE = re.compile(r"Class[- ]?([A-Za-z0-9\-]{1,6})", re.IGNORECASE)
# 괄호도 "Class"도 없이 그냥 "종류A", "종류C4"처럼 쓰는 문서도 있다(제3부
# "3.집합투자기구의 운용실적" 섹션에서 확인 - KR510902511M 46페이지). 이
# 라벨은 데이터 줄 "위"에 오는 경우가 많아(3줄 구조: 종류코드 / 데이터 /
# 상세설명) 예외적으로 이전 줄까지 같이 본다 - "종류"라는 키워드로 앵커링돼
# 있어서 일반 괄호 패턴과 달리 다른 행의 것을 잘못 가져올 위험이 낮다.
CLASS_CODE_JONGRYU_RE = re.compile(r"종류\s*([A-Za-z0-9\-]{1,6})")
# "가.연평균수익률"(누적 1/2/3/5년+설정후, 우리 스키마와 동일)과 "나.연도별
# 수익률 추이"(1~5년차별 단년도 수익률, 컬럼 의미가 다름)는 둘 다 숫자
# 5개짜리 줄이라 구분 안 하면 "나" 표 값을 "가" 표 컬럼에 잘못 매핑하게
# 된다. 섹션 제목으로 구간을 나눠 "나" 섹션은 아예 스킵한다.
# (공백 다 지운 텍스트에 대해 매칭하므로 \s* 불필요)
SECTION_GA_RE = re.compile(r"가[\.．]연평균수익률")
SECTION_NA_RE = re.compile(r"나[\.．]연도별수익률")
# 클래스 행의 설정일("2016-04-18", "2001.01.31" 등) - 표 데이터가 아니라 각 행에
# 딸린 값이라 구조화 필드로 남겨둘 만하다.
INCEPTION_DATE_RE = re.compile(r"\d{4}[.\-]\d{1,2}[.\-]\d{1,2}")
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


# 표 옆 여백에 "투자실적\n추이\n(연평균\n수익률)"이 세로로 회전돼 한 단어씩
# 별도 줄로 찍히는 문서가 있다(KR5116501001 등). 이게 라벨 이어지는 줄
# 사이에 끼어들면 "바로 다음/이전 줄"만 보는 class_code 탐색이 진짜
# 라벨을 건너뛰고 이 캡션 조각을 잘못 집는다 - 데이터(숫자)가 전혀 없고
# 이 캡션 단어 목록에만 정확히 일치하는 줄은 건너뛰고 그 다음/이전 "진짜"
# 줄을 찾는다.
CAPTION_FRAGMENT_RE = re.compile(r"^\(?(투자실적|추이|연평균|연환산|수익률\)?)\)?$")


def _skip_caption_lines(lines, start, step):
    idx = start
    while 0 <= idx < len(lines):
        text = re.sub(r"\s+", "", " ".join(w["text"] for w in lines[idx]))
        if CAPTION_FRAGMENT_RE.match(text):
            idx += step
            continue
        return idx
    return None


def _line_text_skipping_captions(lines, idx, step):
    real_idx = _skip_caption_lines(lines, idx, step)
    if real_idx is None:
        return ""
    return " ".join(w["text"] for w in lines[real_idx])


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
    # "투자신탁"만 라벨로 있는 행은 특정 클래스가 아니라 펀드 전체 평균(모든
    # 클래스를 합친 수익률)이다. class_code가 없는 게 아니라 애초에 클래스가
    # 아니므로 별도 종류로 구분한다.
    if normalized == "투자신탁":
        return "fund_aggregate"
    return "class_return"


def find_return_rows_on_page(page, page_num, section="가"):
    """section: 이 페이지 시작 시점의 "가/나" 섹션 상태(문서 내 이전 페이지에서
    이어받음). "나.연도별 수익률 추이" 섹션에 들어간 뒤로는 다음 "가" 제목을
    다시 만나기 전까지 데이터 행을 전부 스킵한다 - 컬럼 의미가 다른 표라
    "가" 표 스키마(1y/2y/3y/5y/since_inception)에 잘못 매핑하면 안 되기 때문.
    반환값은 (rows, 이 페이지가 끝난 시점의 section)."""
    # x_tolerance=2(기본)로는 일부 문서에서 폰트 문제로 글자가 한 자씩 떨어져
    # 나오는 케이스(예: "4 .2 1")가 있어 숫자 인식이 아예 안 된다. 5로 올리면
    # 그 문제가 해결되면서도(검증 완료) 다른 문서의 값이 잘못 합쳐지진 않았다.
    words = page.extract_words(x_tolerance=5, keep_blank_chars=False)
    lines = cluster_lines(words)
    rows = []
    for i, line in enumerate(lines):
        line_text_for_section = re.sub(r"\s+", "", " ".join(w["text"] for w in line))
        if SECTION_NA_RE.search(line_text_for_section):
            section = "나"
        elif SECTION_GA_RE.search(line_text_for_section):
            section = "가"

        if section == "나":
            continue

        decimals = [w for w in line if DECIMAL_RE.match(w["text"])]
        dashes = [w for w in line if DASH_RE.match(w["text"])]
        value_tokens = sorted(decimals + dashes, key=lambda w: w["x0"])
        if len(value_tokens) < 3 or len(value_tokens) > 5:
            continue
        # 운용전문인력 표(성명/생년/직위 등)와 구분: 그 표는 억원 단위 정수(운용규모)나
        # 4자리 연도(생년) 같은 게 섞여 있고, 클래스 수익률 표는 전부 소수 % 값이다.
        # 값들이 전부 "%" 스타일(대체로 두 자리 이하 정수부)인지로 대충 거른다.
        if any(abs(float(d["text"])) > 100 for d in decimals):
            continue

        pre_text_words = [w for w in line if w["x0"] < value_tokens[0]["x0"]]
        pre_text = " ".join(w["text"] for w in pre_text_words)

        # 클래스명이 인접 줄로 이어질 수 있어 다음 줄까지 확인 (총보수 표에서
        # 검증된 대로 - "이전 줄"은 다른 행 것일 위험이 있어 보지 않는다.
        # 단, "종류A" 패턴은 라벨이 데이터 줄 "위"에 오는 3줄 구조라 예외적으로
        # 이전 줄도 함께 본다 - 아래 종류 코드 탐색 참고).
        # 세로로 회전된 옆면 캡션("투자실적"/"추이" 등)이 진짜 라벨 줄
        # 사이에 끼어들 수 있어(KR5116501001), "바로 다음/이전 줄"이 아니라
        # 그 캡션 조각들을 건너뛴 "진짜" 다음/이전 줄을 본다.
        prev_line_text = _line_text_skipping_captions(lines, i - 1, -1)
        next_line_text = _line_text_skipping_captions(lines, i + 1, 1)
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
            if w["x0"] > value_tokens[-1]["x0"] and re.match(r"^\d{1,4}$", w["text"])
        ]
        if trailing_int_like:
            continue
        # "-"를 값으로 인정하면서 새로 생긴 위험: 총보수 표의 비용예시(천원, 정수)
        # 행이 "- - 240 244 40 40 200 204 -"처럼 대시 사이사이에 정수가 끼어 있는
        # 경우, 대시만 3~5개 세면 수익률 행으로 오인한다. 값 영역(첫~마지막
        # value_token 사이) 안에 순수 정수 토큰이 하나라도 끼어 있으면 총보수
        # 표로 보고 제외한다.
        value_x0, value_x1 = value_tokens[0]["x0"], value_tokens[-1]["x0"]
        stray_ints = [
            w for w in line
            if value_x0 <= w["x0"] <= value_x1
            and w not in value_tokens
            and re.match(r"^\d{1,4}$", w["text"])
        ]
        if stray_ints:
            continue

        # 운용전문인력(운용역) 표 행도 같은 블록에 섞여 있을 수 있다 - "생년(19xx)"이나
        # "운용규모(1,234억원 - 콤마 있는 큰 수)"가 라벨 자리에 있으면 그 표로 본다.
        # 단, 수익률 표의 설정일("2001.01.31")도 4자리 숫자로 시작하니 뒤에 "."이나
        # "-"가 붙어 날짜로 보이면(생년은 그냥 단독 숫자) 제외 대상에서 뺀다.
        # (문자열을 공백 제거 후 정규식 \b로 검사하면 "전준필1996"처럼 한글 뒤에 바로
        # 붙은 숫자에서 단어 경계가 인식되지 않아(둘 다 유니코드 \w) 놓칠 수 있어,
        # 토큰 단위로 직접 검사한다.)
        if any(re.fullmatch(r"(19|20)\d{2}", w["text"]) for w in pre_text_words):
            continue
        if any(re.fullmatch(r"\d{1,3},\d{3}", w["text"]) for w in pre_text_words):
            continue

        # 이 줄 자체가 비교지수/변동성 행인지 먼저 직접 확인한다(같은 줄
        # 텍스트만 본다 - 옆 줄을 보지 않으므로 안전). 비교지수/변동성 행은
        # 애초에 클래스 코드가 없으므로, 여기 해당되면 class_code 검색 자체를
        # 하지 않는다 - 안 그러면 "다음 줄"(보통 바로 다음에 오는 클래스 행의
        # 이름)을 이 행 자신의 클래스 코드로 잘못 가져온다(실측: 변동성 행이
        # 다음 클래스 행 이름을 빌려와 class_code="A"로 잘못 붙는 버그 확인).
        same_line_kind = row_kind(pre_text)

        if same_line_kind in ("benchmark", "volatility"):
            kind = same_line_kind
            class_code = None
        else:
            class_code = None
            m = CLASS_CODE_RE.search(norm_label)
            if m:
                class_code = m.group(1)
            else:
                # 공백을 지우면 "ClassA 2006-09-05"가 "ClassA2006-09-05"로
                # 붙어버려서 뒤에 오는 날짜까지 클래스 코드로 삼켜버린다
                # ("A2006-"). 이 패턴은 원본(공백 유지) 텍스트에서 찾아야
                # 단어 경계("ClassA" 다음 공백)에서 멈춘다.
                m2 = CLASS_CODE_NOPAREN_RE.search(label_search_text)
                if m2:
                    class_code = m2.group(1)
                else:
                    # "종류A"는 라벨이 데이터 줄 "위"에 오는 3줄 구조(종류코드
                    # 줄 / 데이터 줄 / 상세설명 줄)라 이전 줄도 함께 본다 -
                    # "종류"라는 명시적 키워드로 앵커링돼 있어 일반 괄호
                    # 패턴과 달리 다른 행 것을 잘못 가져올 위험이 낮다.
                    m3 = CLASS_CODE_JONGRYU_RE.search(
                        prev_line_text + " " + label_search_text
                    )
                    if m3:
                        class_code = m3.group(1)

            if class_code:
                # 클래스 코드가 확실히 잡혔으면 그 자체로 "클래스 행"이라는
                # 확실한 증거라 주변 줄의 변동성 언급을 볼 필요가 없다
                # (KR5120420039처럼 클래스 여러 개가 변동성/비교지수 한 쌍을
                # 공유하는 표에서, 바로 옆에 다른 그룹의 변동성 행이 우연히
                # 붙어 있는 걸 이 행 자신의 유형으로 착각하는 버그가 있었다).
                kind = "class_return"
            else:
                # "수익률\n변동성"라벨이 데이터 줄 위아래로 걸쳐 있는 경우
                # (KR5113420012/69에서 확인)만 좁혀서 앞뒤 줄을 본다. 이때는
                # 캡션을 건너뛴 줄이 아니라 "바로" 앞/다음 줄이어야 한다 -
                # 캡션 건너뛰기로 더 멀리 있는 줄(예: 바로 위의 비교지수 행
                # 전체)까지 가져오면 그 행 자신의 "비교지수" 텍스트가 섞여
                # 들어와 "변동성 행인데 비교지수도 같이 검출됨" 오판정이
                # 생긴다(KR5113420012에서 실제 확인된 회귀).
                raw_prev_line_text = " ".join(w["text"] for w in lines[i - 1]) if i - 1 >= 0 else ""
                raw_next_line_text = " ".join(w["text"] for w in lines[i + 1]) if i + 1 < len(lines) else ""
                kind = row_kind(pre_text, raw_prev_line_text, raw_next_line_text)

        date_m = INCEPTION_DATE_RE.search(pre_text)
        inception_date = date_m.group() if date_m else None

        # 아직 수익률이 없는 신규 클래스 등은 원본이 그 칸에 "-"를 직접
        # 찍어서 "값이 없다"는 걸 명시적으로 밝힌다. 이걸 그냥 None으로
        # 뭉개면 "추출을 못 해서 모른다"와 "원본이 확인해서 없다고
        # 밝혔다"가 구분이 안 된다(class_fees의 peer_avg_fee/
        # sales_commission_desc에서 이미 사용자 지적으로 고친 것과 같은
        # 문제 - 실측: KR510902511M C1 등 56건에서 evidence는 전부
        # "-"인데 값은 None으로 나오고 있었다). 원본 토큰이 "-"면 "-"를
        # 그대로 남기고, "-"도 아니고 소수도 아닌(추출 자체가 안 된)
        # 경우에만 None으로 남긴다.
        values = {
            PERIOD_LABELS[idx]: (
                t["text"] if DECIMAL_RE.match(t["text"])
                else ("-" if t["text"] == "-" else None)
            )
            for idx, t in enumerate(value_tokens)
        }

        rows.append({
            "row_kind": kind,
            "class_code": class_code,
            "inception_date": inception_date,
            "values": values,
            "page": page_num,
            "evidence": " ".join(w["text"] for w in line),
            "method": "coordinate_reconstruction",
            "confidence": 1.0 if (class_code or kind != "class_return") else 0.5,
            "_top": line[0]["top"],
        })

    _apply_merged_cell_dates(page, words, rows)
    for r in rows:
        del r["_top"]
    return rows, section


def _apply_merged_cell_dates(page, words, rows):
    """'최초설정일' 칸이 여러 행에 걸쳐 병합된 경우, 날짜 텍스트는 병합된 셀
    안 어딘가(보통 시각적 중앙에 가까운 한 행) 한 번만 찍히고 나머지 행은
    비어 보인다. 인접한 줄 순서만 보고 전파하면 실제로는 안 겹치는 행에
    잘못된 날짜가 번질 위험이 있어(예: 병합 안 된 바로 다음 클래스가 우연히
    날짜가 없는 경우), 대신 PDF에 실제로 그려진 셀 테두리(page.rects)를 찾아
    그 테두리 안에 들어오는 행들에만 정확히 전파한다."""
    if not rows:
        return
    tops = [r["_top"] for r in rows]
    y_lo, y_hi = min(tops) - 15, max(tops) + 15

    # 이 표 범위 안에서 실제로 잡힌 설정일 텍스트의 x좌표로 '최초설정일' 칸의
    # 위치를 추정한다 (표마다 칸 위치가 조금씩 다를 수 있어 페이지별로 다시 잡음).
    date_x0 = None
    for w in words:
        if y_lo <= w["top"] <= y_hi and INCEPTION_DATE_RE.fullmatch(w["text"]):
            date_x0 = w["x0"]
            break
    if date_x0 is None:
        return

    col_cells = [
        rc for rc in page.rects
        if abs(rc["x0"] - date_x0) < 15
        and (rc["bottom"] - rc["top"]) > 8
        and rc["top"] >= y_lo - 5 and rc["bottom"] <= y_hi + 5
    ]
    for rc in col_cells:
        cell_words = [
            w for w in words
            if rc["top"] - 1 <= w["top"] <= rc["bottom"] + 1
            and rc["x0"] - 2 <= w["x0"] <= rc["x1"] + 2
        ]
        cell_date = next((w["text"] for w in cell_words if INCEPTION_DATE_RE.fullmatch(w["text"])), None)
        if not cell_date:
            continue
        for r in rows:
            if rc["top"] - 1 <= r["_top"] <= rc["bottom"] + 1:
                r["inception_date"] = cell_date


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
        section = "가"
        for page_num in pages:
            if page_num < 1 or page_num > len(pdf.pages):
                continue
            page = pdf.pages[page_num - 1]
            rows, section = find_return_rows_on_page(page, page_num, section=section)
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
