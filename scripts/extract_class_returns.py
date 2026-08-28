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
from collections import defaultdict

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


def _normalize_date(s):
    """설정일 표기가 문서마다 "2013.08.19"와 "2009-04-20"으로 갈린다(실측:
    점 140건 / 하이픈 128건). 이건 원본이 담은 "정보"가 아니라 그냥 조판
    표기 차이라, 다른 값들("-" 같은 건 원본 뜻이 있어 그대로 두는 것과
    달리) 통일해도 잃는 게 없다. 반대로 섞인 채 두면 SQL에서 날짜 비교/
    정렬이 안 돼서("가장 먼저 설정된 클래스는?" 같은 질의) 실제로 답을
    못 하게 된다. ISO(YYYY-MM-DD)로 맞춘다 - 한 자리 월/일도 0을 채운다."""
    m = re.fullmatch(r"(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})", s)
    if not m:
        return s
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
PERIOD_LABELS = ["1y", "2y", "3y", "5y", "since_inception"]
# 최근3년/최근5년 칸이 원본에 아예 빈칸인(설정된 지 얼마 안 된 펀드) 행이
# 있다(KR5120420091 실측: "최근1년 최근2년 최근3년 최근5년 설정일이후" 5칸
# 헤더인데 실제 값은 "4.23 4.48 4.48" 3개뿐 - 3번째 값은 3년이 아니라
# 설정일이후 값인데, 그냥 순서대로 PERIOD_LABELS[0,1,2]에 채우면
# "3y"라는 잘못된 이름표가 붙는다). 헤더 줄에서 "N년"/"설정일이후" 라벨의
# x좌표를 찾아두고, 각 값 토큰을 순서가 아니라 x좌표가 가장 가까운 헤더
# 칸에 매칭한다 - 헤더를 못 찾은 경우에만 기존 순서 방식으로 되돌아간다.
# "최근"과 "N년" 사이 간격이 좁으면 pdfplumber가 "최근1년"으로 한 토큰에
# 붙여버리는 문서가 있다(KR5113420012 51페이지 실측 - 다른 문서는
# "최근 1년"으로 "최근"/"1년"이 분리돼 있음). "최근" 접두사가 붙어도
# 매치되게 허용한다("10년"/"2024년"처럼 숫자가 여러 자리인 건 여전히
# 안 걸림 - 정확히 한 자리 숫자+"년"만 허용).
YEAR_HEADER_RE = re.compile(r"^(?:최근)?(\d)년$")


def _detect_period_columns(lines):
    # 페이지 전체에서 "N년" 토큰을 찾으면(먼저 시도했던 방식) 같은 페이지의
    # "운용전문인력" 표나 각주 문장("설정일로부터 1년이 경과하지 않은...")에
    # 있는 엉뚱한 "1년"/"2년"까지 걸려서, 실제 수익률 표 헤더가 아닌 좌표를
    # 앵커로 써버리는 사고가 났다(위 파일 docstring의 "운용전문인력 표
    # 혼동" 주의사항과 같은 종류의 문제). 진짜 헤더는 "최근 1년 최근 2년
    # 최근 3년 최근 5년 설정일이후"처럼 여러 개의 "N년"/"설정일이후" 라벨이
    # 한 줄에 다 같이 나온다 - 그런 줄(3개 이상)만 헤더로 인정한다.
    for line in lines:
        anchors = {}
        for w in line:
            m = YEAR_HEADER_RE.match(w["text"])
            if m:
                label = {"1": "1y", "2": "2y", "3": "3y", "5": "5y"}.get(m.group(1))
                if label:
                    anchors[label] = w["x0"]
            elif "설정일이후" in w["text"]:
                anchors["since_inception"] = w["x0"]
        if len(anchors) >= 3:
            return anchors
    return None


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
# 줄을 찾는다. 문서마다 이 캡션이 쪼개지는 위치가 달라서, "연평균"과
# "수익률"이 붙어 "연평균수익률"로 한 토큰이 되거나 앞뒤로 괄호/콤마가
# 붙기도 한다(KR5123420039 실측: "(연평균수익률,"이 한 토큰이라 기존
# 패턴에 안 걸려서 "오프라인-퇴직연"과 "금(C)" 사이의 캡션 줄을 못
# 건너뛰고 멈췄고, class_code(C)를 놓쳐 null로 남았다). 괄호/콤마를
# 부호로 분리해서 허용한다.
CAPTION_FRAGMENT_RE = re.compile(r"^\(?(투자실적|추이|연평균|연환산|연평균수익률|수익률)\)?,?$")


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
    # 아니므로 별도 종류로 구분한다. 원래 "==" 완전일치였는데, 요약표(3페이지
    # 스타일)에서는 "투자신탁" 옆에 최초설정일이 같은 줄에 붙어 나와
    # normalized가 "투자신탁2013.08.19"처럼 되면서 매치가 실패해 기본값인
    # class_return(class_code=null)으로 잘못 새는 버그가 있었다(KR510902773M
    # 실측 - 상세표(45페이지, 설정일 없이 "투자신탁"만 있어 정상 매치)의
    # fund_aggregate 행과 row_kind가 달라져 cross-page dedup도 안 먹혔다).
    # "비교지수"/"변동성"과 같은 방식으로 부분일치로 바꾼다.
    if "투자신탁" in normalized:
        return "fund_aggregate"
    return "class_return"


def find_return_rows_on_page(page, page_num, section="가", known_classes=None):
    """section: 이 페이지 시작 시점의 "가/나" 섹션 상태(문서 내 이전 페이지에서
    이어받음). "나.연도별 수익률 추이" 섹션에 들어간 뒤로는 다음 "가" 제목을
    다시 만나기 전까지 데이터 행을 전부 스킵한다 - 컬럼 의미가 다른 표라
    "가" 표 스키마(1y/2y/3y/5y/since_inception)에 잘못 매핑하면 안 되기 때문.
    known_classes: class_fees.json에서 이미 확인된 이 상품의 클래스 코드
    목록(제공되면 라벨이 상품명 전체와 붙어 나오는 상세표에서 class_code
    보강용으로 씀). 반환값은 (rows, 이 페이지가 끝난 시점의 section)."""
    # x_tolerance=2(기본)로는 일부 문서에서 폰트 문제로 글자가 한 자씩 떨어져
    # 나오는 케이스(예: "4 .2 1")가 있어 숫자 인식이 아예 안 된다. 5로 올리면
    # 그 문제가 해결되면서도(검증 완료) 다른 문서의 값이 잘못 합쳐지진 않았다.
    words = page.extract_words(x_tolerance=5, keep_blank_chars=False)
    lines = cluster_lines(words)
    period_anchors = _detect_period_columns(lines)
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
        # 비교지수(benchmark) 행은 자기 몫의 최초설정일이 없어서 그 칸에
        # 날짜 대신 "-"를 찍는 문서가 있다(KR5113420012 실측: "비교지수 -
        # 5.43 5.18 3.19 1.49 4.00" - 진짜 값 5개 + 설정일 자리의 "-" 1개
        # 해서 6개가 되어 "값 5개 초과"로 행 전체가 통째로 버려지고
        # 있었다). 이 "-"는 항상 실제 값 5개(1y~since_inception) 왼쪽,
        # 최초설정일 칸 위치에 딱 1개만 나온다 - 정확히 6개(진짜 값 5개 +
        # 여분 대시 1개)일 때만, 그 여분이 대시인 걸 확인하고 가장 왼쪽
        # 것만 버린다. 아무 라벨도 없이 대시만 여러 개(예: "- - - - - - -
        # - -" 9개, 설정/환매현황 표의 빈 칸들 - KR510902773M 실측)인
        # 줄까지 5개로 뭉개면 있지도 않은 가짜 행이 생기므로, 6개인
        # 경우로만 좁힌다.
        if len(value_tokens) == 6 and DASH_RE.match(value_tokens[0]["text"]):
            value_tokens = value_tokens[1:]
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
                    elif known_classes:
                        # 상세 부속서류(제2부 등)는 라벨이 "(A1)"처럼 괄호로
                        # 안 떨어지고 "마이다스 책임투자 증권 투자신탁(주식)A1"
                        # 처럼 상품 전체 명칭 뒤에 클래스 코드가 그냥 이어
                        # 붙기도 한다(KR5157450017 실측). 이런 임의의 접미사를
                        # 정규식만으로 뽑으면 엉뚱한 문자열을 클래스 코드로
                        # 오인할 위험이 크다 - 대신 class_fees.json에서 이미
                        # 확인된 "이 상품의 진짜 클래스 코드 목록"에 있는
                        # 것으로 끝나는 경우에만(더 긴 코드 우선, 그 앞
                        # 글자가 영문/숫자가 아닌 경우만 - "BA1"의 "A1"처럼
                        # 엉뚱하게 잘라 오는 걸 방지) 인정한다.
                        # 클래스명이 3줄로 쪼개지면서 코드가 데이터 줄
                        # *두 줄* 아래에 오는 서식이 있다(KR5172450019
                        # 실측: "수수료미징구-" / "오프라인- 14.78 ..." /
                        # "11.6.27"(최초설정일) / "보수체감(C4)"). 한 줄만
                        # 보면 이런 클래스를 통째로 놓친다(이 문서는 15개
                        # 클래스 중 1개만 잡혔다).
                        # 그렇다고 코드 탐색 창을 일반적으로 넓히면 바로
                        # 다음 클래스의 이름을 이 행 것으로 잘못 가져오는
                        # 사고가 난다(이 파일 곳곳의 주석 참고 - 넓은 창은
                        # "틀렸는데 그럴듯한 값"을 만들어 이상치 검사도
                        # 못 걸러낸다). 그래서 두 줄 아래는 known_classes
                        # (class_fees.json으로 검증된 이 상품의 실제 코드
                        # 목록)로 걸러지는 이 경로에서만, 그리고 사이 줄이
                        # 값도 비교지수/변동성도 아닐 때만(=아직 이 행의
                        # 라벨이 이어지는 중일 때만) 본다.
                        # 한 줄까지만 본 텍스트로 "먼저" 맞춰보고, 거기서
                        # 못 찾을 때만 두 줄째를 붙여 다시 본다. 처음엔
                        # 무조건 두 줄째까지 붙여서 봤는데, 라벨이 한 줄
                        # 아래에서 이미 끝나는 문서(KR5157450017: "...
                        # 신탁(주식)A1"에서 끝남)는 그 뒤에 비교지수 줄이
                        # 딸려 붙어 "...3.98"로 끝나게 돼 endswith 매칭이
                        # 깨졌다(그 문서 클래스가 7개→1개로 회귀).
                        candidates = [(prev_line_text + " " + label_search_text).rstrip()]
                        if i + 2 < len(lines):
                            mid_has_value = any(
                                DECIMAL_RE.match(t) or DASH_RE.match(t)
                                for t in next_line_text.split()
                            )
                            if not mid_has_value and row_kind(next_line_text) not in ("benchmark", "volatility"):
                                candidates.append(
                                    (candidates[0] + " " + _line_text_skipping_captions(lines, i + 2, 1)).rstrip()
                                )
                        for combined in candidates:
                            # 괄호형("...보수체감(C3)")도 여기서 같이 본다.
                            # 위 CLASS_CODE_RE 검사는 좁은 창(pre_text +
                            # 한 줄)만 보기 때문에 두 줄 아래에 있는 괄호
                            # 코드를 못 잡는데(KR5172450019), 그렇다고 그
                            # 검사 자체의 창을 넓히면 옆 클래스 코드를
                            # 주워온다. known_classes에 있는 코드만
                            # 인정하는 이 경로에서는 넓혀도 안전하다.
                            for mm in reversed(list(CLASS_CODE_RE.finditer(combined))):
                                if mm.group(1) in known_classes:
                                    class_code = mm.group(1)
                                    break
                            if class_code:
                                break
                            for code in sorted(known_classes, key=len, reverse=True):
                                if combined.endswith(code):
                                    before = combined[: -len(code)]
                                    if not before or not before[-1].isalnum():
                                        class_code = code
                                        break
                            if class_code:
                                break

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
        inception_date = _normalize_date(date_m.group()) if date_m else None

        # 아직 수익률이 없는 신규 클래스 등은 원본이 그 칸에 "-"를 직접
        # 찍어서 "값이 없다"는 걸 명시적으로 밝힌다. 이걸 그냥 None으로
        # 뭉개면 "추출을 못 해서 모른다"와 "원본이 확인해서 없다고
        # 밝혔다"가 구분이 안 된다(class_fees의 peer_avg_fee/
        # sales_commission_desc에서 이미 사용자 지적으로 고친 것과 같은
        # 문제 - 실측: KR510902511M C1 등 56건에서 evidence는 전부
        # "-"인데 값은 None으로 나오고 있었다). 원본 토큰이 "-"면 "-"를
        # 그대로 남기고, "-"도 아니고 소수도 아닌(추출 자체가 안 된)
        # 경우에만 None으로 남긴다.
        # 값이 5개 다 있으면 순서(1y/2y/3y/5y/since_inception)와 x좌표
        # 매칭이 항상 같은 결과를 주지만, 중간 칸(3년/5년)이 원본에서
        # 통째로 비어 있으면(위 PERIOD_LABELS 주석 참고) 순서 방식은
        # 남은 값들을 앞칸부터 밀어 채워서 라벨이 틀어진다 - x좌표가 더
        # 가까운 헤더 칸으로 매칭한다.
        if period_anchors:
            labels = [
                min(period_anchors, key=lambda lbl: abs(period_anchors[lbl] - t["x0"]))
                for t in value_tokens
            ]
            if len(set(labels)) != len(labels):
                # 매칭이 겹치면(이례적인 레이아웃) 안전하게 기존 순서
                # 방식으로 되돌아간다.
                labels = PERIOD_LABELS[: len(value_tokens)]
        else:
            labels = PERIOD_LABELS[: len(value_tokens)]

        values = {
            labels[idx]: (
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
    # "_top"(줄의 y좌표)은 여기서 바로 지우지 않고 호출자의 중복 제거
    # 단계까지 들고 간다 - 값만으로 중복을 판정하면(아래 dedup 주석 참고)
    # 서로 다른 진짜 행을 잘못 지워버리는 사고가 나서, 페이지 안에서의
    # 실제 위치까지 같이 봐야 한다.
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
                r["inception_date"] = _normalize_date(cell_date)


def candidate_pages_for_doc(doc_id, max_page):
    """"최근"+"설정일"만으로 범위를 잡으면 문서 뒤쪽 상세 부속서류(제2부 등)의
    모펀드 관련 반복 섹션까지 걸려서 범위가 너무 넓어진다 (KR5113420012에서
    확인: 51/52페이지에 모펀드용 표가 또 있음). "투자실적추이"는 요약정보
    섹션에만 붙는 라벨이라 훨씬 정확한 스코핑 기준이다.

    ("최근"+"설정일" 둘 다로 넓혀서 실측해봤다가 되돌림: KR5113420012
    51페이지가 실제로는 C-e/S-P까지 있는 진짜 "가" 표였던 건 맞지만,
    코퍼스 전체로 넓히면 67개 문서에 새 후보 페이지가 생기면서 600건→
    1799건, 문서당 30~54행까지 튀는 걸 확인했다 - 대부분 무관한 표까지
    "최근"+"설정일" 문구가 우연히 같이 들어있어 걸린 것으로 보인다.
    "투자실적" 하나만으로 완전일치 스코핑하던 원래 방식이 안전하고,
    KR5113420012 같은 개별 문서의 후보 페이지 누락은 문서 단위로 더
    구조적인 판정 기준(예: 표 헤더가 "종류"+"최근N년"+"설정일이후" 컬럼
    구조와 정확히 일치하는지)을 새로 설계해야 한다 - 다음 과제로 남김.)"""
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


_KNOWN_CLASSES_BY_DOC = None


def _known_classes_for_doc(doc_id):
    """class_fees.json에 이미 확인된 이 상품의 class_code 목록(있으면) -
    상세 부속서류에서 라벨이 상품 전체 명칭에 붙어 나올 때 class_code
    보강용으로 쓴다. class_fees.json이 없으면(아직 안 만들었으면) 그냥
    빈 결과로 조용히 넘어간다 - 이 보강은 있으면 좋고 없어도 기존 동작
    그대로다."""
    global _KNOWN_CLASSES_BY_DOC
    if _KNOWN_CLASSES_BY_DOC is None:
        _KNOWN_CLASSES_BY_DOC = defaultdict(set)
        fp = os.path.join(REPO_ROOT, "class_fees.json")
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8") as f:
                for r in json.load(f):
                    if r.get("class_code"):
                        _KNOWN_CLASSES_BY_DOC[r["product_code"]].add(r["class_code"])
    return _KNOWN_CLASSES_BY_DOC.get(doc_id, set())


# 앞쪽 "요약정보" 섹션의 "가.연평균수익률" 표에는 대표 클래스 한두 개만 싣고,
# 전 클래스 수익률은 문서 뒤쪽(제2부 상세)의 같은 형식 표에만 싣는 문서가 아주
# 많다(KR5122420005 실측: 요약표엔 ClassC 하나뿐인데 52페이지 상세표엔 14개
# 클래스가 전부 있음). candidate_pages_for_doc은 "투자실적" 캡션이 붙은
# 요약 섹션만 보기 때문에 이 상세표를 통째로 놓쳤고, 그 결과 class_fees.json
# 기준 612개 클래스 중 수익률이 있는 건 269개뿐이었다(399개 클래스의 수익률이
# 빠짐 - 6축 중 "수익률" 축이 문서당 사실상 1개 클래스만 있는 상태였다).
#
# 이걸 고치려고 candidate_pages_for_doc의 페이지 후보를 넓히는 방식은 이미
# 3번 시도했다가 전부 되돌렸다(위 그 함수의 주석 참고 - 600건이 1799~1822건
# 으로 튀고, 좌수 변동표처럼 "라벨 + 숫자 5개" 모양이 똑같은 무관한 표까지
# 딸려 들어왔다). 페이지의 "모양"만으로는 이 상품의 진짜 수익률 표와 우연히
# 같은 모양인 다른 표를 구분할 수 없다는 게 그때의 결론이었다.
#
# 그래서 class_fees.py의 enrich_with_detail_fee_table에서 검증된 방식을 쓴다:
# 모양이 아니라 "이미 확실히 아는 값과 대조"해서 판단한다. 요약표에서 뽑아
# 둔 클래스(위 예시의 ClassC = 2.85/3.49/4.08/2.93/2.37)가 그 페이지에도
# 있고 값이 전부 일치할 때만 그 표를 "같은 표"로 인정하고, 거기 있는 나머지
# 클래스를 가져온다. 값이 하나라도 어긋나면 그 페이지는 통째로 버린다.
#
# 이 대조 하나로 "나.연도별 수익률 추이" 표(1~5년차 단년도 수익률 - 컬럼
# 의미가 달라서 같은 스키마에 넣으면 안 되는 표)도 자동으로 걸러진다:
# KR5122420005 53페이지의 ClassC는 2.85/4.13/5.29/1.93/0.50 이라 첫 값만
# 우연히 같고 나머지가 전부 달라 검증에 실패한다(섹션 제목 판정에 더해
# 이중 안전장치가 되는 셈).
def _values_agree(a, b):
    """(일치하는 숫자 칸 수, 어긋난 칸이 있는지) - 한쪽이 "-"인 칸은 비교에서
    제외한다(아직 수익률이 없는 클래스는 원본이 "-"로 두는데, 요약표와
    상세표의 기준일이 달라 한쪽에만 값이 생겼을 수 있어 그것만으로 다른
    표라고 볼 근거는 안 된다)."""
    matched = 0
    for k in PERIOD_LABELS:
        va, vb = a.get(k), b.get(k)
        if va is None or vb is None:
            continue
        try:
            fa, fb = float(va), float(vb)
        except (TypeError, ValueError):
            continue  # "-" 등 숫자가 아닌 칸
        if abs(fa - fb) <= 0.0005:
            matched += 1
        else:
            return matched, True
    return matched, False


def _merge_detail_into_summary(summary, detail):
    """같은 클래스가 요약표와 상세표에 둘 다 있을 때의 합치기 규칙.

    처음엔 "요약표 행을 그대로 두고 최초설정일만 채운다"는 식으로 필요한
    필드만 하나씩 백필했는데, 그러다 values(수익률 값 자체)를 빠뜨렸다
    (사용자 지적: "최초설정일 말고 다른것들은?"). 필드를 하나씩 떠올려
    가며 채우는 방식은 빠뜨리기 쉬워서, SQL의 FULL OUTER JOIN처럼 "이
    행이 가진 모든 필드에 대해 어느 쪽을 쓸지"를 여기 한 곳에 전부
    적어두는 방식으로 바꿨다(사용자 제안: "미리 FULL OUTER JOIN으로
    만들어두면 안 되나"). 새 필드가 생기면 여기만 고치면 된다.

    전제: 호출 전에 이미 값 대조(_values_agree)로 "같은 표의 같은 행"임을
    확인했다 - 숫자가 서로 어긋나는 경우는 애초에 여기까지 안 온다.
    """
    # values: 칸 단위로 COALESCE. 요약표가 "-"(원본이 비워둔 칸)이거나
    # 칸 자체가 없는데 상세표엔 실제 숫자가 있으면 그 숫자를 살린다
    # (반대로 요약표에 숫자가 있으면 그대로 둔다 - 둘 다 숫자면 위
    # 대조에서 같음이 확인된 값이라 어느 쪽을 써도 같다).
    # 상세표에서 가져온 필드는 어느 페이지에서 왔는지 필드 단위로 남긴다.
    # source_pages(=이 행이 어느 페이지들에서 만들어졌는지)만으론 "그
    # 최초설정일 어디서 봤어?"에 "4번 아니면 47번"까지밖에 못 답한다
    # (사용자 지적: "최초설정일을 47에서 가져와도 물어보면 47인지 아는
    # 거야?"). 근거 페이지를 틀리게 대면 사람이 그 페이지를 열어보고
    # 값을 못 찾게 되므로, 채워 넣은 필드는 정확한 페이지를 기록한다.
    # 이 행의 기본 page에서 온 필드는 안 적는다(그게 대부분이라 다
    # 적으면 파일만 커진다) - 여기 없는 필드는 page에서 온 것이다.
    from_detail = summary.setdefault("field_source_pages", {})
    for k in PERIOD_LABELS:
        sv, dv = summary["values"].get(k), detail["values"].get(k)
        if dv is None:
            continue
        try:
            float(dv)
        except (TypeError, ValueError):
            continue  # 상세표도 "-"면 채울 게 없다
        try:
            float(sv)
        except (TypeError, ValueError):
            summary["values"][k] = dv  # 요약표가 "-"이거나 없음 → 상세표 숫자 채택
            from_detail[f"values.{k}"] = detail["page"]
    # inception_date: COALESCE (최초설정일 칸이 요약표에만 있는 문서도,
    # 상세표에만 있는 문서도 실측으로 확인됨 - KR510902773M vs KR5157450017)
    if summary.get("inception_date") is None and detail.get("inception_date"):
        summary["inception_date"] = detail["inception_date"]
        from_detail["inception_date"] = detail["page"]
    # page/evidence/confidence/method: 요약표 것을 유지한다. page는 "이
    # 행의 값이 실제로 적혀 있던 위치"라 근거 표시에 쓰이는데, 요약표
    # 페이지가 문서 앞쪽이라 사람이 찾아보기도 쉽다.
    pages = summary.setdefault("source_pages", [summary["page"]])
    if detail["page"] not in pages:
        pages.append(detail["page"])


def enrich_with_detail_return_table(pdf, doc_id, existing_rows, used_pages, known_classes):
    """요약표엔 없고 뒤쪽 상세표에만 있는 클래스의 수익률 행을 보강한다.
    검증 실패 시(대조할 클래스가 없거나 값이 어긋나면) 아무것도 안 돌려준다."""
    known_rows = {
        r["class_code"]: r
        for r in existing_rows
        if r["row_kind"] == "class_return" and r.get("class_code")
    }
    known = {code: r["values"] for code, r in known_rows.items()}
    if not known:
        return []

    new_rows = []
    seen_codes = set(known)
    section = "가"
    for page_num in range(1, len(pdf.pages) + 1):
        page = pdf.pages[page_num - 1]
        rows, section = find_return_rows_on_page(
            page, page_num, section=section, known_classes=known_classes
        )
        if page_num in used_pages:
            continue  # 이미 요약표로 처리한 페이지 - 섹션 상태만 이어받고 넘어감
        class_rows = [r for r in rows if r["row_kind"] == "class_return" and r.get("class_code")]
        refs = [r for r in class_rows if r["class_code"] in known]
        if not refs:
            continue
        total_matched = 0
        conflict = False
        for r in refs:
            matched, bad = _values_agree(r["values"], known[r["class_code"]])
            total_matched += matched
            conflict = conflict or bad
        # 숫자 3칸 이상이 정확히 일치해야 인정한다 - 소수점 둘째 자리까지
        # 3개가 우연히 맞을 확률은 사실상 없어서, 이 표가 같은 표라는 걸
        # 충분히 특정한다(반대로 "-"뿐이라 대조할 숫자가 없는 문서는 그냥
        # 보강을 포기한다 - 틀린 값을 넣느니 없는 채로 두는 쪽).
        if conflict or total_matched < 3:
            continue
        for r in class_rows:
            r.pop("_top", None)  # 요약표 쪽은 _dedupe_and_merge가 지운다
            cur = known_rows.get(r["class_code"])
            if cur is not None:
                _merge_detail_into_summary(cur, r)
                continue
            if r["class_code"] in seen_codes:
                continue
            seen_codes.add(r["class_code"])
            r["product_code"] = doc_id
            r["method"] = "detail_return_table_cross_validated"
            r["source_pages"] = [r["page"]]
            new_rows.append(r)
    return new_rows


def process_doc(doc_id):
    pdf_candidates = glob.glob(os.path.join(DATA_DIR, doc_id, "*.pdf"))
    if not pdf_candidates:
        return []

    known_classes = _known_classes_for_doc(doc_id)
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
            rows, section = find_return_rows_on_page(
                page, page_num, section=section, known_classes=known_classes
            )
            for r in rows:
                r["product_code"] = doc_id
                results.append(r)

        # 요약표 기준으로 중복 제거/merge를 먼저 끝낸 뒤(그 결과가 상세표
        # 대조의 "정답지"가 된다) 뒤쪽 상세표 보강을 돌린다 - pdf 핸들이
        # 필요해서 이 with 블록 안에서 호출한다.
        final = _dedupe_and_merge(results)
        final += enrich_with_detail_return_table(
            pdf, doc_id, final, set(pages), known_classes
        )
        # 합쳐진 행만 source_pages를 갖고 나머지는 없으면 스키마가 들쭉날쭉
        # 해진다(조회하는 쪽이 매번 존재 여부를 따져야 함) - 모든 행이
        # 갖도록 맞춘다(안 합쳐진 행은 자기 page 하나).
        for r in final:
            r.setdefault("source_pages", [r["page"]])
            r.setdefault("field_source_pages", {})
        return final


def _dedupe_and_merge(results):
    """요약표 후보 페이지들에서 뽑힌 행들의 중복 제거 + 같은 클래스 merge.
    (원래 process_doc 본문이었는데, 상세표 보강이 "중복 제거까지 끝난
    결과"를 대조 기준으로 써야 해서 별도 함수로 분리했다.)"""
    # 페이지 후보를 넓게 잡다 보니(다음 페이지도 포함) 같은 행이 중복될 수 있다.
    # 처음엔 (row_kind, class_code, values의 1y값)으로 판정했는데, 비교지수/
    # 수익률변동성 행은 class_code가 애초에 없고(None) 같은 상품 안의 여러
    # 클래스가 값까지 우연히 똑같이 나오는 경우가 실제로 있어서(KR5120420091
    # 실측: 초단기우량채/Class A/Class Ae/Class Ce의 비교지수가 전부 "3.72
    # 3.88 3.88"로 동일) 서로 다른 진짜 행 4개 중 1개만 남기고 나머지 3개를
    # "중복"으로 오인해 지워버리고 있었다(사용자가 "클래스는 있는데 비교지수/
    # 변동성이 없다"고 지적해서 발견). page 안에서의 실제 세로 위치(_top)까지
    # 같이 봐야 서로 다른 행을 구분할 수 있다 - 진짜 중복(페이지 후보가
    # 겹쳐서 같은 물리적 줄을 두 번 읽은 경우)은 같은 페이지의 같은 위치에서
    # 다시 나오므로 여전히 걸러진다.
    seen = set()
    deduped = []
    for r in results:
        key = (r["row_kind"], r["class_code"], r["page"], round(r["_top"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    for r in deduped:
        del r["_top"]

    # 페이지 위치까지 다르면 서로 다른 진짜 행으로 봐야 하지만(위 참고),
    # 한 문서 안에 "가.연평균수익률" 표 자체가 통째로 두 번(앞쪽 요약정보 +
    # 뒤쪽 제2부 상세) 나오면서 같은 클래스의 값까지 완전히 똑같이 반복
    # 되는 경우가 실제로 있다(KR5153520012 실측 - 사용자가 "C, C-P2e
    # 없음"이라고 지적해서 다시 살렸는데, 그러면서 같은 클래스가 두 번
    # 잡히는 부작용이 새로 생겼다). class_fees.json이 "같은 클래스는
    # 문서당 행 하나"로 통일하는 것과 같은 원칙으로 맞춘다 - class_code가
    # 있는 행은 (product_code, class_code)로 하나만 남기고, confidence가
    # 같으면 뒤쪽 페이지(제2부 상세표) 것을 남긴다. 상세표 쪽이 클래스
    # 개수도 더 많이 나오는 걸 실측으로 확인해서(같은 문서에서 요약표엔
    # 없던 클래스가 상세표에만 있는 경우 - C-F 등) 상세표를 더 완전한
    # 쪽으로 본다.
    # 뒤쪽(상세표) 행을 통째로 승자로 남기면, 요약표에만 있고 상세표엔
    # 아예 컬럼 자체가 없는 필드(최초설정일 - 실측: KR510902773M의
    # "가.연평균수익률" 표가 요약표(3페이지)엔 최초설정일 칸이 있는데
    # 상세표(45페이지)엔 그 칸이 통째로 빠져 있었다)까지 패자 행과 함께
    # 버려진다. 값(values)/row_kind는 그대로 승자(뒤쪽) 기준으로 두되,
    # 승자에 없는 필드만 패자에서 채워 넣는다 - 사용자 지적: "최초
    # 설정일이 없어지는건데 ㄱㅊ은거야?" → merge로 처리.
    best_by_class = {}
    demoted_pages = set()
    for r in deduped:
        if not r["class_code"]:
            continue
        key = r["class_code"]
        cur = best_by_class.get(key)
        if cur is None:
            best_by_class[key] = r
            continue
        if (r["confidence"], r["page"]) > (cur["confidence"], cur["page"]):
            demoted_pages.add(cur["page"])
            if r.get("inception_date") is None and cur.get("inception_date") is not None:
                r["inception_date"] = cur["inception_date"]
            best_by_class[key] = r
        else:
            demoted_pages.add(r["page"])
            if cur.get("inception_date") is None and r.get("inception_date") is not None:
                cur["inception_date"] = r["inception_date"]
    kept_class_return_ids = {id(r) for r in best_by_class.values()}

    # class_code가 없는 행(비교지수/수익률변동성/투자신탁 합계)은 클래스
    # 처럼 명확한 키가 없다. 같은 페이지 안에서 값이 우연히 같은 건(위
    # KR5120420091 케이스처럼) 무조건 서로 다른 진짜 행이라 절대 지우면
    # 안 되지만, "같은 (row_kind, 값 전부)"인 행이 서로 다른 페이지에
    # 걸쳐 있으면 그건 표 자체가 문서 안에서 반복된 것(위 참고)이므로
    # 가장 뒤쪽 페이지 것만 남긴다 - class_code 유무와 무관하게 이
    # 문서에서 실제로 확인된 반복 패턴(44개 값 그룹, class_return이 한
    # 쪽에서 안 겹쳐도 비교지수/변동성/투자신탁 합계만 따로 반복되는
    # 경우도 있었다)을 포괄하도록 class_code 중복 제거와 독립적으로
    # 처리한다.
    no_class_groups = defaultdict(list)
    for r in deduped:
        if not r["class_code"]:
            no_class_groups[(r["row_kind"], tuple(sorted(r["values"].items())))].append(r)
    drop_ids = set()
    for group in no_class_groups.values():
        pages_in_group = {r["page"] for r in group}
        if len(pages_in_group) > 1:
            latest_page = max(pages_in_group)
            drop_ids.update(id(r) for r in group if r["page"] != latest_page)

    final = []
    for r in deduped:
        if r["class_code"]:
            if id(r) in kept_class_return_ids:
                final.append(r)
        elif id(r) not in drop_ids:
            final.append(r)
    return final


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
