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
# "A(수수료선취-오프라인)"처럼 클래스 코드가 괄호 안이 아니라 괄호 바로
# 앞에 붙어 나오는 문서가 있다(괄호 안은 클래스 코드가 아니라 상품유형
# 설명 - KR5125450023/KR5125450070 실측).
CLASS_CODE_PREFIX_RE = re.compile(r"^([A-Za-z]{1,3})\(")
# "(Cp(퇴직연금))"처럼 클래스 코드 뒤에 괄호가 또 하나 중첩돼 부가설명이
# 따라붙는 문서도 있다(코드 자체는 "Cp"/"Cpe"처럼 하이픈 없는 표기 -
# KR5114420027 실측, 원본 표를 사용자가 직접 캡처해서 확인함: 글자가
# 깨진 게 아니라 원래 이렇게 이중 괄호로 표기됨). 여는 괄호 바로 다음에
# 또 여는 괄호가 오면(닫는 괄호 대신) 그 사이를 코드로 본다.
CLASS_CODE_NESTED_RE = re.compile(r"\(([A-Za-z0-9\-]{1,8})\(")
# "운용전환일"이 고정된 캘린더 날짜가 아니라 목표기준가격 도달 같은 조건이
# 충족돼야 발생하는 문서가 있다(KR5147430065 실측: "목표전환형" 펀드 -
# "목표기준가격(종류A 누적기준가격 1,060원 이상)에 도달한 이후 운용전환").
# total_fee_after_conversion 등 필드만 보면 "후"가 언제/왜인지 알 수 없다.
# 처음엔 이 조건을 문장으로 풀어서 conversion_note에 남겼는데, 이 파일의
# 다른 모든 필드가 원본에서 그대로 뽑은 값(숫자/코드)이지 해석문이 아닌
# 것과 성격이 달라서("답을 미리 써주는" 꼴이 될 위험 - 사용자 지적)
# 숫자만 구조화된 필드로 남기기로 바꿨다. 의미(= 목표가격 도달 시 전환)는
# 필드 이름과 이 주석/README에 문서화해두고, 실제 문장으로 풀어 답하는 건
# 나중에 에이전트 규칙을 만들 때 다룬다.
CONVERSION_TRIGGER_RE = re.compile(r"목표기준가격\([^)]*?([\d,]+)\s*원\s*이상\)")
# "운용전환일 전/후로 수수료가 나뉜다"는 표는 "구분" 칸에 "최초설정일부터
# 운용전환일 전일까지"/"운용전환일부터 해지일까지"라는 문구를 직접 적어
# 둔다(KR5147430065 실측) - 숫자 개수·줄 간격만으로 추측하지 않고 이
# 문구가 실제로 근처에 있는지로 확인한다.
PERIOD_LABEL_RE = re.compile(r"최초설정일|운용전환일|해지일")
# 위 "구분" 칸 문구는 클래스명 칸과 같은 x좌표 구간(왼쪽)에 찍히는 경우가
# 있어("최초설정일부터"가 줄바꿈으로 "최초설정일부"/"터"로 쪼개짐 -
# KR5147430065 실측), evidence의 클래스명을 만들 때 이 문구를 걸러내지
# 않으면 "구분 총보수 수수료 최초설정일부 터 운용전환일 수수료선취- 전일까지
# 오프라인형(A)"처럼 클래스명과 구분 칸 문구가 뒤섞여 보인다(사용자 지적).
# "터"는 "부터"가 줄바꿈으로 쪼개진 조각이라 단어 자체엔 문맥이 없지만,
# 실제 클래스명(수수료선취/미징구/후취-오프라인/온라인...)에는 "터" 한
# 글자짜리 토큰이 나올 일이 없어 안전하게 같이 걸러낸다.
PERIOD_COLUMN_WORD_RE = re.compile(r"최초설정일|운용전환일|해지일|전일까지")
PERIOD_COLUMN_LONE_WORDS = {"터"}
# 클래스명이 줄바꿈될 때, 위/아래로 넓히는 과정에서 표 자체의 칸 이름(헤더)
# 줄까지 같이 끌려 들어오는 경우가 있다(예: KR518101002M 실측 - 표의 첫
# 클래스 행은 위로 넓혀도 "납입금"이 안 나오니 MAX_EXTRA_LINES까지 계속
# 올라가다가 "클래스종류/판매수수료/총보수·비용/1년~10년" 헤더 줄까지
# 포함해버려 evidence 클래스명이 "판매 총보수 수수료 수수료미징구-..."처럼
# 헤더 단어가 섞여 나온다 - 사용자가 KR5147430065에서 이 현상을 지적해
# 다른 문서도 전수 확인해보니 총 26건에서 같은 문제가 있었다). 헤더 줄은
# 실측 문서들에서 전부 "클래스/종류/구분/판매/수수료/판매보수/총보수/
# 보수/비용/동종유형/N년" 같은 정해진 칸 이름 단어로만 이루어져 있고 실제
# 클래스명 글자(수수료선취-오프라인(A) 등)가 섞이는 일이 없어, 그 줄의
# 모든 단어가 이 칸 이름 집합(+숫자)에 속할 때만 "헤더 줄"로 판단한다 -
# 클래스명이 조금이라도 섞인 줄은 걸리지 않도록 보수적으로 잡는다.
HEADER_LABEL_TOKENS = {
    "클래스", "종류", "(클래스)", "구분",
    "판매", "수수료", "판매보수", "판매수수료",
    "총보수", "보수", "비용", "년",
    "총보수·", "총보수ㆍ", "ㆍ비용", "·비용",
    "총보수·비용", "총보수ㆍ비용", "동종유형",
}


def _is_header_row(l):
    non_empty = [w for w in l if w["text"].strip()]
    if not non_empty:
        return False
    for w in non_empty:
        t = w["text"]
        if t in HEADER_LABEL_TOKENS or t.isdigit():
            continue
        return False
    return True


# "후취"(환매 시점에 떼는) 판매수수료 클래스는 "납입금액의 N%이내"가 아니라
# "OO시 환매시: 환매금액의 N%이내"처럼 판매수수료율의 기준을 "환매금액"으로
# 쓴다(KR5114420027 S클래스 실측: "3년 미만 환매시: 환매금액의 100분의 0.15
# 이내" - 이건 별개의 벌칙성 수수료가 아니라 이 클래스의 판매수수료 문구
# 자체다). 처음엔 "환매"가 들어간 줄을 별개의 조건문으로 보고 위/아래 확장의
# 경계로 아예 걷어냈는데, 그러면 정작 판매수수료 문구 자체가 통째로 빠져
# sales_commission_desc가 null이 돼버렸다(사용자가 evidence 이상하다고
# 지적해서 재확인 중 발견) - "납입금액"만 판매수수료 신호로 보던 기존 판정을
# "환매금액"도 같은 뜻으로 인정하도록 넓혀서 고쳤다(아래 사용처 참고).
# 다만 "3년/미/만/환/매/시" 같은 낱글자는 여전히 클래스명도 판매수수료
# 숫자도 아니므로, evidence 클래스명에 섞이지 않도록 이 문구가 있는 줄의
# 단어는 전부 "commission" 역할로 묶어서 보여준다(아래 role 분류 참고).
REDEMPTION_NOTE_RE = re.compile(r"환매")
CLASS_NAME_START_RE = re.compile(r"^수수료(선취|미징구|후취)")
# "환매금액의 N%이내"엔 원래 "OO년 미만 환매시:"라는 조건이 붙어 있다(위
# 실측 - 3년을 채우기 전에 환매하면 벌칙성으로 이 수수료가 붙는다는 뜻).
# 이 조건 없이 "환매금액의 N%이내"만 남기면 무조건 떼는 수수료처럼 보여서
# 뜻이 달라진다(사용자 지적: "3년미만 환매시인데 이건 언급이 없는데
# 있어야하는거 아닌가"). 글자 사이에 공백이 낀 채로도(letter-spacing)
# 찾도록 각 글자 사이에 \s*를 둔다.
REDEMPTION_CONDITION_RE = re.compile(r"(\d+)\s*년\s*미\s*만\s*환\s*매\s*시")


def _is_note_row(l):
    non_empty = [w for w in l if w["text"].strip()]
    if not non_empty:
        return False
    text = "".join(w["text"] for w in non_empty)
    if CLASS_NAME_START_RE.match(text):
        return False
    # "100분의 0.15 이내"처럼 "환매"라는 낱말 없이 판매수수료 비율만 나오는
    # 줄도 있다(위 KR5114420027 S클래스의 같은 문구가 줄바꿈으로 갈라진
    # 다음 줄 - "10"/"0분"/"의"/"0"/".1"/"5"/"이"/"내"). 이 조각들 하나하나는
    # _word_role의 어떤 패턴에도 안 걸려("0분"/"이"/"내" 등은 숫자도 "%"도
    # "이내" 전체 토큰도 아님) 기본값(class_name)으로 새어나간다. "100분의"
    # 패턴 자체가 이미 판매수수료 신호이므로(BUNUI_RE) 같이 잡는다.
    if REDEMPTION_NOTE_RE.search(text) or BUNUI_RE.search(text):
        return True
    # "이내"가 데이터 행을 사이에 두고 앞뒤로 갈라지면서("...이" / [데이터
    # 행] / "내") 뒤쪽 "내"만 뚝 떨어진 별도 줄로 남는 경우가 있다
    # (KR5114420016/KR5114420027 실측 - 클래스명 뒤에 "이 내"가 덧붙어
    # 보였다). 줄에 "이"/"내" 말고 다른 글자가 없으면 그 "이내" 잔여
    # 조각으로 본다.
    return text in ("이", "내", "이내")


# 판매수수료 칸은 숫자가 아니라 정형화된 문구("없음" 또는 "납입금액의 N%[ ]이내")인데,
# "납입금액의"와 "N%이내"가 셀 줄바꿈 때문에 서로 다른 줄(그 사이에 다른 칸 텍스트가
# 끼어든 상태)로 떨어져 있는 경우가 많아 하나의 정규식으로는 못 잡는다. "이내"까지
#3줄로 쪼개지는 경우도 있어("납입금" / "액의 N%" / "이내") 퍼센트 숫자만 여기서
# 찾고, "이내"가 바로 붙어 있을 필요는 없다고 본다 (이 좁은 윈도우 안의 "%"는
# 사실상 판매수수료율 말고는 나올 데가 없다).
SALES_COMMISSION_PCT_RE = re.compile(r"([\d.]+)\s*%")
# "N%" 대신 "100분의 N"(=N/100, 같은 뜻)으로 표기하는 문서가 있다(KR5114420027).
BUNUI_RE = re.compile(r"100\s*분의\s*([\d.]+)")


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


# "총보수·비용" 칸의 가운뎃점 연결자가 문서마다 다른 글자로 나온다
# (ㆍ/▪/･/· 뿐 아니라, KR5111450067처럼 임베딩 폰트가 유니코드 사용자
# 영역(PUA) 글자 ""로 대체해 나오는 경우도 실측으로 확인함) - "한글도
# 공백도 아닌 글자 하나 + 비용"으로 넓게 잡되, 단순히 본문 어딘가에 홀로
# 나오는 "비용"이라는 단어(예: 표 위 설명문 "총보수 및 비용")까지 걸리면
# 안 되므로 그 연결자 글자가 반드시 붙어 있어야 한다("비용" 단독 토큰은
# 이 글자 수 요건에 안 걸림).
HAS_COST_COLUMN_RE = re.compile(r"^(?:총보수)?[^가-힣\sA-Za-z0-9]비용$")


def page_has_cost_column_header(words, lines):
    """표에 "총보수ㆍ비용"(총보수+판매보수+동종유형총보수를 더한 결과) 칸이
    아예 없는 문서가 있다(KR5194450018 실측: 헤더가 총보수/판매보수/
    동종유형총보수 3개뿐, "총보수ㆍ비용" 헤더 자체가 없음). 이 경우 데이터
    행도 소수 3개(총보수/판매보수/동종유형총보수)만 나오는데, 기존 로직은
    "소수 3개=총보수/판매보수/총보수ㆍ비용(동종유형총보수 없음)"으로 가정해
    왔던 것과 똑같은 개수라 구분이 안 되고, 세 번째 소수(동종유형총보수)를
    총보수ㆍ비용으로 잘못 읽어 "총보수ㆍ비용 < 총보수" 같은 앞뒤가 안 맞는
    값이 나왔다. 헤더에 "ㆍ비용"류 표기(가운뎃점+비용, "총비용예시"의
    "총비용"과는 다름 - 그쪽은 가운뎃점이 없음)가 있는지로 이 칸의 존재
    여부를 확인한다.

    주의: 페이지 전체에서 찾으면 오탐이 난다 - KR5194450018은 표 헤더엔
    "총보수ㆍ비용"이 없는데도, 표 한참 아래(주석 "(주3)/(주4)" 문단, top
    ~380~450)에서 "총보수·비용비율은"/"총보수·비용" 같은 설명 문구로
    우연히 다시 등장해 실측으로 오탐을 확인했다. 표 헤더는 항상 데이터
    행(소수 3개 이상 있는 첫 줄)보다 위에 있고 주석은 항상 그 아래에
    있으므로, 첫 데이터 행보다 위쪽(top이 더 작은 영역)에서만 찾는다."""
    first_data_top = None
    for line in lines:
        if sum(1 for w in line if DECIMAL_RE.match(w["text"])) >= 3:
            first_data_top = line[0]["top"]
            break
    header_words = words if first_data_top is None else [w for w in words if w["top"] < first_data_top]
    return any(HAS_COST_COLUMN_RE.search(w["text"]) for w in header_words)


def page_cost_projection_years(words, lines):
    """비용예시가 보통 5개년(1/2/3/5/10년)인데, 기간별로 수수료율이 바뀌는
    문서(운용전환일 전/후로 수수료가 달라지는 구조 - KR5147430065 실측)는
    3개년(1/2/3년)뿐인 경우가 있다. 헤더에서 "5년"이 있는지로 판별한다
    (위 has_cost_column과 같은 이유로 첫 데이터 행보다 위쪽에서만 찾는다)."""
    first_data_top = None
    for line in lines:
        if sum(1 for w in line if DECIMAL_RE.match(w["text"])) >= 3:
            first_data_top = line[0]["top"]
            break
    header_words = words if first_data_top is None else [w for w in words if w["top"] < first_data_top]
    header_text = "".join(w["text"] for w in header_words)
    if "5년" in header_text:
        return ["1y", "2y", "3y", "5y", "10y"]
    if "3년" in header_text:
        return ["1y", "2y", "3y"]
    return None


def find_fee_rows_on_page(page, page_num, has_cost_column, next_page_head_lines=None, cost_years=None):
    words = page.extract_words(x_tolerance=2, keep_blank_chars=False)
    lines = cluster_lines(words)
    cost_years = cost_years or ["1y", "2y", "3y", "5y", "10y"]

    def _nearby_has_period_label(center, span=3):
        # "% 있는데 정수가 근처에 없는 줄"이 정말 "운용전환일 전/후 기간
        # 구분" 표 때문인지, 그냥 숫자 개수/줄 간격만으로 추측하지 않고
        # "구분" 칸에 실제로 적히는 문구("최초설정일"/"운용전환일"/
        # "해지일")가 근처에 있는지로 확인한다(사용자 지적: 표지 없이
        # 순전히 개수·간격만 보면 다른 문서에서 우연히 오탐할 위험이 있음
        # - 이 라벨 문구가 있으면 그 위험이 사실상 없어진다).
        lo, hi = max(0, center - span), min(len(lines), center + span + 1)
        nearby = "".join(w["text"] for k in range(lo, hi) for w in lines[k])
        return bool(PERIOD_LABEL_RE.search(nearby))

    rows = []
    for i, line in enumerate(lines):
        # decimals를 NUM_RE로 거른 뒤 다시 추리면 "1.18%"처럼 %가 붙은 토큰이
        # NUM_RE(퍼센트 미허용)에 애초에 안 걸려 통째로 빠진다 - line에서 직접
        # 따로 찾는다.
        decimals = [w for w in line if DECIMAL_RE.match(w["text"])]
        int_like = [w for w in line if NUM_RE.match(w["text"]) and w not in decimals]

        # 수수료 %(소수)와 비용예시 정수가 아예 다른 줄(y좌표)에 떨어져
        # 있는 문서가 있다(KR5147430065 실측: "운용전환일" 전/후로 클래스당
        # 수수료가 두 번 나오는 구조인데, 첫 번째 시기 줄엔 %만 있고, 그
        # 2줄 아래 별도 줄에 정수만 있음 - "최초설정일부"/"터 운용전환일
        # 0.443% ... 0.443%"/"수수료선취- 납입금액의"/"전일까지 145 192
        # 241"). 이 줄 자체엔 정수가 모자라면(소수는 있는데) 바로 아래
        # 몇 줄 안에서 소수 없이 정수만 있는 줄을 찾아 빌려온다 - 그런
        # 줄이 없으면(대부분의 다른 문서) 원래대로 아무 효과 없다.
        if decimals and len(int_like) < 3 and _nearby_has_period_label(i):
            for k in range(i + 1, min(i + 4, len(lines))):
                cand_line = lines[k]
                if any(DECIMAL_RE.match(w["text"]) for w in cand_line):
                    break
                cand_nums = [w for w in cand_line if NUM_RE.match(w["text"])]
                if len(cand_nums) >= 3:
                    int_like = cand_nums
                    break

        # 판매수수료("납입금액의 N%이내") 문구의 퍼센트 숫자가 데이터 줄
        # 자체에 얹혀 나오는 서식이 있다(KR5123490013 등: "의 0.8% 0.845
        # 0.40 0.75 0.868 ..." - "납입금액"은 윗줄, "의 N%"만 이 줄에 걸침).
        # 이 %값도 DECIMAL_RE에 걸려 decimals 맨 앞에 끼어들면서 실제 4개
        # 컬럼(총보수/판매보수/동종유형총보수/총보수·비용)이 통째로 한 칸씩
        # 밀려 읽힌다(총보수 자리에 판매수수료%가, 판매보수 자리에 실제
        # 총보수가... 실측으로 확인, 마지막 총보수·비용 값은 아예 유실됨).
        # 실제 컬럼은 최대 4개뿐이므로 소수가 5개면 맨 왼쪽은 무조건 이
        # 판매수수료% 이다(모호할 수 없음 - 드롭). 소수가 4개인 경우는
        # 정상적인 "4개 다 실제 컬럼" 케이스와 개수가 같아 구분이 안 되는데,
        # 실측 사례(KR5114420016)에서 이땐 맨 앞 소수 바로 앞/뒤에 "의"
        # (납입금액"의") 또는 "이내"가 같은 줄에 붙어 있어 그걸로 가려낸다.
        # 단, 이 "의"/"이내" 인접 판정만으로는 오탐이 난다(KR5113420069:
        # "취-오프 액의 0.3910 ..."에서 "액의" 바로 뒤가 하필 진짜 총보수
        # 값이었음, 진짜 판매수수료%는 다음 줄 "0.02%"였음 - 실측으로 확인).
        # 판매수수료 스트레이 값은 반드시 "%"가 붙어 있는 반면(퍼센트 값이므로)
        # 총보수 등 실제 컬럼 값은 이 데이터 줄 자체에서는 "%" 없이 나온다는
        # 점으로 추가 필터링한다(맨 왼쪽 소수 자신이 "%"로 끝나야만 스트레이
        # 후보로 본다).
        if len(decimals) == 5 and decimals[0]["text"].endswith("%"):
            decimals = decimals[1:]
        elif len(decimals) == 4 and decimals[0]["text"].endswith("%"):
            idx0 = next((idx for idx, w in enumerate(line) if w is decimals[0]), -1)
            prev_w = line[idx0 - 1] if idx0 > 0 else None
            next_w = line[idx0 + 1] if 0 <= idx0 + 1 < len(line) else None
            if (prev_w and prev_w["text"].endswith("의")) or (next_w and next_w["text"] == "이내"):
                decimals = decimals[1:]

        # 판매수수료 문구가 "%" 대신 "100분의 N"(=N/100, 같은 뜻)으로 나오는
        # 문서가 있다(KR5114420027 실측: "납입금액의 100분의 0.3 이내" - 이
        # "0.3"엔 "%"가 안 붙어서 위의 % 기반 판별에 안 걸리고, 총보수 등
        # 진짜 4개 컬럼 앞에 끼어들어 전부 한 칸씩 밀리는 같은 종류의 문제를
        # 일으켰다). 맨 왼쪽 소수 바로 앞 토큰이 "...분의"로 끝나면(그
        # 사이에 낀 판매수수료 숫자라는 뜻) 마찬가지로 드롭한다.
        if len(decimals) >= 4:
            idx0 = next((idx for idx, w in enumerate(line) if w is decimals[0]), -1)
            prev_w = line[idx0 - 1] if idx0 > 0 else None
            if prev_w and prev_w["text"].endswith("분의"):
                decimals = decimals[1:]

        # 일부 문서(KR5169950018 등)는 네 번째 소수(총보수·비용)의 소수점이
        # 쉼표로 잘못 찍혀 나온다("1.807"이어야 할 값이 "1,807"로 나와서
        # 정수(비용예시)로 오인됨) - 그러면 소수 3개+정수 6개(정상은 4개+5개)
        # 라는 특이한 개수 조합이 되는데, 이때만 좁게 판별해서 정수 목록의
        # 첫 값을 다시 소수로 되돌린다. 총보수(decimals[0])보다 약간 큰
        # 값이어야 한다는 조건까지 걸어(총보수·비용은 총보수+기타비용이라
        # 항상 총보수 이상) 진짜 큰 비용예시 정수(예: "1,937")를 잘못
        # 건드리지 않게 한다.
        # 판매보수(3번째 열)가 소수점 없이 정수 하나로만 나오는 경우가 있다
        # (KR5153420318/KR5153450785 실측: "1" - 원본 PDF 글자 자체가 그렇게
        # 찍혀 있음, 추출 오류 아님 - 같은 줄 다른 숫자들과 폰트/크기 동일함을
        # 확인). 판매보수 칸이 소수 목록에서 통째로 빠지면 그 다음 소수(동종
        # 유형총보수)를 판매보수로 잘못 읽고, 정수였던 "1"은 1년 비용예시
        # 자리로 잘못 흘러들어간다. 총보수(decimals[0])와 그 다음 소수
        # (decimals[1], 정상 문서라면 판매보수 그 자체) 사이 x좌표에 낀 정수
        # 토큰이 정확히 하나 있으면(비용예시 정수들은 훨씬 오른쪽에 있어
        # 안 걸림) 그게 판매보수라고 보고 소수 목록 제자리에 되돌린다.
        if len(decimals) >= 2:
            between = [
                w for w in int_like
                if decimals[0]["x1"] < w["x0"] < decimals[1]["x0"]
            ]
            if len(between) == 1:
                decimals = [decimals[0], between[0]] + decimals[1:]
                int_like = [w for w in int_like if w is not between[0]]

        if len(decimals) == 3 and len(int_like) == 6:
            m = re.match(r"^(\d),(\d{3})$", int_like[0]["text"])
            if m:
                candidate = float(f"{m.group(1)}.{m.group(2)}")
                if candidate >= float(decimals[0]["text"].rstrip("%")):
                    fixed_word = dict(int_like[0])
                    fixed_word["text"] = f"{m.group(1)}.{m.group(2)}"
                    decimals = decimals + [fixed_word]
                    int_like = int_like[1:]

        # 총보수·비용 칸도 없고(has_cost_column=False) 동종유형총보수까지
        # "-"인 문서가 있다(KR5116501001 실측: 판매수수료도 "-", 총보수/
        # 판매보수만 진짜 소수, 동종유형총보수도 "-") - 이러면 소수가 2개
        # (총보수/판매보수)뿐이라 기존 3개 기준에 걸려 이 문서 전체가
        # 통째로 빠지고 있었다("데이터 100건 중 97건만 나온다"고 사용자가
        # 지적해서 발견). 소수 2개까지는 허용하되, 이 행이 진짜 총보수
        # 표의 데이터 행이라는 걸 더 확실히 하기 위해(엉뚱한 텍스트가
        # 우연히 소수 2개+정수 4개를 만족하는 오탐 방지) 총보수 앞쪽에
        # "-"(판매수수료 없음 표시) 단독 토큰이 있을 때만 허용한다 - 이
        # 문서에서 실측으로 확인된 실제 패턴과 동일.
        if len(decimals) == 2:
            has_leading_dash = any(
                w["text"] == "-" and w["x0"] < decimals[0]["x0"] for w in line
            )
            # 위 리딩 대시 기준은 KR5116501001(판매수수료="-", 동종유형
            # 총보수="-")에 맞춰 만든 것인데, 소수 2개짜리 행이 이 패턴
            # 하나만 있는 게 아니었다(KR5194450018 실측: 12개 클래스 중
            # 7개(W/F/S/RP/RP-e/S-P/CP)가 판매수수료="없음"이거나 빈칸,
            # 동종유형총보수는 뒤쪽 "-"이거나 아예 빈칸이라 리딩 대시가
            # 없어서 통째로 빠지고 있었다 - 사용자가 "클래스 없는것들이
            # 너무 많다"고 지적해서 발견). 리딩 대시가 없어도, 바로
            # 위/아래(±2줄) 근처에 클래스명 시작 패턴("수수료선취/
            # 미징구/후취-")이 있으면 진짜 총보수 표의 데이터 행으로
            # 본다(엉뚱한 문장이 우연히 소수 2개+정수 4개를 만족해도
            # 그 근처에 클래스명이 있을 리는 없어 오탐 위험이 낮다).
            nearby_class_name = any(
                CLASS_NAME_START_RE.match("".join(w["text"] for w in lines[k]))
                for k in range(max(0, i - 2), min(len(lines), i + 3))
            )
            if not has_leading_dash and not nearby_class_name:
                continue
        elif len(decimals) == 1:
            # 총보수만 진짜 소수고 판매보수·동종유형총보수가 둘 다 "-"인
            # 행도 있다(KR5194450018 W클래스 실측: "0.765 - - 78 161
            # 247 433 986" - 총보수 뒤에 대시가 두 개 연달아 나옴). 위와
            # 같은 클래스명 인접 여부로 진짜 데이터 행인지 확인한 뒤,
            # 총보수 뒤·비용예시 정수 앞 구간의 "-" 토큰들을 순서대로
            # 판매보수/동종유형총보수 자리로 채운다.
            nearby_class_name = any(
                CLASS_NAME_START_RE.match("".join(w["text"] for w in lines[k]))
                for k in range(max(0, i - 2), min(len(lines), i + 3))
            )
            if not nearby_class_name:
                continue
            right_bound = int_like[0]["x0"] if int_like else float("inf")
            dashes_after = sorted(
                (w for w in line if w["text"] == "-" and decimals[0]["x1"] < w["x0"] < right_bound),
                key=lambda w: w["x0"],
            )
            if not dashes_after:
                continue
            # 실제 대시 단어 객체를 그대로 써야(x0/x1 좌표 포함) 아래
            # 열 배정 로직이 그 좌표를 다시 참조해도 안전하다.
            decimals = [decimals[0]] + dashes_after[:2]
        elif len(decimals) < 1:
            continue
        if len(int_like) < min(4, len(cost_years)):
            # "운용전환일" 전/후로 수수료가 바뀌는 문서(위 참고, KR5147430065)는
            # 전환 후 시기도 소수(%) 4개는 멀쩡히 있는데 비용예시 정수가
            # 원본 자체에 없다(사용자가 원본 표를 캡처해서 확인 - 전환
            # 후 줄엔 정말 %만 있고 1/2/3년 비용예시는 전환 전 줄 것 하나만
            # 공유됨). 이 값도 버리지 말고 바로 앞에서 찾은 행(같은 클래스의
            # 전환 전 값)에 "전환 후" 값으로 덧붙인다 - 페이지 안에서 아주
            # 가까운 줄에 있을 때만(다른 클래스와 헷갈릴 위험 방지).
            if (
                len(decimals) == 4 and rows
                and (i - rows[-1].get("_row_line_idx", -99)) <= 8
                and _nearby_has_period_label(i)
            ):
                rows[-1]["total_fee_after_conversion"] = decimals[0]["text"].rstrip("%")
                rows[-1]["distribution_fee_after_conversion"] = decimals[1]["text"].rstrip("%")
                rows[-1]["peer_avg_fee_after_conversion"] = decimals[2]["text"].rstrip("%")
                rows[-1]["total_fee_and_cost_after_conversion"] = decimals[3]["text"].rstrip("%")
            continue

        # 운용전문인력(운용역) 표 행이 같은 블록에 섞여 있을 수 있다 - 생년
        # (19xx/20xx, 단독 숫자)이나 콤마 있는 큰 수(운용규모)가 라벨 자리에
        # 있으면 그 표로 보고 제외한다(수익률 표에서 실제로 겪은 문제와 동일 -
        # "김혜용 1980 8개 55.78% ..."이 총보수 55.78%인 것처럼 잘못 뽑힘).
        pre_text_words_check = [w for w in line if w["x0"] < decimals[0]["x0"]]
        if any(re.fullmatch(r"(19|20)\d{2}", w["text"]) for w in pre_text_words_check):
            continue
        if any(re.fullmatch(r"\d{1,3},\d{3}", w["text"]) for w in pre_text_words_check):
            continue

        # 열 순서: [클래스종류] [판매수수료] 총보수 판매보수 동종유형총보수 총보수·비용
        #          1년 2년 3년 5년 10년  (동종유형총보수는 '-'로 빠질 수 있어 소수 3개까지 허용)
        has_peer_avg = len(decimals) >= 4
        total_fee, distribution_fee = decimals[0], decimals[1]
        if has_peer_avg:
            peer_avg_fee = decimals[2]
            total_fee_and_cost = decimals[3]
        elif len(decimals) == 2:
            # 총보수·비용 칸도 없고 동종유형총보수도 "-"인 문서(위 참고) -
            # 판매보수 뒤, 비용예시 정수들 앞 구간에 단독 "-"가 있으면
            # 동종유형총보수가 "-"로 확인된 것으로 본다.
            total_fee_and_cost = None
            right_bound = int_like[0]["x0"] if int_like else float("inf")
            dash_between = [
                w for w in line
                if w["text"] == "-" and distribution_fee["x1"] < w["x0"] < right_bound
            ]
            peer_avg_fee = "-" if dash_between else None
        elif not has_cost_column:
            # 이 페이지엔 "총보수ㆍ비용" 칸 자체가 없다(위 has_cost_column
            # 참고) - 소수 3개는 총보수/판매보수/동종유형총보수이고
            # 총보수ㆍ비용은 원본에 없는 정보라 null로 둔다(하이픈으로 확인된
            # 부재가 아니라 애초에 그 칸이 없는 것 - null이 맞다).
            peer_avg_fee = decimals[2]
            total_fee_and_cost = None
        else:
            total_fee_and_cost = decimals[2]
            # 동종유형총보수 칸이 원본에 "-"로 명시돼 있으면(정보가 없다는
            # 걸 실제로 확인한 것) null이 아니라 "-"로 남긴다 - 못 찾은 것과
            # 원본이 실제로 "-"라고 밝힌 건 다른 의미다(사용자가 지적함).
            dash_between = [
                w for w in line
                if w["text"] == "-" and distribution_fee["x1"] < w["x0"] < total_fee_and_cost["x0"]
            ]
            peer_avg_fee = "-" if dash_between else None
        cost_projection = {
            y: int_like[idx]["text"] for idx, y in enumerate(cost_years) if idx < len(int_like)
        }

        pre_text_words = [w for w in line if w["x0"] < total_fee["x0"]]
        class_part1 = " ".join(w["text"] for w in pre_text_words)

        # 클래스 코드와 판매수수료 문구는 이 줄 또는 인접한 줄(줄바꿈으로 나뉜 셀)에
        # 걸쳐 있을 수 있어서, 이 줄 기준 앞뒤로 창을 넓혀서 찾는다.
        #
        # 판매수수료 문구가 "납입금"/"액의 N%"/"이내" 3줄로 나뉘어 데이터 줄
        # 앞뒤로 2줄 넘게 걸치는 경우가 실측으로 확인됐다(KR510902511M A-e:
        # "납입금"(2줄 위)/"액의"(1줄 위)/[데이터]/"0.5%"(1줄 아래)/"이내"
        # (2줄 아래) - 총 5줄). 그렇다고 무작정 고정폭(±2 등)으로 넓히면
        # 바로 옆 클래스 행의 판매수수료를 잘못 가져오는 더 나쁜 문제가
        # 생긴다 - 실측으로 두 가지 경로를 확인했다: (a) 옛날에 확인한 "C
        # 클래스가 A 클래스의 0.10%이내를 잘못 가져옴", (b) 이번에 새로
        # 확인한 KR5114420027 - 이 문서는 클래스 한 줄에 줄바꿈 없이 값이
        # 다 붙어 나오는 서식인데, 그래도 바로 위/아래 줄이 "다른 클래스의
        # 완전한 데이터 행 그 자체"라서(줄바꿈이 아예 없으니 인접 줄=다른
        # 클래스 전체), 그걸 무조건 한 줄까지는 포함하던 기존 로직이 그
        # 다른 클래스의 %값을 그대로 판매수수료로 잘못 집어왔다(A가 C의
        # "0.4500%"를, C가 A의 "0.3000%"를 서로 잘못 가져옴).
        #
        # 그래서 "다른 클래스의 완전한 데이터 행(소수 3개 이상)"은 바로
        # 인접한 한 줄이라도 절대 포함하지 않는다(포함 여부 자체를 먼저
        # 판단) - 그 다음에야, 포함하기로 한 범위 안에서 "납입금"/"이내"을
        # 찾을 때까지, 역시 그런 완전한 데이터 행이나 클래스 코드 괄호
        # "(...)"가 있는 줄(다른 클래스명의 마지막 조각)을 만나기 전까지만
        # 늘려간다.
        def _is_full_data_row(l):
            return sum(1 for w in l if DECIMAL_RE.match(w["text"])) >= 3

        def _has_class_paren(l):
            text = " ".join(w["text"] for w in l)
            if CLASS_CODE_RE.search(text):
                return True
            # 글자를 한 자씩 따로 찍는 서식(letter-spacing)이 있는 문서는
            # "(Ce)"가 "(", "Ce", ")"처럼 별도 단어로 쪼개져 나와 join한
            # 텍스트에 공백이 끼어(예: "( Ce )") 위 정규식이 못 잡는다
            # (KR5114420022 실측: 이 탓에 닫는 괄호 줄을 경계로 못 보고
            # 계속 아래로 넓혀가다 다음 각주 문단까지 클래스명에 끌려
            # 들어왔다). 공백을 다 지운 버전으로도 한 번 더 확인한다.
            if CLASS_CODE_RE.search(text.replace(" ", "")):
                return True
            # "(Cp(퇴직연금))"처럼 코드 뒤에 괄호가 중첩되는 문서(위
            # CLASS_CODE_NESTED_RE 참고)는 여는 괄호가 두 번 나오고 닫는
            # 괄호가 한 번에 다 안 붙어 있어서 위 CLASS_CODE_RE로는(공백을
            # 지워도) 못 잡는다 - 사이에 낀 한글 설명 때문에 "괄호 안이
            # 전부 영숫자"라는 전제가 깨짐(KR5114420027 실측: 이 탓에 Cp의
            # 닫는 괄호 줄을 경계로 못 보고 다음 클래스(Cpe) 이름 시작까지
            # 서로 끌고 들어왔다). 이런 줄도 중첩 코드 정규식으로 확인한다.
            if CLASS_CODE_NESTED_RE.search(text) or CLASS_CODE_NESTED_RE.search(text.replace(" ", "")):
                return True
            # 일부 문서(KR5125450023)는 클래스 코드가 "A(수수료선취-...오프
            # 라인)"처럼 여는 괄호가 클래스 행 자체에, 닫는 괄호만 다음 줄에
            # 따로 떨어져 나온다("오프라인)") - 이 경우 CLASS_CODE_RE(여닫는
            # 괄호가 한 줄에 다 있어야 매치)는 못 잡아서, 여는 괄호 없이
            # 닫는 괄호만 있는 줄도 "다른 클래스명의 마지막 조각"으로 본다.
            return ")" in text and "(" not in text

        MAX_EXTRA_LINES = 3

        def _has_word(l, word):
            # "납입금"은 "납입금액"(전화번호처럼 붙여 나온 문서도 있음)처럼
            # 뒤에 다른 글자가 붙는 경우가 있어(KR5127420083 실측), 정확히
            # 일치할 때만 찾으면 못 잡고 지나쳐 위/아래로 계속 넓혀버린다
            # (결국 표 헤더까지 evidence에 끼어드는 사고로 이어졌다) - 부분
            # 일치로 찾는다.
            if any(word in w["text"] for w in l):
                return True
            # 글자를 한 자씩 따로 찍는 서식(letter-spacing)이 있는 문서는
            # "이내"조차 "이"/"내"처럼 서로 다른 단어로 쪼개져 나와 단어
            # 하나씩만 봐서는 못 찾는다(KR5114420016 실측: 환매수수료
            # 문구의 "이내"가 쪼개져 있어 아래로 넓히는 걸 못 멈추고 다음
            # 각주 문단까지 끌고 옴). 줄 전체를 이어붙인 텍스트로도 한 번
            # 더 확인한다.
            return word in "".join(w["text"] for w in l)

        # 바로 위/아래 한 줄은 줄바꿈된 이 행 자신의 클래스명 조각을 담기
        # 위해 일단 넣어본다(경계에 걸리지만 않으면).
        base_up = (
            [lines[i - 1]]
            if i - 1 >= 0
            and not _is_full_data_row(lines[i - 1])
            and not _has_class_paren(lines[i - 1])
            and not _is_header_row(lines[i - 1])
            else []
        )
        base_down = (
            [lines[i + 1]]
            if i + 1 < len(lines)
            and not _is_full_data_row(lines[i + 1])
            and not _is_header_row(lines[i + 1])
            else []
        )

        # 이 행 자신의 판매수수료가 이미 "없음"/"-"으로 결론 나 있는지는 이
        # 데이터 줄 자체가 아니라 줄바꿈된 자신의 라벨 줄(방금 넣어본 바로
        # 위/아래 한 줄)에 있을 수도 있다(KR5152420028 Ce 실측: "없음"이
        # 데이터 줄이 아니라 바로 위의 라벨 줄 "수수료미징구- 없음"에 있음).
        # 그래서 데이터 줄 + 바로 위/아래 한 줄까지 합쳐서 확인한다.
        own_row_no_commission = any(
            _has_word(wl, "없음") for wl in (base_up + [line] + base_down)
        ) or any(
            w["text"] == "-" and w["x0"] < total_fee["x0"] for w in line
        )

        # "없음"/"-"으로 이미 결론 난 행은 "납입금"을 더 찾아 위/아래로 넓힐
        # 이유가 없다(넓히면 다른 클래스 문구를 잘못 끌고 오는 사고만 남음 -
        # KR510902511M C1 실측). 그리고 바로 위/아래 한 줄조차, 데이터도
        # 없고 괄호도 없어 경계 판정엔 안 걸리지만 사실은 *다른* 클래스의
        # 판매수수료 문구 잔재("이내"/"납입금" 단독)일 수 있어(C-e 실측)
        # 그런 경우 아예 빼버린다.
        if own_row_no_commission:
            if base_up and _has_word(base_up[0], "이내"):
                base_up = []
            if base_down and _has_word(base_down[0], "납입"):
                base_down = []

        # 이 행 자신의 줄(class_part1)에 이미 괄호 닫힌 클래스 코드가 완전히
        # 있으면("수수료미징구-온라인(C-e)"처럼 한 줄에 다 있는 경우), 클래스명은
        # 이미 완성된 것이라 위/아래로 더 이어붙일 필요가 없다. 그런데도 바로
        # 아래 줄을 무조건 넣다 보니, 그게 사실은 *다음* 클래스의 이름 시작
        # 부분("수수료미징구-오프라인-개"처럼 아직 괄호가 안 나온 라벨 앞
        # 조각)인 경우 잘못 이어붙여 버렸다(C-e 실측: "수수료미징구-온라인
        # (C-e) 수수료미징구-오프라인-개"처럼 다음 클래스 이름이 붙어버림).
        # 클래스명은 항상 "수수료선취-"/"수수료미징구-"/"수수료후취-"로
        # 시작하므로, 이미 완성된 행에서 인접 줄이 이 패턴으로 새로 시작하면
        # 다음 클래스의 것으로 보고 뺀다.
        own_class_name_complete = bool(CLASS_CODE_RE.search(class_part1))
        if own_class_name_complete:
            # 글자를 한 자씩 따로 찍는 서식은 "수수료미징구"조차 "수수료미"/
            # "징"/"구"처럼 여러 단어로 쪼개져 나와서, 단어 하나하나를 이
            # 패턴과 비교하면(예전 방식) 매칭되는 단어가 하나도 없어 못
            # 걸러낸다(KR5114420027 Ce 실측: 다음 클래스(C-P)의 시작
            # "수수료미 징 구 -오 프 라인-개"가 안 걸러지고 그대로 끌려
            # 들어왔다). 줄 전체를 이어붙인 텍스트로 비교한다(표 왼쪽 여백
            # 캡션은 클래스명 칸보다 왼쪽(x0<70)이라 먼저 제외).
            down_text = "".join(w["text"] for w in base_down[0] if w["x0"] >= 70) if base_down else ""
            if down_text and CLASS_NAME_START_RE.match(down_text):
                base_down = []
            # "OO년 미만 환매시: 환매금액의..." 같은 조건문 줄은 항상 그
            # *다음* 클래스(아직 코드가 안 나온 쪽)의 것이다 - 이 행은
            # 클래스명+코드가 이미 이 줄에서 끝났으니 판매수수료도 이미
            # 이 줄에 다 있거나("납입금액의 N%이내"), 아예 "없음"이다.
            # 그런데도 바로 아래 조건문 줄을 계속 끌고 오면, 다음 클래스
            # 것인 "환매금액" 기준과 "OO년 미만" 조건이 엉뚱하게 이
            # 행에 붙어버린다(KR5114420016 R-A 실측: "수수료선취-오프라인
            # (R-A)"는 원래 "납입금액의 0.3%이내"인데, 바로 아래 S클래스의
            # "3년 미만 환매시: 환매금액의..." 조건문까지 끌려와서
            # "환매금액의 0.3%이내"로 잘못 나왔다 - 사용자가 "3년미만
            # 환매시 언급이 없는데 있어야 하는거 아니냐"고 물어서 고치다가
            # 발견).
            # 다만 이 행 자신의 줄에 판매수수료가 아직 안 나와 있으면(예:
            # KR5114420027 S클래스 - 이름+코드만 한 줄에 있고 "100분의
            # 0.15 이내"는 바로 아래 줄에 있음) 그 조건문 줄이야말로 이
            # 행의 진짜 판매수수료이므로 빼면 안 된다. "이 줄에 이미
            # 납입금/환매금액/% 판매수수료 신호가 있는지"로 구분한다.
            own_commission_already_on_line = bool(
                SALES_COMMISSION_PCT_RE.search(class_part1)
                or BUNUI_RE.search(class_part1)
                or "납입" in class_part1
                or "환매금액" in class_part1
            )
            if base_down and _is_note_row(base_down[0]) and own_commission_already_on_line:
                base_down = []
            if base_up and any(")" in w["text"] for w in base_up[0]):
                base_up = []

        # 판매수수료가 없다고 이미 결론난 행(own_row_no_commission)도
        # 클래스명 자체는 여러 줄에 걸쳐 나뉠 수 있다(KR5113450111 실측:
        # "수수료미"(2줄 위)/"징구-"(1줄 위)/[없음+데이터]/"개인연금"(1줄
        # 아래)/"(C)"(2줄 아래) - "없음"이 있다고 위/아래 확장을 아예 막아
        # 버리면 "수수료미"를 놓쳐 evidence의 클래스명이 "징구-..."로
        # 잘려 보인다). 그래서 확장 자체는 막지 않되, 그러다가 "이내"
        # (위쪽)/"납입금"(아래쪽)을 만나면 - 이 행 자신은 판매수수료가
        # 없다고 이미 확인됐으니 그건 무조건 다른(이웃) 클래스의 판매수수료
        # 문구 잔재다 - 포함하지 않고 그 자리에서 멈춘다.
        up_lines = list(base_up)
        found_napipgeum = any(_has_word(wl, "납입") for wl in up_lines)
        j = i - 2
        extra = 0
        while up_lines and j >= 0 and extra < MAX_EXTRA_LINES and not found_napipgeum:
            if (
                _is_full_data_row(lines[j])
                or _has_class_paren(lines[j])
                or _is_header_row(lines[j])
            ):
                break
            if own_row_no_commission and _has_word(lines[j], "이내"):
                break
            up_lines.insert(0, lines[j])
            found_napipgeum = _has_word(lines[j], "납입")
            extra += 1
            j -= 1

        down_lines = list(base_down)
        found_ianae = any(_has_word(wl, "이내") for wl in down_lines)
        stop_down = down_lines and _has_class_paren(down_lines[0])
        # 닫는 괄호가 있는 줄을 만나면 보통 "클래스명이 끝났다"는 뜻으로
        # 보고 거기서 멈추는데, 판매수수료 %가 그 닫는 괄호 줄보다도 더
        # 아래에 떨어져 나오는 문서가 있다(KR5185450009 실측: "...
        # 오프라인(A1)" 다음 줄에 "1.0%이내"가 옴 - "%"/"이내"가 있는데도
        # 그 앞줄에 이미 클래스 코드 괄호가 있다는 이유로 못 보고
        # 지나쳤다). 닫는 괄호를 봤어도 바로 다음 줄이 "이내"나 %값처럼
        # 보이면 아직 판매수수료 문구가 이어지는 것으로 보고 한 줄은 더
        # 열어준다.
        if stop_down and (i + 2) < len(lines):
            peek_text = "".join(w["text"] for w in lines[i + 2])
            if "이내" in peek_text or SALES_COMMISSION_PCT_RE.search(peek_text) or BUNUI_RE.search(peek_text):
                stop_down = False
        j = i + 2
        extra = 0
        while (
            down_lines and j < len(lines) and extra < MAX_EXTRA_LINES
            and not found_ianae and not stop_down
        ):
            if _is_full_data_row(lines[j]) or _is_header_row(lines[j]):
                break
            if own_row_no_commission and _has_word(lines[j], "납입"):
                break
            down_lines.append(lines[j])
            found_ianae = _has_word(lines[j], "이내")
            stop_down = _has_class_paren(lines[j])
            extra += 1
            j += 1

        # 클래스명/판매수수료 문구가 페이지 경계를 넘어가는 경우도 있다
        # (KR514X450008 Ae 실측: 데이터 줄 자체가 그 페이지의 마지막
        # 줄이라 "온라인형(Ae)"와 "0.5%이내"가 통째로 다음 페이지 첫
        # 줄로 넘어감). class_code 탐색은 이미 next_page_head_lines로
        # 이런 경우를 봐주고 있었지만(위 참고), evidence/판매수수료
        # 재구성 쪽은 이 페이지 안(`lines`)에서만 찾다 보니 이 행만
        # "클래스명: 수수료선취 –"처럼 끊긴 채로 남고 sales_commission_desc
        # 도 null이 됐다. 이 페이지 끝까지 갔는데도 아직 "이내"를 못
        # 찾았고(다른 클래스의 완전한 경계도 아직 안 만났다면) 다음
        # 페이지 머리글 후보를 이어서 본다.
        if (
            not found_ianae and not stop_down and not own_row_no_commission
            and extra < MAX_EXTRA_LINES and j >= len(lines) and next_page_head_lines
        ):
            for hl in next_page_head_lines:
                if extra >= MAX_EXTRA_LINES or _is_full_data_row(hl) or _is_header_row(hl):
                    break
                down_lines.append(hl)
                found_ianae = _has_word(hl, "이내")
                stop_down = _has_class_paren(hl)
                extra += 1
                if found_ianae or stop_down:
                    break

        # 표 왼쪽 여백(클래스명 칸보다도 왼쪽)에 세로로 찍힌 구간 캡션
        # ("투자비용" 등, x0≈27.8)이 y좌표가 데이터 행과 가까워 같은 줄로
        # 묶이는 경우가 있다. 클래스명 칸은 실측 사례들에서 전부 x0≈77.6
        # 부터 시작해서(수십 개 문서에서 일관됨) 이 캡션과는 확실히 구간이
        # 갈린다 - 처음엔 "그 줄 안에서 유독 멀리 떨어진 단어만" 걸렀는데,
        # 캡션이 클래스명 바로 옆(간격 13pt 정도)에 붙어 나오는 문서도 있어
        # (KR5153420105 실측) 그 조건으로는 놓치는 경우가 있었다. 그렇다고
        # 이 행 자신의 줄 최솟값을 기준으로 자르면(전에 시도) 그 줄에 클래스
        # 명이 없는 행(예: 대시만 있는 행)에서 다음 줄의 진짜 클래스명까지
        # 잘라내는 부작용이 있었다(C1 실측) - 그래서 문서 전체에서 일관되게
        # 관찰된 절대 좌표 기준(70pt)으로 고정한다.
        # 이 캡션의 x0가 문서마다 달라(대부분 27.8 근처인데 KR5185450009는
        # 74.4로 70pt 기준을 살짝 넘어서 안 걸러졌다 - "수수료선취-
        # 온라인(A-e) 투자비용"처럼 클래스명 뒤에 캡션이 그대로 붙어
        # 보였다) 절대좌표만으로는 모든 문서를 다 못 잡는다. "투자비용"은
        # 이 세로 캡션에서만 쓰는 고정 라벨이라(클래스명엔 나올 일이
        # 없음) 좌표와 무관하게 글자 자체로도 걸러낸다.
        def _strip_stray_caption(wl):
            return [w for w in wl if w["x0"] >= 70 and w["text"] != "투자비용"]

        window_lines = [_strip_stray_caption(wl) for wl in (up_lines + [line] + down_lines)]
        window_lines = [wl for wl in window_lines if wl]
        window_text = " ".join(" ".join(w["text"] for w in wl) for wl in window_lines)

        # evidence를 물리적 줄 순서 그대로 이어 붙이면 "클래스종류"/"판매수수료"가
        # 실제로는 서로 다른 칸(컬럼)인데도 마치 한 문장인 것처럼 뒤섞여 보인다
        # (사용자가 원본 표 캡처와 나란히 대조해서 지적함: "납입금 수수료선취-
        # 오프라인(A) 액의 1%"는 원본에서 "클래스종류" 칸과 "판매수수료" 칸이
        # 우연히 같은 y좌표 구간에 걸쳐 있어서 생기는 순서일 뿐, 실제로 섞여
        # 있는 게 아니다 - 그런데 그대로 이어 붙이면 마치 한 칸인 것처럼 보여서
        # 오해를 산다). 게다가 클래스명이 데이터 줄 앞/뒤로 쪼개지면 그 사이에
        # 낀 숫자들 때문에 두 조각이 evidence 안에서 뚝 떨어져 보인다. 그래서
        # 칸(클래스명 vs 판매수수료 vs 숫자데이터)별로 단어를 분리해 각각
        # 이어붙인 뒤, 칸 이름을 붙여 evidence를 구성한다 - 물리적 줄 순서가
        # 아니라 "논리적 칸" 순서로 보여준다.
        COMMISSION_MARKER_WORDS = {"이내", "없음"}
        COMMISSION_PCT_TOKEN_RE = re.compile(r"^[\d.]+%$")

        def _word_role(w):
            if w["x0"] >= total_fee["x0"]:
                return "data"
            # "운용전환일" 전/후로 수수료가 나뉘는 문서는 "구분" 칸(최초설정일부터/
            # 운용전환일부터/해지일까지 등)이 클래스명 칸과 같은 x좌표 구간에
            # 찍혀 있어서, 걸러내지 않으면 클래스명에 "최초설정일부 터
            # 운용전환일"처럼 구분 칸 문구가 섞여 들어간다(KR5147430065 실측,
            # 사용자 지적).
            if PERIOD_COLUMN_WORD_RE.search(w["text"]) or w["text"] in PERIOD_COLUMN_LONE_WORDS:
                return "period"
            # "납입금"/"납입금액"처럼 뒤에 "액"이 붙거나 안 붙거나 하는
            # 표기가 문서마다 달라서(KR5123490017 실측: "납입금액"이 한
            # 토큰) 부분 일치로 잡는다. "납입금"조차 "납입"/"금"으로 다시
            # 쪼개져 나오는 문서도 있어(KR5185450009 실측: "납입"이 데이터
            # 줄 2줄 위에 단독으로 떨어져 있는데, 이 2글자만으론 "납입금"
            # 부분일치에도 안 걸려 기본값(class_name)으로 샜다) "납입"까지만
            # 봐도 충분히 특정된다(클래스명에 이 글자가 나올 일이 없음).
            # "액의"/"금액의"처럼 "...의"로 끝나는 조각도 마찬가지로
            # "납입금액의"가 쪼개진 조각이라 접미사로 잡는다(클래스명은 이런
            # 조사로 끝나지 않아 오탐 위험이 낮다).
            if "납입" in w["text"] or w["text"].endswith("의"):
                return "commission"
            if w["text"] in COMMISSION_MARKER_WORDS or w["text"].endswith("이내"):
                return "commission"
            # 글자를 한 자씩 따로 찍는 서식은 "이내"조차 "이"/"내" 두 단어로
            # 쪼개져 나온다(KR5114420016 S클래스 실측: "0.15%이내"의 "이"/
            # "내"가 따로 떨어진 채 x0가 클래스명 칸 쪽이라 기본값(class_name)
            # 으로 새서 evidence에 "수수료후취-온라인슈퍼(S) 이 내"처럼 붙어
            # 보였다). 클래스명에 "이"/"내" 한 글자짜리 단독 토큰이 나올 일은
            # 없어 안전하게 판매수수료 쪽으로 본다.
            if w["text"] in ("이", "내"):
                return "commission"
            if COMMISSION_PCT_TOKEN_RE.match(w["text"]) or "%" in w["text"]:
                # "0.30%이내"처럼 %값과 "이내"가 공백 없이 한 토큰으로 붙어
                # 나오는 문서가 있다(KR5127420083 실측) - 클래스명 글자에는
                # "%"가 나올 일이 없어 "%"가 있으면 무조건 판매수수료 쪽으로
                # 본다.
                return "commission"
            if w["text"] == "-":
                return "commission"
            if w["text"].endswith("분의") or re.fullmatch(r"[\d.]+", w["text"]):
                return "commission"
            return "class_name"

        class_name_words = []
        commission_words = []
        for wl in window_lines:
            # "환매금액의 N%이내"류 문구가 있는 줄은 그 자체가 판매수수료
            # 문구라 줄 전체를 판매수수료 쪽으로 묶는다 - 그래야 "3년/미/
            # 만/환/매/시" 같은 낱글자가 기본값(class_name)으로 새서
            # evidence 클래스명에 섞이는 걸 막는다(위 REDEMPTION_NOTE_RE
            # 주석 참고).
            note_line = _is_note_row(wl)
            for w in wl:
                role = "commission" if note_line else _word_role(w)
                if role == "class_name":
                    class_name_words.append(w["text"])
                elif role == "commission":
                    commission_words.append(w["text"])
        class_name_full = " ".join(class_name_words) if class_name_words else None
        commission_raw = " ".join(commission_words) if commission_words else None
        # 클래스명/판매수수료만 남기고 총보수 등 숫자 데이터를 통째로 빼버리면
        # (원래는 실수로 빠졌었다 - 사용자가 "판매수수료만 보이는거야?"라고
        # 바로 지적함) total_fee/distribution_fee/peer_avg_fee/
        # total_fee_and_cost/cost_projection_per_10m을 원본과 대조 확인할
        # 방법이 없어진다. 이 행 자신의 줄(line)에서 "숫자데이터"로 분류된
        # 토큰만(클래스명/판매수수료 단어는 이미 위에서 따로 보여주므로
        # 여기서 또 반복하지 않는다) 순서대로 남긴다.
        data_text = " ".join(w["text"] for w in line if _word_role(w) == "data")

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

        # 클래스 코드(괄호 안 텍스트)는 실측 사례들에서 항상 "이 줄" 또는 "다음 줄들"
        # 에서만 나타났다 - "이전 줄"은 위쪽 행(다른 클래스)의 이름 꼬리일 수 있어서
        # 잘못 가져다 쓸 위험이 있다 (KR514X450008에서 확인: 이전 줄의 클래스코드를
        # 엉뚱하게 가져와서 실제로는 다른 클래스인 행에 잘못 붙인 사례). 그래서
        # 클래스 코드는 이 줄 기준 아래쪽으로만 찾고, 이전 줄은 보지 않는다.
        #
        # 닫는 괄호가 데이터 줄 바로 다음 줄이 아니라 그보다 더 아래(클래스명이
        # 3줄 이상으로 나뉘는 경우 - 예: "오프라인- 없음 [데이터]" / "개인연금" /
        # "(C)", KR5113420012 실측)에 있는 경우가 있어서, 다른 클래스의 완전한
        # 데이터 행(경계)을 만나기 전까지는 몇 줄 더 내려가며 찾는다. 또한 일부
        # 문서는 글자 간격이 벌어진 폰트 때문에 괄호 안까지 공백이 끼어 나온다
        # ("( C- P)") - 정규식이 원래 공백을 허용 안 하므로, 못 찾으면 공백을
        # 지운 텍스트로 한 번 더 시도한다.
        def _try_class_code(text):
            m = CLASS_CODE_RE.search(text)
            if m:
                return m.group(1)
            # 공백을 지운 뒤 다시 찾아보는데, 판매수수료 칸 글자("없음")가
            # 클래스명 조각과 닫는 괄호 사이에 끼어 있으면 공백만 지웠을 때
            # 오히려 그 글자와 들러붙어버려 방해가 된다(KR5123420015 실측:
            # "-온라인(C- 없음 e)"를 그냥 공백만 지우면 "...C-없음e)"가 돼
            # 안 걸림) - "없음"을 먼저 빼고 공백을 지운다.
            cleaned = re.sub(r"\s+", "", text.replace("없음", " "))
            m = CLASS_CODE_RE.search(cleaned)
            if m:
                return m.group(1)
            # "(Cp(퇴직연금))"처럼 코드 뒤에 괄호가 중첩되는 경우(위
            # CLASS_CODE_NESTED_RE 참고).
            m = CLASS_CODE_NESTED_RE.search(cleaned)
            return m.group(1) if m else None

        # 여는 괄호와 닫는 괄호가 서로 다른 줄에 떨어져 있는 경우도 있다
        # (KR5123420039 실측: "(C-"가 데이터 줄+1, "e)"가 데이터 줄+2) -
        # 한 줄씩 따로따로만 보면 못 잡으므로, 줄을 누적해가며 본다(줄 하나
        # 추가할 때마다 매번 다시 시도).
        class_code = None
        accumulated = class_part1
        j = i + 1
        steps = 0
        while class_code is None and j < len(lines) and steps < 4:
            if _is_full_data_row(lines[j]):
                break
            accumulated += " " + " ".join(w["text"] for w in lines[j])
            class_code = _try_class_code(accumulated)
            j += 1
            steps += 1
        if class_code is None:
            # 데이터 줄 자신에서도(다음 줄이 전혀 없을 때) 시도해둔다.
            class_code = _try_class_code(class_part1)
        if class_code is None and next_page_head_lines:
            # 이 페이지 안에서 못 찾았으면, 표가 다음 페이지로 이어지면서
            # 클래스명의 닫는 괄호 조각이 다음 페이지 맨 앞줄로 넘어간 경우도
            # 확인한다(KR514X450008 실측: "온라인형(Ae)"가 다음 페이지 첫
            # 줄에 있어서, 이 페이지 안의 다음 줄(무관한 페이지 푸터)만 보면
            # 놓쳤다).
            next_page_text = " ".join(w["text"] for wl in next_page_head_lines for w in wl)
            class_code = _try_class_code(class_part1 + " " + next_page_text)
        if class_code is None:
            # "A(수수료선취-오프라인)"처럼 클래스 코드가 괄호 안이 아니라
            # 괄호 바로 앞에 붙는 문서도 있다(KR5125450023/KR5125450070
            # 실측 - 괄호 안은 클래스 코드가 아니라 상품유형 설명임). 보통은
            # 이 행 자신의 클래스명 첫 토큰에서 찾는데, 이 행 자신의 줄에는
            # 숫자와 판매수수료 칸의 "없음"만 있고(클래스명 첫 토큰 자리가
            # 아님) 클래스명 전체가 바로 윗줄에 있는 문서도 있다(같은 두
            # 문서에서 판매수수료가 "없음"인 클래스들 실측: 데이터 줄엔
            # "없음"+숫자뿐, "C(수수료미징구"는 바로 위 줄 전체). "없음"은
            # 클래스명이 아니므로 pre_text_words 첫 단어가 있어도 그게
            # "없음"이면 윗줄도 같이 후보로 본다 - 윗줄이 이미 다른
            # 클래스의 완전한 데이터 행이면(경계) 후보에서 뺀다.
            prefix_candidates = []
            if pre_text_words and pre_text_words[0]["text"] != "없음":
                prefix_candidates.append(pre_text_words[0]["text"])
            if i - 1 >= 0 and lines[i - 1] and not _is_full_data_row(lines[i - 1]):
                prefix_candidates.append(lines[i - 1][0]["text"])
            for cand in prefix_candidates:
                m3 = CLASS_CODE_PREFIX_RE.match(cand)
                if m3:
                    class_code = m3.group(1)
                    break

        if class_code is None:
            # 괄호로 감싼 클래스 코드 자체가 원본에 없는 문서도 있다
            # (KR5123365001 실측: 클래스가 애초에 하나뿐이라 "(A)" 같은
            # 코드 없이 "투자신탁"이라는 라벨 하나만 있음). 이럴 땐 코드를
            # 추측해서 지어내는 대신, 이 행 자신의 줄에 실제로 적힌 라벨
            # 글자(판매수수료 칸의 "없음"은 제외)를 그대로 클래스 이름으로
            # 쓴다 - 없는 값을 만들어내는 것보다 원본에 있는 걸 그대로
            # 옮기는 쪽이 "틀린 값 < 없는 값" 원칙에 맞다.
            label_words = [w["text"] for w in pre_text_words if w["text"] != "없음"]
            if label_words:
                class_code = " ".join(label_words)

        # "납입금액의"가 3줄로 쪼개지는 경우도 있다("납입금" / 데이터 줄에 낀
        # "액의 1%" / "이내" - 사이에 클래스명 등 다른 텍스트가 끼어 있어서
        # "납입금액의"를 하나의 이어붙은 문자열로 찾으면 놓친다). "납입금"이라는
        # 조각만으로도 판매수수료 문구라는 걸 충분히 특정할 수 있어 그걸로 판별한다.
        sales_commission_desc = None
        # 글자를 한 자씩 따로 찍는 서식이 있는 문서는 "100분의 0.15"의
        # "100"조차 "10"/"0분의"처럼 서로 다른 단어로 쪼개져 나와(공백이
        # 그 사이에 끼어) 위 두 정규식이 이어붙인 텍스트에서도 못 찾는다
        # (KR5114420027 Ae/S 실측: 판매수수료가 실제로 있는데도
        # sales_commission_desc가 null로 나왔다). 공백을 다 지운 버전으로
        # 한 번 더 시도한다.
        wide_text_nospace = wide_text.replace(" ", "")
        pct_m = SALES_COMMISSION_PCT_RE.search(wide_text) or SALES_COMMISSION_PCT_RE.search(wide_text_nospace)
        bunui_m = BUNUI_RE.search(wide_text) or BUNUI_RE.search(wide_text_nospace)
        # "후취"(환매 시점에 떼는) 클래스는 기준이 "납입금액"이 아니라
        # "환매금액"이다(위 REDEMPTION_NOTE_RE 주석 참고) - 원본 문구 그대로
        # "환매금액의 N%이내"로 남기고, 그 외(선취/일반)는 기존대로
        # "납입금액의 N%이내"로 남긴다.
        # "납입금액"이 "납입"/"금액의"로 쪼개져 나오는 문서도 있다(위
        # _word_role의 "납입" 주석 참고, KR5185450009 실측) - "납입금"
        # 대신 "납입"까지만 봐야 그런 경우도 잡힌다.
        commission_basis = (
            "환매금액"
            if "환매금액" in wide_text or "환매금액" in wide_text_nospace
            else ("납입금액" if "납입" in wide_text else None)
        )
        # "환매금액"을 기준으로 쓰는 후취형은 거의 항상 "OO년 미만 환매시"
        # 조건이 같이 붙어 있다(위 REDEMPTION_CONDITION_RE 주석 참고) -
        # 조건 없이 "환매금액의 N%이내"만 남기면 무조건 떼는 수수료처럼
        # 읽혀서 뜻이 달라지므로, 찾아지면 원본 그대로 앞에 붙인다.
        condition_prefix = ""
        if commission_basis == "환매금액":
            cond_m = REDEMPTION_CONDITION_RE.search(wide_text) or REDEMPTION_CONDITION_RE.search(wide_text_nospace)
            if cond_m:
                condition_prefix = f"{cond_m.group(1)}년 미만 환매시: "
        if commission_basis and pct_m:
            sales_commission_desc = f"{condition_prefix}{commission_basis}의 {pct_m.group(1)}%이내"
        elif commission_basis and bunui_m:
            # "N%" 대신 "100분의 N"(=N/100, 같은 뜻)으로 쓰는 문서가 있다
            # (KR5114420027). 위에서 이 값을 이미 총보수 등 실제 컬럼과
            # 분리해뒀으니, 여기서는 같은 뜻인 "%" 표기로 통일해서 남긴다.
            sales_commission_desc = f"{condition_prefix}{commission_basis}의 {bunui_m.group(1)}%이내"
        elif "없음" in window_text or has_standalone_dash:
            # 원본이 "없음"이라는 글자를 쓰든 그냥 "-"만 찍든 의미는 같아서
            # ("판매수수료가 없다"는 확인된 사실), 출력은 원본에 실제로 보이는
            # 기호인 "-"로 통일한다(사용자 요청).
            sales_commission_desc = "-"

        if isinstance(peer_avg_fee, str):
            peer_avg_fee_text = peer_avg_fee
        elif peer_avg_fee:
            peer_avg_fee_text = peer_avg_fee["text"].rstrip("%")
        else:
            peer_avg_fee_text = None

        # evidence는 "클래스명"을 물리적 줄 순서가 아니라 논리적 칸 이름을
        # 붙여 따로 보여주고, 그 뒤에 판매수수료 문구 + 숫자데이터를 이어
        # 붙인다(사용자 요청 - "판매수수료" 이름표 자체는 빼고, 클래스명/
        # 판매수수료 원문("수수료선취-오프라인(A) 액의 1%")이 숫자 앞에
        # 또 반복되지 않게). sales_commission_desc가 이미 정규화됐으면 그걸
        # 쓰고, 못 찾았으면(null) 원본에서 실제로 걸린 원문 조각
        # (commission_raw)을 대신 보여줘 왜 못 찾았는지 확인할 수 있게
        # 한다. 숫자데이터(data_text)는 total_fee/distribution_fee/
        # peer_avg_fee/total_fee_and_cost/cost_projection_per_10m을 원본과
        # 대조 확인하는 용도다.
        commission_display = sales_commission_desc if sales_commission_desc is not None else (commission_raw or "(확인안됨)")
        evidence = f"클래스명: {class_name_full or '(확인안됨)'} | {commission_display} {data_text}".rstrip()

        rows.append({
            "class_code": class_code,
            "sales_commission_desc": sales_commission_desc,
            "total_fee": total_fee["text"].rstrip("%"),
            "distribution_fee": distribution_fee["text"].rstrip("%"),
            "peer_avg_fee": peer_avg_fee_text,
            "total_fee_and_cost": total_fee_and_cost["text"].rstrip("%") if total_fee_and_cost else None,
            "cost_projection_per_10m": cost_projection,
            # "운용전환일" 전/후로 수수료가 바뀌는 문서에서만 이 키들이
            # 붙는다(아래 참고, KR5147430065) - 그 외 문서(대다수)는 애초에
            # 키 자체가 없다(null로 채운 빈 필드를 모든 행에 다 넣으면
            # 대부분 안 쓰는 필드로 보기 불편하다는 지적을 받아, 해당되는
            # 행에만 조건부로 붙이도록 바꿨다). total_fee 등 위쪽 필드는
            # 전환 "전"(현재 적용 중인) 값이고, *_after_conversion은 전환
            # 이후 예정된 값이다.
            "page": page_num,
            "evidence": evidence,
            "method": "coordinate_reconstruction",
            # 주의: 이 confidence는 "이 행의 모든 필드가 다 맞다"는 뜻이
            # 아니다 - "class_code(클래스 이름표)를 다른 클래스와 헷갈릴
            # 위험 없이 찾았는가"만 본다(class_code를 못 찾으면 어느
            # 클래스 것인지조차 불확실하니 0.5로, 찾았으면 1.0으로). 사용자
            # 지적대로 "다 제대로 뽑아야 1이어야 하는 거 아니냐"는 게 맞는
            # 말이지만, total_fee/판매수수료/클래스명 표기처럼 서로 다른
            # 이유로 틀릴 수 있는 필드들을 하나의 숫자로 합칠 근거가 없어
            # (이번 세션에서 고친 버그들 - sales_commission_desc null,
            # 인접 클래스명 섞임 등 - 이 전부 class_code는 처음부터 1.0
            # 이었던 행에서 나왔다는 게 그 증거) 이 좁은 의미로 한정해서
            # 쓴다. "행 전체가 실제로 맞는지"는 confidence가 아니라
            # extract_class_fees.py 실행 후 매번 돌리는 전수 이상치 검사
            # (1y>500/1y<10/total_fee>10/distribution_fee>total_fee/
            # total_fee_and_cost<total_fee, class_code 중복 등 - README
            # 참고)가 실질적으로 그 역할을 한다.
            "confidence": 1.0 if class_code else 0.5,
            "_row_line_idx": i,
        })

    # "판매수수료" 칸이 "없음" 글자 하나를 여러 클래스 행에 걸쳐 세로로
    # 병합해서 공유하는 문서가 있다(KR5194450018 실측 - 화면 캡처로 직접
    # 확인: C1/C-e/W/F 4개 클래스, RP/RP-e/S-P/CP/CP-e 5개 클래스가 각각
    # "없음" 하나씩을 그룹 세로 중앙에 공유). 개별 행 위/아래 몇 줄만
    # 보는 기존 로직은 그 그룹의 가운데 근처 행(C-e/S-P처럼 "없음"과
    # 가까운 행)만 우연히 맞고, 그룹 양 끝 행(C1/CP-e, RP)은 놓쳐서
    # sales_commission_desc가 null로 남았다(사용자가 "제발 제대로
    # 해줘"라고 지적해서 화면 캡처까지 받아 직접 확인함 - class_returns의
    # 병합 셀 설정일 처리와 같은 종류의 문제). 아직 못 찾은 행에 대해,
    # 그 행과 "없음" 토큰 사이에 이미 다른 진짜 문구("납입금액의..."/
    # "환매금액의..." 등 - "-"로 확정된 행은 같은 그룹일 수 있어 경계로
    # 안 본다)로 확정된 다른 행이 끼어있지 않은 가장 가까운 "없음"을
    # 찾아 같은 병합 셀로 보고 채운다.
    #
    # 다만 이 판정은 "중간에 진짜 문구가 없다"만 보기 때문에, 표에 아무
    # 마커도 안 남기고 진짜로 비어있는 행(우리가 아직 본 적 없는 케이스)이
    # 끼어 있으면 엉뚱하게 먼 "없음"을 끌어다 붙일 위험이 있다. 실측
    # 병합 그룹(KR5194450018)의 최대 거리가 6줄이었던 것에 근거해, 그보다
    # 뚜렷이 먼 "없음"은 같은 병합 셀이라 확신할 수 없다고 보고 채우지
    # 않는다 - 틀린 "-"보다 null로 남겨 이상치 검사에 걸리게 하는 게 낫다
    # ("틀린 값은 없는 값보다 나쁘다").
    # "없음" 판정을 줄 안에 그 글자가 있는지만으로 하면, 표가 아니라
    # 근처의 다른 문장(각주/설명 문구 등)에 우연히 등장한 "없음"까지
    # 병합 셀로 착각할 위험이 있다(사용자 지적). 판매수수료 칸 자체에서
    # 이미 직접 "없음"이 잡힌 행(예: 이 페이지의 C-e처럼 병합 그룹
    # 가운데라 기존 로직으로도 맞은 행)이 있으면 그 x좌표를 이 칸의
    # 실제 위치로 보고, 후보 "없음"도 그 x좌표 근처에 있는 것만
    # 인정한다(class_returns의 최초설정일 병합 셀 판정과 같은 방식 -
    # 표 밖 문장은 x좌표가 이 칸과 다를 수밖에 없어 걸러진다). 이 페이지에
    # 그런 직접-매치 행이 아예 없으면(앵커를 못 구하면) 판정 불가로 보고
    # 안전하게 x좌표 필터 없이 기존 방식(거리 상한만 적용)으로 대체한다.
    MAX_MERGED_CELL_DISTANCE = 8
    unresolved = [r for r in rows if r.get("sales_commission_desc") is None]
    if unresolved:
        row_line_idxs = {r["_row_line_idx"] for r in rows}
        commission_col_x0 = None
        for idx in row_line_idxs:
            for w in lines[idx]:
                if w["text"] == "없음":
                    commission_col_x0 = w["x0"]
                    break
            if commission_col_x0 is not None:
                break

        def _none_word_x0(l):
            return next((w["x0"] for w in l if w["text"] == "없음"), None)

        none_word_lines = [
            idx for idx, l in enumerate(lines)
            if (x0 := _none_word_x0(l)) is not None
            and (commission_col_x0 is None or abs(x0 - commission_col_x0) < 15)
        ]
        real_phrase_positions = [
            r["_row_line_idx"] for r in rows
            if r.get("sales_commission_desc") not in (None, "-")
        ]
        for r in unresolved:
            ri = r["_row_line_idx"]
            best = None
            for ni in none_word_lines:
                if abs(ni - ri) > MAX_MERGED_CELL_DISTANCE:
                    continue
                lo, hi = min(ri, ni), max(ri, ni)
                if any(lo < p < hi for p in real_phrase_positions):
                    continue
                if best is None or abs(ni - ri) < abs(best - ri):
                    best = ni
            if best is not None:
                r["sales_commission_desc"] = "-"

    for r in rows:
        r.pop("_row_line_idx", None)
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


def conversion_trigger_nav_price(doc_id):
    """total_fee_after_conversion 등이 채워진 행이 있는 문서에서, "운용전환일"이
    고정 날짜가 아니라 이 펀드 자신의 기준가격이 특정 값 이상 오르면 발생하는
    조건부 전환인 경우(목표전환형 펀드), 그 목표 기준가격(원) 숫자만 뽑는다.
    문장으로 풀어 쓰지 않는 이유: 이 파일의 다른 모든 필드는 원본에서 그대로
    뽑은 값이지 해석문이 아니다 - 숫자만 남기고 의미(목표가 도달 시 전환)는
    필드 이름과 README에 문서화한다. 못 찾으면(고정 날짜인 일반적인 경우
    - 이 필드 자체가 만들어지는 문서는 지금 KR5147430065 하나뿐) None."""
    fp = os.path.join(EXTRACTED_DIR, f"{doc_id}_text.json")
    if not os.path.exists(fp):
        return None
    with open(fp, "r", encoding="utf-8") as f:
        pages = json.load(f)
    full_text = " ".join(p.get("text", "") for p in pages)
    m = CONVERSION_TRIGGER_RE.search(full_text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _cluster_header_labels(lines, header_end_idx, x_tol=8):
    """header_end_idx 위쪽 최대 12줄에서 x좌표로 헤더 라벨 텍스트를
    재구성한다(소수 값 토큰은 제외) - "나.집합투자기구에 부과되는 보수"류
    상세표는 헤더가 여러 줄에 걸쳐 한 글자씩 쌓이는 서식이 많다.

    묶는 기준은 x0가 아니라 글자 뭉치의 가운데(center)다. 헤더 칸 이름은
    가운데 정렬이라 조각마다 글자 수가 다르면 x0가 어긋난다
    (KR510902511M 28페이지 실측: "집합"@141.1 / "투자업자"@131.1 /
    "보수"@141.1 - x0로 묶으면 10pt 차이라 "투자업자"가 떨어져 나가
    라벨이 "집합"으로 잘렸다. 가운데로 맞추면 세 조각이 하나로 묶여
    "집합투자업자보수"가 된다). 칸 간격은 실측상 45pt 안팎이라 이
    허용오차로 옆 칸과 섞일 위험은 없다. 돌려주는 x는 그 칸의 왼쪽
    끝(x0 최솟값)으로, 값 토큰의 x0와 비교하는 호출부와 기준을 맞춘다."""
    header_lines = lines[max(0, header_end_idx - 12) : header_end_idx + 1]
    clusters = []
    for row_i, line in enumerate(header_lines):
        for w in line:
            if DECIMAL_RE.match(w["text"]):
                continue
            center = (w["x0"] + w["x1"]) / 2
            placed = False
            for c in clusters:
                if abs(c["center"] - center) <= x_tol:
                    c["pieces"].append((row_i, w["text"]))
                    c["x0"] = min(c["x0"], w["x0"])
                    placed = True
                    break
            if not placed:
                clusters.append({"center": center, "x0": w["x0"], "pieces": [(row_i, w["text"])]})
    labels = []
    for c in clusters:
        pieces = sorted(c["pieces"])
        labels.append((c["x0"], "".join(t for _, t in pieces)))
    return sorted(labels)


DETAIL_FEE_DASH_RE = re.compile(r"^-$")
# "(C)"처럼 괄호로 안 떨어지고 "종류C"/"종류C-F"로만 나오는 라벨 서식
# (KR510902773M 실측). 뒤에 다른 한글이 바로 안 붙게(예: "종류형") 경계를
# 둔다 - class_returns.py의 CLASS_CODE_JONGRYU_RE와 같은 취지.
DETAIL_FEE_CLASS_CODE_JONGRYU_RE = re.compile(r"종류([A-Za-z][A-Za-z0-9\-]{0,6})(?![A-Za-z0-9\-])")
# 헤더 여러 줄이 데이터 행과 가까워서(표마다 헤더 줄 수가 달라 정확한
# 경계를 못 잡음) 클래스 코드 정규식에 걸리는 흔한 금융 약어들
# (KR5172450019 실측: 헤더의 "(TER)"이 A클래스 행의 코드로 잘못 붙어서
# 이후 모든 클래스가 한 칸씩 밀림). 실제 클래스 코드로 이 값들이 나올
# 일은 없다고 봐도 안전하다.
DETAIL_FEE_CODE_BLOCKLIST = {"TER", "CDSC", "IRP", "Class", "Wrap", "Cost"}


def _detail_fee_labels_by_column(lines, first_data_idx, col_x0s):
    """상세표의 각 컬럼(값 x좌표) 위에 있는 헤더 글자들을 모아 칸 이름을
    복원한다. fee_breakdown 항목 이름으로 쓰이므로 "집합"처럼 잘리거나
    옆 칸 이름이 붙으면 안 된다(잘린 이름은 못 쓰고, 틀린 이름은 더 나쁘다).

    처음엔 페이지 전체 헤더를 x좌표로 한 번에 클러스터링하고 컬럼마다
    "가장 가까운 라벨"을 붙였는데, 두 가지로 깨졌다(KR510902511M 28페이지
    실측):
      - 문서 제목("미래에셋장기성장포커스...")이나 여러 칸을 아우르는
        묶음 헤더("지급비용(연간%)")가 가로로 넓어 개별 칸 글자와 같은
        클러스터로 묶였다 - 그 바람에 "집합투자업자보수"가 제목 클러스터에
        흡수되고, 0.72(집합투자업자보수)에 옆 칸 이름 "판매회사보수"가
        붙는 밀림이 생겼다.
      - "총 보수"처럼 한 줄에 띄어 쓴 칸 이름은 세로로 쌓이지 않아 하나로
        안 묶였다.
    그래서 전역 클러스터링 대신 컬럼마다 "이 칸 위에 실제로 겹쳐 있는
    글자"만 모은다. 칸 하나를 넘어 퍼지는 토큰(칸 간격의 1.5배 초과)은
    묶음 헤더/제목으로 보고 제외한다."""
    if len(col_x0s) < 2:
        return [None] * len(col_x0s)
    spacing = min(b - a for a, b in zip(col_x0s, col_x0s[1:]))
    max_width = spacing * 1.5
    # 헤더 영역의 위 경계: 표 제목("나.집합투자기구에 부과되는 보수 및
    # 비용")보다 위로는 안 올라간다. 제목 글자도 폭이 좁아 폭 필터에 안
    # 걸리고 컬럼 위에 겹쳐서, 안 자르면 칸 이름이 "부과되는보수집합투자
    # 업자보수"처럼 제목 조각을 달고 나온다(KR5122420005/KR5172450019 실측).
    start = max(0, first_data_idx - 14)
    for j in range(first_data_idx - 1, start - 1, -1):
        if "부과되는" in "".join(w["text"] for w in lines[j]):
            start = j + 1
            break
    header_lines = lines[start:first_data_idx]

    out = []
    for cx in col_x0s:
        # 칸 이름은 값 위에 가운데 정렬로 찍히므로, 값의 가운데를 기준으로
        # 칸 간격의 절반 안에 "글자 뭉치의 가운데"가 들어올 때만 이 칸의
        # 이름으로 본다(겹침만 보면 옆 칸 이름까지 딸려온다 - 실측으로
        # "집합판매회사투자업자보수"처럼 두 칸 이름이 섞였다). 값 폭은
        # "0.72"류 4~6글자라 10pt를 더해 가운데로 잡는다.
        center = cx + 10
        lo, hi = center - spacing * 0.5, center + spacing * 0.5
        pieces = []
        for row_i, line in enumerate(header_lines):
            for w in line:
                if DECIMAL_RE.match(w["text"]):
                    continue
                if (w["x1"] - w["x0"]) > max_width:
                    continue  # 묶음 헤더("지급비용(연간%)")/문서 제목
                # 값 칸 전체를 아우르는 단위·범위 표기("지급비율",
                # "지급비용", "(연간,%)")는 특정 칸의 이름이 아니다 -
                # 폭이 좁아 위 필터를 통과하므로 글자로도 걸러낸다.
                if w["text"].startswith("지급") or "연간" in w["text"]:
                    continue
                wc = (w["x0"] + w["x1"]) / 2
                if lo <= wc <= hi:
                    pieces.append((row_i, w["x0"], w["text"]))
        text = "".join(t for _, _, t in sorted(pieces))
        out.append(text or None)
    return out


def _find_detail_fee_data_rows(pdf):
    """"나.집합투자기구에 부과되는 보수 및 비용"류 상세표 - 캡션 문구가
    문서마다 달라서(README 참고: "투자실적" 같은 고정 캡션이 없음) 캡션
    대신 데이터 행 자체의 모양으로 찾는다: 순수 소수(%아님, 정수 비용예시도
    아님) + "-"(원본이 명시적으로 비워둔 칸)를 합쳐 7개 이상 한 줄에 있으면
    이 표의 데이터 행으로 본다(앞쪽 요약표는 소수 3~4개 + 정수 비용예시라
    이 조건에 안 걸림). 소수만으로 세면, 컬럼 대부분이 "-"인 클래스(직판/
    기관형 등 부가서비스가 거의 없는 클래스 - KR510902773M의 C-F 실측:
    소수 6개뿐이라 7개 기준에 아예 안 걸려서 데이터 행 취급을 못 받았다)를
    통째로 놓친다 - "-"도 원본이 실제로 채워 넣은 값(칸이 빈 게 아니라
    명시적으로 "없음"이라고 표시한 것)이므로 같이 센다. 페이지별로
    (page_num, 그 페이지의 lines, 데이터 행 인덱스 목록)을 돌려준다."""
    results = []
    for i, page in enumerate(pdf.pages):
        words = page.extract_words(x_tolerance=2, keep_blank_chars=False)
        lines = cluster_lines(words)
        data_idx = []
        for j, line in enumerate(lines):
            decimals = [w for w in line if DECIMAL_RE.match(w["text"]) and "%" not in w["text"]]
            dashes = [w for w in line if DETAIL_FEE_DASH_RE.match(w["text"])]
            if len(decimals) + len(dashes) >= 7:
                data_idx.append(j)
        if data_idx:
            results.append((i + 1, lines, data_idx))
    return results


def _detail_fee_row_class_code(lines, row_idx, consumed=None, header_idx=None):
    """데이터 행 자신 또는 앞/뒤 몇 줄에서 클래스 코드를 찾는다(클래스명이
    데이터 행 앞/뒤로 걸쳐 있는 서식이 많음 - class_returns.py에서 이미
    검증된 것과 같은 패턴). consumed: 이미 앞선(위쪽) 행의 라벨로 확정돼
    "소비"된 줄 번호 집합 - 한 클래스의 라벨이 "시작(앞) / 데이터 / 끝(뒤)"
    구조로 자기 데이터 행을 감싸는 서식에서, 다음 클래스가 위로 훑을 때
    바로 이전 클래스의 "끝" 조각을 자기 라벨로 잘못 주워가는 걸 막는다
    (KR5122420005 실측: A-E 행이 바로 위의 "형(A)"(A 자신의 끝 라벨
    조각)를 A-E의 라벨로 착각해 코드가 "A"로 잘못 나옴 - consumed로
    한 번 쓰인 줄은 다음 행 탐색에서 제외해야 고쳐짐). header_idx: 이
    줄보다 위로는 절대 안 넘어간다 - 첫 번째 데이터 행이 헤더 바로
    다음이면, 헤더 자체에 있는 "(TER)"(Total Expense Ratio 약자) 같은
    괄호 문구를 클래스 코드로 잘못 주워서 전체 클래스가 한 칸씩 밀리는
    사고가 났었다(KR5172450019 실측 - A행이 "TER"로 잘못 라벨링되면서
    A/Ae/C1/... 전체 값이 한 행씩 밀려 대응됨). 호출부는 데이터 행을
    위→아래 순서로 처리해야 한다."""
    if consumed is None:
        consumed = set()
    floor = header_idx if header_idx is not None else -1
    line = lines[row_idx]
    decimals = [w for w in line if DECIMAL_RE.match(w["text"]) and "%" not in w["text"]]
    if not decimals:
        return None, None
    pre_text = "".join(w["text"] for w in line if w["x0"] < decimals[0]["x0"])
    # 다른 클래스의 데이터 행(소수 3개 이상)이나 이미 소비된 줄, 헤더 위
    # 영역을 만나면 그 전에서 멈춰 옆 클래스 라벨(또는 헤더 문구)을 잘못
    # 가져오지 않게 한다. prev는 가장 가까운(마지막) 매치, next는 가장
    # 가까운(첫) 매치만 쓴다.
    prev_idx = []
    for k in range(row_idx - 1, max(row_idx - 4, floor), -1):
        if k in consumed or sum(1 for w in lines[k] if DECIMAL_RE.match(w["text"])) >= 3:
            break
        prev_idx.insert(0, k)
    next_idx = []
    for k in range(row_idx + 1, min(row_idx + 4, len(lines))):
        if sum(1 for w in lines[k] if DECIMAL_RE.match(w["text"])) >= 3:
            break
        next_idx.append(k)
    prev_text = "".join("".join(w["text"] for w in lines[k]) for k in prev_idx)
    next_text = "".join("".join(w["text"] for w in lines[k]) for k in next_idx)

    def _nearest_match(regex, text, want_last):
        matches = [m for m in regex.finditer(text) if m.group(1) not in DETAIL_FEE_CODE_BLOCKLIST]
        if not matches:
            return None
        return (matches[-1] if want_last else matches[0]).group(1)

    for regex in (CLASS_CODE_RE, DETAIL_FEE_CLASS_CODE_JONGRYU_RE):
        code = _nearest_match(regex, pre_text, want_last=True)
        if code:
            return code, pre_text
        code = _nearest_match(regex, prev_text, want_last=True)
        if code:
            consumed.update(prev_idx)
            return code, prev_text
        code = _nearest_match(regex, next_text, want_last=False)
        if code:
            consumed.update(next_idx)
            return code, next_text
    return None, prev_text + pre_text + next_text


def _detail_fee_grids(pdf):
    """"나.집합투자기구에 부과되는 보수 및 비용"류 상세표를 셀 격자로
    읽는다. 좌표 방식(줄 묶기 + x 근접 매칭)은 이 표에서도 같은 문제를
    겪었다 - 클래스명이 데이터 행 위/아래로 쪼개져 옆 행 것을 주워오거나
    (consumed 추적 필요), 헤더가 여러 줄로 쌓여 라벨이 잘리거나
    ("집합" 하나만 남음), 값이 "-"라 빠진 칸 때문에 열이 밀렸다.
    셀 경계를 쓰면 이 보정들이 전부 필요 없어진다.

    돌려주는 것: (page_num, header_rows, data_rows, col_x0s)
      - col_x0s: 이 표의 열 왼쪽 x좌표(정렬)
      - data_rows: [{"label": 맨왼쪽칸 텍스트, "cells": {열번호: 텍스트}}]
        (숫자 칸이 5개 이상인 행만 - 헤더/각주 행 제외)
    """
    results = []
    for i, page in enumerate(pdf.pages):
        words = page.extract_words(x_tolerance=2, keep_blank_chars=False)
        for t in page.find_tables():
            cells = [c for c in t.cells if c]
            if len(cells) < 12:
                continue
            # 같은 논리적 열인데 헤더 셀과 값 셀의 x가 몇 pt 어긋나 있는
            # 문서가 있다(KR5122420005 실측: "일반사무관리회사보수" 헤더는
            # x=283, 그 값은 x=278 - 그대로 두면 서로 다른 열로 잡혀 열이
            # 14개로 늘고 라벨과 값이 어긋난다). 가까운 x는 한 열로 묶는다.
            raw_x0s = sorted({round(c[0], 1) for c in cells})
            col_x0s = []
            for x in raw_x0s:
                if col_x0s and x - col_x0s[-1] <= 6:
                    continue
                col_x0s.append(x)
            if len(col_x0s) < 7:
                continue

            def col_of(x0):
                return min(range(len(col_x0s)),
                           key=lambda k: abs(col_x0s[k] - x0))

            bands = sorted({(round(c[1], 1), round(c[3], 1)) for c in cells})
            rows = []
            for top, bottom in bands:
                row_cells = [c for c in cells
                             if abs(c[1] - top) < 1 and abs(c[3] - bottom) < 1]
                if not row_cells:
                    continue
                entry = {}
                for (x0, ctop, x1, cbottom) in row_cells:
                    # 단어가 셀 경계를 살짝 넘는 경우가 있어(좁은 클래스명
                    # 칸에서 "…형(A)"의 꼬리가 잘려 클래스 코드를 통째로
                    # 놓쳤다 - KR5122420005 실측) 완전 포함이 아니라 단어
                    # 중심이 셀 안에 드는지로 담는다.
                    ws = [w for w in words
                          if x0 - 1 <= (w["x0"] + w["x1"]) / 2 <= x1 + 1
                          and ctop - 1 <= (w["top"] + w["bottom"]) / 2 <= cbottom + 1]
                    ws.sort(key=lambda w: (round(w["top"], 1), w["x0"]))
                    txt = " ".join(w["text"] for w in ws).strip()
                    if txt:
                        entry[col_of(x0)] = txt
                if entry:
                    rows.append({"top": top, "bottom": bottom, "cells": entry})

            data_rows, header_rows = [], []
            for r in rows:
                nnum = sum(1 for v in r["cells"].values()
                           if DECIMAL_RE.match(v.replace(" ", "")))
                if nnum >= 5:
                    data_rows.append(r)
                elif not data_rows:
                    header_rows.append(r)

            # 클래스명 칸이 값 행과 다른 y구간에 그려진 문서가 있다
            # (KR5131420007 실측: 값 행엔 0번 열이 아예 없고, 클래스명은
            # 별도 행 구간에 "수수료선취-"/"오프라인(A)"처럼 나뉘어 있다).
            # 같은 구간만 한 행으로 묶으면 클래스명을 통째로 놓쳐 그 표의
            # 클래스가 전부 빠진다 - 값 행과 세로로 겹치는 0번 열 글자를
            # 모아 라벨로 붙인다(겹치는 게 없으면 그대로 둔다).
            first_col_x = col_x0s[0]
            label_ws = [w for w in words
                        if first_col_x - 1 <= (w["x0"] + w["x1"]) / 2 < col_x0s[1] - 1]
            for r in data_rows:
                if r["cells"].get(0):
                    continue
                lo, hi = r["top"], r["bottom"]
                ws = [w for w in label_ws
                      if lo - 1 <= (w["top"] + w["bottom"]) / 2 <= hi + 1]
                if ws:
                    ws.sort(key=lambda w: (round(w["top"], 1), w["x0"]))
                    r["cells"][0] = " ".join(w["text"] for w in ws).strip()

            if len(data_rows) >= 2:
                results.append((i + 1, header_rows, data_rows, col_x0s))
    return results


def enrich_with_detail_fee_table(doc_id, existing_rows):
    """요약표(앞쪽)엔 없고 "나.집합투자기구에 부과되는 보수 및 비용"류
    상세표에만 있는 클래스를 보강한다(KR5122420005 실측: 요약표엔 5개
    클래스뿐인데 상세표엔 18개 - README "class_fees.json 코퍼스 전체
    완전성 문제" 참고). 상세표 컬럼 구성이 문서마다 달라서(신탁업자보수/
    수탁회사보수처럼 이름도 다르고 컬럼 개수도 다름) 고정 매핑을 쓰지
    않고, 이미 확인된(요약표에서 뽑힌) 클래스 값과 대조해서 이 문서
    안에서만 통하는 매핑을 매번 다시 찾는다 - 검증 안 되면(요약표 클래스가
    2개 미만이거나 값이 안 맞으면) 아무것도 안 채우고 조용히 넘어간다."""
    known = {r["class_code"]: r for r in existing_rows if r.get("class_code")}
    if len(known) < 2:
        return existing_rows

    def close(a, b, tol=0.0005):
        try:
            return abs(float(a) - float(b)) <= tol
        except (TypeError, ValueError):
            return False

    pdf_candidates = glob.glob(os.path.join(DATA_DIR, doc_id, "*.pdf"))
    if not pdf_candidates:
        return existing_rows

    new_rows = []
    with pdfplumber.open(pdf_candidates[0]) as pdf:
        for page_num, header_rows, grid_rows, col_x0s in _detail_fee_grids(pdf):
            # 칸 이름: 헤더 행들에서 열별로 이어붙인다(셀이 열을 알려주니
            # 좌표로 묶을 필요가 없다 - 예전엔 문서 제목/묶음 헤더가
            # 섞여 "집합"처럼 잘리는 문제가 있었다).
            #
            # "지급비율(연간,%)"/"지급비용(연간%)"처럼 값 칸 전체를 아우르는
            # 단위 표기는 특정 칸의 이름이 아니라서 빼야 한다 - 안 그러면
            # 항목 이름이 "지급비율(연간,%)집합투자업자보수"가 된다.
            label_by_col = []
            for ci in range(len(col_x0s)):
                parts = [h["cells"][ci] for h in header_rows if ci in h["cells"]]
                parts = [p for p in parts
                         if not (p.replace(" ", "").startswith("지급") or "연간" in p)]
                joined = " ".join(parts).replace(" ", "")
                label_by_col.append(joined or None)

            raw_rows = []
            for r in grid_rows:
                label = r["cells"].get(0, "")
                code = None
                for regex in (CLASS_CODE_RE, DETAIL_FEE_CLASS_CODE_JONGRYU_RE):
                    mm = [x for x in regex.finditer(label.replace(" ", ""))
                          if x.group(1) not in DETAIL_FEE_CODE_BLOCKLIST]
                    if mm:
                        code = mm[-1].group(1)
                        break
                cols = {}
                for ci, v in r["cells"].items():
                    if ci == 0:
                        continue
                    t = v.replace(" ", "")
                    if DECIMAL_RE.match(t) and "%" not in t:
                        cols[ci] = t
                raw_rows.append({"class_code": code, "cols": cols, "label": label})

            if not raw_rows:
                continue
            n_cols = len(col_x0s)

            ref_rows = [r for r in raw_rows if r["class_code"] in known]
            if len(ref_rows) < 2:
                continue

            # distribution_fee/peer_avg_fee/total_fee_and_cost: 특정 컬럼
            # 위치 하나가, 값이 있는 참조 행들에서 전부 일치하는지 테스트
            # (그 컬럼 값이 "-"인 참조 행은 그 필드 검증에서만 제외).
            def find_column(field):
                for col in range(n_cols):
                    matched = [
                        r for r in ref_rows
                        if col in r["cols"] and close(r["cols"][col], known[r["class_code"]][field])
                    ]
                    if len(matched) >= 2 and len(matched) == sum(
                        1 for r in ref_rows if col in r["cols"]
                    ):
                        return col
                return None

            dist_col = find_column("distribution_fee")
            peer_col = find_column("peer_avg_fee")
            cost_col = find_column("total_fee_and_cost")

            # total_fee: 단일 컬럼으로 안 맞으면 왼쪽부터 N개 합으로 시도
            # (관측: 항상 "관리 성격" 앞쪽 컬럼들의 합 - README 참고).
            #
            # 셀 격자에선 열 번호가 "표의 모든 칸"에 매겨져서, 값이 안 들어
            # 가는 칸(클래스명 칸 0번, 묶음 헤더만 걸친 빈 칸 등)이 중간에
            # 섞인다(KR5122420005 실측: 값이 1,3,4,5번 열에 있고 2번은 빔).
            # 그래서 "연속된 열 1..n"이 아니라 "값이 실제로 있는 열을 왼쪽
            # 부터 N개"로 잡아야 한다.
            value_cols = sorted(
                {c for r in ref_rows for c in r["cols"] if c != 0})
            total_col = find_column("total_fee")
            total_sum_n = None
            total_sum_cols = None
            if total_col is None:
                for n in range(2, min(6, len(value_cols)) + 1):
                    span = value_cols[:n]
                    complete_refs = [r for r in ref_rows if all(c in r["cols"] for c in span)]
                    if len(complete_refs) >= 2 and all(
                        close(
                            sum(float(r["cols"][c]) for c in span),
                            known[r["class_code"]]["total_fee"],
                        )
                        for r in complete_refs
                    ):
                        total_sum_n, total_sum_cols = n, span
                        break

            # peer_avg_fee(동종유형총보수)는 요약표에서 이미 아는 클래스들도
            # 전부 "-"인 문서가 있다(KR510902773M 실측: C/C-e 둘 다 이미
            # "-") - 대조할 실제 숫자가 하나도 없어 컬럼을 특정할 수 없다.
            # 이 경우 "그 칸이 있는지조차 특정 못 함"이 아니라 "이 문서
            # 자체가 이 필드를 클래스별로 안 보여줌"으로 보고, 새로 채우는
            # 행도 똑같이 "-"로 둔다(거짓으로 숫자를 지어내지 않되, 행
            # 전체를 놓치지도 않는다).
            peer_all_dash = all(known[r["class_code"]]["peer_avg_fee"] == "-" for r in ref_rows)
            if dist_col is None or cost_col is None:
                continue
            if peer_col is None and not peer_all_dash:
                continue
            if total_col is None and total_sum_n is None:
                continue

            for r in raw_rows:
                if not r["class_code"]:
                    continue
                cols = r["cols"]
                if r["class_code"] in known:
                    # 요약표에서 이미 뽑힌 클래스라도, 상세표에만 있는
                    # 세부 항목(집합투자업자보수/신탁업자보수/일반사무관리
                    # 회사보수 등 = fee_breakdown)은 가져와서 채운다.
                    # 처음엔 이런 클래스를 통째로 건너뛰어서, 같은 상세표
                    # 안에 값이 멀쩡히 있는데도 요약표 출신 클래스만
                    # fee_breakdown이 없는 상태가 됐다(KR510902511M 실측:
                    # 14개 클래스 중 요약표 출신 6개만 breakdown 없음 -
                    # 사용자가 원본 표와 대조해서 지적). class_returns에서
                    # 쓴 것과 같은 원칙(FULL OUTER JOIN처럼 "어느 쪽에만
                    # 있는 정보든 다 살린다")을 여기에도 적용한다.
                    # 숫자 필드(total_fee 등)는 요약표 것을 그대로 둔다 -
                    # 이 페이지를 채택한 조건 자체가 "요약표 값과 정확히
                    # 일치"라서 어느 쪽을 써도 같고, 요약표 쪽엔 비용예시
                    # (cost_projection_per_10m)까지 있어 더 완전하다.
                    cur = known[r["class_code"]]
                    if not cur.get("fee_breakdown"):
                        bd = [
                            {"label": label_by_col[c], "value": v}
                            for c, v in sorted(cols.items())
                            if c not in (dist_col, peer_col, cost_col)
                            and (total_col is None or c != total_col)
                        ]
                        if bd:
                            cur["fee_breakdown"] = bd
                            cur.setdefault("field_source_pages", {})["fee_breakdown"] = page_num
                            sp = cur.setdefault("source_pages", [cur["page"]])
                            if page_num not in sp:
                                sp.append(page_num)
                    continue
                # 판매회사보수/동종유형총보수 등 특정 칸이 원본에 "-"로
                # 찍혀 대시 토큰 자체가 안 잡히는 클래스가 있다
                # (KR5122420005 C-W, KR510902773M C-F 등 실측 - 부가서비스가
                # 거의 없는 클래스라 관련 칸이 통째로 "없음"). 이 필드
                # 하나 없다고 행 전체(total_fee 등 나머지 다 있는 값까지)를
                # 버리면 안 되므로, 그 필드만 "-"로 남기고 나머지는 살린다
                # - class_fees.json 기존 관례(peer_avg_fee "-" 보존)와
                # 동일. total_fee만은 이 행의 핵심 값이라 "-" 대체 없이
                # 못 찾으면 이 행 자체를 건너뛴다.
                if total_col is not None:
                    if total_col not in cols:
                        continue
                    total_fee = cols[total_col]
                elif total_sum_cols and all(c in cols for c in total_sum_cols):
                    total_fee = f"{sum(float(cols[c]) for c in total_sum_cols):.4f}"
                else:
                    continue
                breakdown = [
                    {"label": label_by_col[c], "value": v}
                    for c, v in sorted(cols.items())
                    if c not in (dist_col, peer_col, cost_col)
                    and (total_col is None or c != total_col)
                ]
                new_rows.append({
                    "class_code": r["class_code"],
                    "sales_commission_desc": None,
                    "total_fee": total_fee,
                    "distribution_fee": cols.get(dist_col, "-"),
                    "peer_avg_fee": cols.get(peer_col, "-"),
                    "total_fee_and_cost": cols.get(cost_col, "-"),
                    # 상세표엔 1,000만원 비용예시(1년~10년) 칸이 없다 -
                    # build_product_facts_db.py의 cp.get("1y") 등이
                    # None.get()에서 죽지 않도록 dict({})로 둔다(요약표
                    # 클래스는 실제 값이 채워진 dict를 씀).
                    "cost_projection_per_10m": {},
                    "fee_breakdown": breakdown,
                    "page": page_num,
                    "source_pages": [page_num],
                    "field_source_pages": {},
                    "evidence": f"[상세표 보강] {sorted(cols.items())}",
                    "method": "detail_table_cross_validated",
                    "confidence": 0.7,
                    "product_code": doc_id,
                })

    return existing_rows + new_rows

# ---------------------------------------------------------------------------
# "가.투자자에게 직접 부과되는 수수료" 표 - 셀 경계 기반 판매수수료 복구
#
# 이 표는 원래 다른 표들과 같이 좌표(단어의 y로 줄 묶기 + x로 칸 판정)로
# 읽었는데, 그 방식으론 구조적으로 못 푸는 게 두 가지 있었다:
#
#   (1) 병합 셀 - "없음" 하나가 여러 클래스 행을 세로로 덮는 서식.
#       텍스트는 한 번만 찍히니 나머지 행에선 아무것도 안 보인다
#       (KR5172450019: 12행/14행을 덮는 "없음").
#   (2) 셀 내부 줄바꿈 - 한 칸의 내용이 여러 줄로 내려오는데, 옆 칸
#       (클래스명/가입자격)도 같이 줄바꿈되면서 y로 묶을 때 서로 섞인다
#       (KR5157450090 S: "3년미만"과 "환매시" 사이에 클래스명
#       "수수료후취-"가 끼어들어 조건 문구를 통째로 놓쳤다).
#
# 실측으로 두 원인이 남은 실패의 98%였고(줄바꿈 55% / 병합 43%),
# page.find_tables()가 100개 문서 전부에서 동작하는 걸 확인해서
# 셀 경계 기반으로 바꿨다. 그 결과 좌표 방식에서 필요했던 보정들
# (수수료 칸 왼쪽 경계 x 추정, 창 확장 규칙, 헤더 폭 필터, 마커 개수
# 매칭)이 전부 필요 없어졌다 - 칸 경계를 PDF가 직접 알려주기 때문이다.
#
# 문구는 원문을 그대로 쓰지 않고 "{조건}{기준}의 {비율}%이내" 틀로 다시
# 쓴다. 원본이 "이내"를 빼먹는 문서가 있어서다(KR5122420005 A: 이 표엔
# "0.10%"인데 요약표 확인값은 "납입금액의 0.10%이내").
GA_CAPTION_RE = re.compile(r"투자자에게직접부과되는수수료")
GA_NO_VALUE = ("없음", "-")
# 후취 판매수수료 조건 문구는 문서마다 표기가 다르다(실측):
#   "3년 미만 환매시" / "3 년 이내 환매시" / "3년 미만:" (환매시 없이 콜론)
# 조건을 놓치면 "무조건 떼는 수수료"로 뜻이 달라지므로 넓게 잡는다.
# 후취 조건 표기를 전수 조사해보니 141종의 수수료 문구 중 아래처럼 갈렸다:
#   "3년 미만 환매시 환매금액의..."  (가장 흔함)
#   "3 년 이내 환매시 ..."           (미만 대신 이내)
#   "3년 미만: 환매금액의 ..."        (환매시 없이 콜론)
#   "3년 미만 환매금액의 ..."         (환매시도 콜론도 없음)
#   "1,095일 미만 환매 시 ..."        (년이 아니라 일수)
# 조건을 놓치면 "무조건 떼는 수수료"로 뜻이 달라지므로("3년미만 환매시인데
# 언급이 없다"는 사용자 지적으로 처음 발견) 뒤쪽 표현은 선택으로 두고
# "N년/N일 + 미만/이내"까지만 필수로 본다. 일수는 년으로 환산하지 않고
# 원문 단위 그대로 남긴다(1,095일 = 3년이지만 임의로 바꾸면 원문과 달라짐).
GA_COND_RE = re.compile(
    r"([\d,]+)\s*(년|일)\s*(?:미\s*만|이\s*내)\s*(?:환\s*매\s*시)?\s*[:：]?")
GA_PCT_RE = re.compile(r"([\d.]+)\s*%")
GA_BUNUI_RE = re.compile(r"100\s*분의\s*([\d.]+)")


def _ga_cells(page):
    """이 페이지 표들의 셀을 (bbox + 그 안의 텍스트)로 돌려준다.
    셀 안 단어를 y→x 순으로 이어붙이므로 셀 내부 줄바꿈이 있어도 원래
    읽는 순서대로 한 덩어리가 된다(옆 칸 텍스트는 애초에 안 들어온다)."""
    words = page.extract_words(x_tolerance=2, keep_blank_chars=False)
    out = []
    for t in page.find_tables():
        cells = [c for c in t.cells if c]
        if len(cells) < 4:
            continue
        for (x0, top, x1, bottom) in cells:
            ws = [w for w in words
                  if x0 - 1 <= (w["x0"] + w["x1"]) / 2 <= x1 + 1
                  and top - 1 <= (w["top"] + w["bottom"]) / 2 <= bottom + 1]
            ws.sort(key=lambda w: (round(w["top"], 1), w["x0"]))
            out.append({
                "x0": x0, "top": top, "x1": x1, "bottom": bottom,
                "text": " ".join(w["text"] for w in ws).strip(),
            })
    return out


def _ga_left_margin_cells(page, cells):
    """표의 맨 왼쪽 칸(종류/클래스명) 경계를 find_tables()가 못 잡는
    페이지가 있다(KR5139420020 실측: 표가 다음 장으로 이어지는데 그
    페이지에선 왼쪽 세로선이 인식이 안 돼, 클래스명 단어가 어떤 셀에도
    안 들어간다 - 클래스를 못 찾아 그 페이지가 통째로 비었다).

    이럴 때만 쓰는 보완: 인식된 셀들의 왼쪽 바깥에 있는 단어를, 그 셀들이
    만든 행 구간(y)에 맞춰 묶어 "클래스명 칸"을 되살린다. 인식된 셀이
    이미 그 자리를 덮고 있으면(정상 문서) 왼쪽 바깥에 아무것도 없어
    자동으로 아무 일도 안 한다."""
    if not cells:
        return []
    words = page.extract_words(x_tolerance=2, keep_blank_chars=False)
    out = []
    # 왼쪽 끝은 "페이지 전체"가 아니라 "표별"로 잡아야 한다. 한 페이지에
    # 다른 표가 같이 있으면(KR5139420020 p28: 아래쪽에 "나" 보수표가 x=51
    # 부터 있음) 페이지 기준 최솟값이 51이 돼서, 정작 x=135부터 시작하는
    # "가" 표 왼쪽의 클래스명(x≈69~129)을 "바깥"으로 못 본다.
    tables = [t for t in page.find_tables() if len([c for c in t.cells if c]) >= 4]
    for t in tables:
        tcells = [c for c in t.cells if c]
        left_edge = min(c[0] for c in tcells)
        outside = [w for w in words if w["x1"] <= left_edge + 1]
        if not outside:
            continue
        bands = sorted({(round(c[1], 1), round(c[3], 1)) for c in tcells
                        if c[3] - c[1] > 8})
        for top, bottom in bands:
            ws = [w for w in outside if top - 1 <= w["top"] and w["bottom"] <= bottom + 1]
            if not ws:
                continue
            ws.sort(key=lambda w: (round(w["top"], 1), w["x0"]))
            out.append({
                "x0": min(w["x0"] for w in ws), "x1": left_edge,
                "top": top, "bottom": bottom,
                "text": " ".join(w["text"] for w in ws).strip(),
            })
    return out


def _ga_pages(pdf):
    """이 표가 있는 페이지 번호. 캡션은 자본시장법 투자설명서 서식의 고정
    문구라 문서마다 안 바뀐다(판매수수료가 비어 있던 43개 문서 전부에서
    이 문구로 찾아지는 걸 확인). 표가 다음 장으로 이어지는 문서가 있어
    캡션 페이지와 그 다음 페이지를 같이 본다."""
    caption = []
    for i, page in enumerate(pdf.pages):
        words = page.extract_words(x_tolerance=2, keep_blank_chars=False)
        for l in cluster_lines(words):
            if GA_CAPTION_RE.search("".join(w["text"] for w in l).replace(" ", "")):
                caption.append(i)
                break
    wanted = set()
    for i in caption:
        wanted.add(i)
        if i + 1 < len(pdf.pages):
            wanted.add(i + 1)
    return sorted(n + 1 for n in wanted)


def _ga_header_columns(cells):
    """헤더 셀에서 선취/후취/환매/전환 칸의 x구간을 찾는다. 전환수수료
    칸이 아예 없는 문서가 있어(선취/후취/환매 3칸) 찾아지는 만큼만 쓴다.
    긴 설명문 셀이 우연히 걸리지 않게 짧은 셀만 본다.

    칸 이름이 위아래 두 셀로 쪼개져 있는 문서가 있다(KR5157450090 실측:
    x[505-552]에 "환매"(위 셀)와 "수수료"(아래 셀)가 따로 들어 있어,
    한 셀만 보면 "환매수수료"라는 이름을 못 찾고 이 문서 전체를 놓친다).
    x구간이 같은 셀들을 세로로 먼저 이어붙인 뒤 이름을 맞춘다."""
    stacked = {}
    for c in cells:
        t = c["text"].replace(" ", "")
        if not t or len(t) > 12:
            continue
        stacked.setdefault((round(c["x0"]), round(c["x1"])), []).append((c["top"], t))

    cols = {}
    for (x0, x1), pieces in stacked.items():
        joined = "".join(t for _, t in sorted(pieces))
        for key, pat in (("선취", "선취"), ("후취", "후취"),
                         ("환매", "환매수수료"), ("전환", "전환수수료")):
            if key not in cols and pat in joined:
                cols[key] = (x0, x1)
    if "환매" not in cols or len(cols) < 2:
        return None
    return cols


def _ga_commission_desc(text):
    """수수료 칸 텍스트를 정형화된 문구로 다시 쓴다. 기준(납입/환매금액)과
    비율을 둘 다 못 찾으면 None(= 채우지 않음)."""
    flat = text.replace(" ", "")
    basis = ("환매금액" if "환매금액" in flat
             else ("납입금액" if "납입" in flat else None))
    if not basis:
        return None
    pm = GA_PCT_RE.search(flat) or GA_BUNUI_RE.search(flat)
    if not pm:
        return None
    prefix = ""
    if basis == "환매금액":
        cm = GA_COND_RE.search(flat)
        if cm:
            prefix = f"{cm.group(1)}{cm.group(2)} 미만 환매시: "
    return f"{prefix}{basis}의 {pm.group(1)}%이내"


def enrich_sales_commission_from_ga_table(doc_id, existing_rows):
    """"나" 상세표 보강으로 추가된 클래스는 판매수수료 문구가 없다
    (그 표엔 그 칸 자체가 없음). 이 표에서 채운다."""
    targets = [r for r in existing_rows
               if r.get("class_code") and r.get("sales_commission_desc") is None]
    if not targets:
        return existing_rows
    by_code = {}
    for r in targets:
        by_code.setdefault(r["class_code"], []).append(r)

    pdf_candidates = glob.glob(os.path.join(DATA_DIR, doc_id, "*.pdf"))
    if not pdf_candidates:
        return existing_rows

    with pdfplumber.open(pdf_candidates[0]) as pdf:
        prev_cols, prev_page = None, None
        for page_num in _ga_pages(pdf):
            page = pdf.pages[page_num - 1]
            cells = _ga_cells(page)
            cells = cells + _ga_left_margin_cells(page, cells)
            if not cells:
                continue
            cols = _ga_header_columns(cells)
            if cols is None:
                # 표가 다음 장으로 이어지면 그 페이지엔 헤더가 없다
                # (KR5113420013: 헤더 44p / 데이터 45p). 바로 앞 페이지의
                # 칸 구성을 물려받는다 - 같은 표의 연속이라 x도 같다.
                if prev_cols is None or prev_page != page_num - 1:
                    continue
                cols = prev_cols
            prev_cols, prev_page = cols, page_num

            for c in cells:
                code = None
                for rx in (CLASS_CODE_RE, DETAIL_FEE_CLASS_CODE_JONGRYU_RE):
                    mm = [x for x in rx.finditer(c["text"].replace(" ", ""))
                          if x.group(1) not in DETAIL_FEE_CODE_BLOCKLIST]
                    if mm:
                        code = mm[-1].group(1)
                        break
                if not code or code not in by_code:
                    continue
                if by_code[code][0].get("sales_commission_desc") is not None:
                    continue  # 앞 페이지에서 이미 채움

                # 이 클래스 셀과 세로로 겹치는 수수료 칸 셀들. 병합 셀은
                # 여러 행을 덮는 하나의 큰 셀이라 이 겹침 판정만으로
                # 자연스럽게 해당 행 전부에 적용된다.
                lo, hi = c["top"], c["bottom"]
                vals = {}
                for key, (cx0, cx1) in cols.items():
                    # 헤더 셀이 데이터 셀보다 안쪽으로 그려진 문서가 있어
                    # (KR5172450019: 헤더 x[277-359] vs 값 x[271-365])
                    # 포함이 아니라 헤더 칸 중심이 값 셀 안에 드는지로 본다.
                    ccx = (cx0 + cx1) / 2
                    hit = [d for d in cells
                           if d is not c
                           and not (d["bottom"] <= lo + 1 or d["top"] >= hi - 1)
                           and d["x0"] - 3 <= ccx <= d["x1"] + 3]
                    vals[key] = " ".join(h["text"] for h in hit if h["text"]).strip()

                filled = [v for v in vals.values() if v]
                if len(filled) < 2:
                    continue  # 이 표의 칸을 제대로 못 읽음 - 건드리지 않는다
                if all(v in GA_NO_VALUE for v in filled):
                    # 읽힌 칸이 전부 "없음"/"-" = 판매수수료가 없는 클래스.
                    # (이어지는 페이지에서 전환수수료 칸 셀이 아예 없는
                    #  문서가 있어, 칸 개수가 아니라 "읽힌 것 전부"로 본다)
                    desc = "-"
                else:
                    desc = _ga_commission_desc(
                        " ".join(v for v in filled if v not in GA_NO_VALUE))
                if desc:
                    for r in by_code[code]:
                        r["sales_commission_desc"] = desc
                        r.setdefault("field_source_pages", {})["sales_commission_desc"] = page_num
                        sp = r.setdefault("source_pages", [r["page"]])
                        if page_num not in sp:
                            sp.append(page_num)

    return existing_rows



# ---------------------------------------------------------------------------
# 요약표(앞쪽 "<요약정보>" 안의 투자비용 표) - 셀 경계 기반
#
# 이 표가 class_fees의 주력 소스다(전체의 3분의 2). 좌표 방식일 때는 여기에
# 문서별 예외가 가장 많이 붙었다 - "소수 4개 + 정수 5개" 같은 개수 판정,
# 판매수수료 문구를 찾으려 데이터 줄 앞뒤로 창을 넓히는 규칙, 세로 캡션
# 걸러내는 x좌표 상수, 글자가 한 자씩 쪼개지는 서식 보정 등. 전부 "이 단어가
# 어느 칸인지 모른다"에서 나온 것이라, 셀 경계를 쓰면 사라진다.
#
# 열 매핑은 상세표와 달리 대조할 정답지가 없어서(이 표가 곧 정답지다)
# 헤더 이름으로 한다. 전수 조사(98개 문서) 결과 핵심 이름은 거의 고정이다:
#   판매보수 98 / 총보수 94 / 동종유형총보수 86 / 총보수ㆍ비용 계열 80
# 주의 두 가지:
#   - 가운뎃점이 문서마다 다르다(ㆍ · ･ ∙ ▪ •) → 정규화해서 비교
#   - "총보수"가 "총보수ㆍ비용"의 앞부분이라 짧은 이름부터 맞추면 밀린다
#     → 긴(구체적인) 이름부터 매칭

def _label_class_code(label):
    """라벨 문자열에서 클래스 코드를 뽑는다(못 찾으면 None). 요약표/상세표가
    같은 규칙을 쓰도록 한 곳에 모은다 - 코드 표기가 "(A)"(괄호 안),
    "A(수수료선취-오프라인)"(괄호 앞), "종류A", "(Cp(퇴직연금))"(중첩)로
    문서마다 다르다."""
    flat = label.replace(" ", "")
    for regex in (CLASS_CODE_RE, CLASS_CODE_NESTED_RE,
                  DETAIL_FEE_CLASS_CODE_JONGRYU_RE):
        mm = [x for x in regex.finditer(flat)
              if x.group(1) not in DETAIL_FEE_CODE_BLOCKLIST]
        if mm:
            return mm[-1].group(1)
    m2 = CLASS_CODE_PREFIX_RE.match(flat)
    if m2:
        return m2.group(1)
    return None


# 가운뎃점은 문서마다 다른 글자를 쓴다. 심볼 폰트로 찍힌 문서는 유니코드
# 사용자 정의 영역(U+F000~U+F0FF)에 들어와서 눈으로는 똑같은 "총보수·비용"인데
# 글자 코드가 전혀 다르다(KR5111450067 실측: U+F09E - 총보수·비용 열이
# 통째로 안 잡혔다).
FOOTNOTE_RE = re.compile(r"^\(?주\s*\d*\)")
SUMMARY_DOT_RE = re.compile("[\u318d\u00b7\uff65\u2219\u25aa\u2022\u30fb\u22c5\u2027\uf000-\uf0ff]")


def _norm_header(s):
    return SUMMARY_DOT_RE.sub("·", s.replace(" ", ""))


def _summary_column_field(name):
    """헤더 이름 → 필드. 못 알아보면 None. 긴 이름부터 본다."""
    n = _norm_header(name)
    if not n:
        return None
    if "동종유형" in n:
        return "peer_avg_fee"
    # "합성총보수,비용"처럼 가운뎃점 자리에 쉼표를 쓰는 문서가 있다
    # (KR5122420005 실측 - 그대로 두면 이 칸을 못 알아보고, 옆의 비용예시
    # 묶음 헤더가 대신 매칭돼 1년 비용예시가 총보수·비용 자리에 들어갔다).
    if ("총보수·비용" in n or "총보수비용" in n or "총보수,비용" in n
            or n.endswith("총보수·")):
        return "total_fee_and_cost"
    if "판매보수" in n:
        return "distribution_fee"
    if "판매수수료" in n:
        return "sales_commission_desc"
    if n == "총보수" or n.endswith("총보수"):
        return "total_fee"
    # 글자가 그려진 순서 때문에 "1년"이 "년 1"로 뒤집혀 추출되는 문서가
    # 있다(KR5156450026/KR5160420009/KR555202013M 실측 - 비용예시 열이
    # 통째로 안 잡혀 폴백의 큰 덩어리였다). 두 순서를 모두 받는다.
    m = (re.fullmatch(r"(?:최근)?(\d+)년(?:차|째|간)?", n)
         or re.fullmatch(r"(?:최근)?년(\d+)", n))
    if m:
        return f"cost_{m.group(1)}y"
    return None


def _summary_grid(page, next_page=None, inherited=None):
    """요약표를 셀 격자로 읽는다. 이 페이지에서 "클래스종류/총보수/판매보수"
    헤더를 가진 표만 고른다(같은 페이지의 운용전문인력 표 등과 구분).
    돌려주는 것: (field_by_col, data_rows) 또는 None."""
    words = page.extract_words(x_tolerance=2, keep_blank_chars=False)
    header_carry = None
    for t in page.find_tables():
        tbox = t.bbox          # 아래에서 t를 다른 뜻으로 다시 쓰므로 먼저 잡아둔다
        cells = [c for c in t.cells if c]
        if len(cells) < 20:
            continue
        raw_x0s = sorted({round(c[0], 1) for c in cells})
        col_x0s = []
        for x in raw_x0s:
            if col_x0s and x - col_x0s[-1] <= 6:
                continue
            col_x0s.append(x)
        if len(col_x0s) < 4:
            continue

        def col_of(x0):
            return min(range(len(col_x0s)), key=lambda k: abs(col_x0s[k] - x0))

        bands = sorted({(round(c[1], 1), round(c[3], 1)) for c in cells})
        grid = []
        for top, bottom in bands:
            ent = {}
            used = set()
            for (x0, ct, x1, cb) in [c for c in cells
                                     if abs(c[1] - top) < 1 and abs(c[3] - bottom) < 1]:
                ws = [w for w in words
                      if x0 - 1 <= (w["x0"] + w["x1"]) / 2 <= x1 + 1
                      and ct - 1 <= (w["top"] + w["bottom"]) / 2 <= cb + 1]
                ws.sort(key=lambda w: (round(w["top"], 1), w["x0"]))
                txt = " ".join(w["text"] for w in ws).strip()
                if txt:
                    ent[col_of(x0)] = txt
                    used.update(id(w) for w in ws)
            grid.append({"top": top, "bottom": bottom, "cells": ent, "used": used})

        # 표의 오른쪽 세로줄이 인식되지 않아 마지막 열이 격자에서 통째로
        # 빠지는 문서가 있다(KR5113420069/KR5113450401 실측: 격자는
        # x=500.8에서 끝나는데 "10년" 머리글과 값 510은 그 오른쪽,
        # 표 테두리(542.6) 안쪽에 있다). 테두리 안에 격자 밖 글자가
        # 남아 있으면 열을 하나 더 만들어 머리글과 값을 함께 살린다.
        last_x1 = min((c[2] for c in cells if abs(c[0] - col_x0s[-1]) < 1),
                      default=col_x0s[-1])
        if tbox[2] - last_x1 > 10:
            extra = [w for w in words
                     if last_x1 <= (w["x0"] + w["x1"]) / 2 <= tbox[2] + 1
                     and tbox[1] <= (w["top"] + w["bottom"]) / 2 <= tbox[3]]
            if extra:
                xi = len(col_x0s)
                col_x0s.append(last_x1)
                claimed = set()
                # 병합 셀 때문에 세로로 긴 띠가 같은 글자를 또 가져가면
                # 머리글이 "10년 510"처럼 뭉쳐 열 이름 매칭이 깨진다.
                # 짧은 띠부터 나눠 가장 딱 맞는 행이 먼저 가져가게 한다.
                for r in sorted(grid, key=lambda r: r["bottom"] - r["top"]):
                    ws = [w for w in extra
                          if id(w) not in claimed and id(w) not in r["used"]
                          and r["top"] - 1 <= (w["top"] + w["bottom"]) / 2 <= r["bottom"] + 1]
                    if not ws:
                        continue
                    ws.sort(key=lambda w: (round(w["top"], 1), w["x0"]))
                    r["cells"][xi] = " ".join(w["text"] for w in ws).strip()
                    r["used"].update(id(w) for w in ws)
                    claimed.update(id(w) for w in ws)

        # 데이터 행: 소수 2개 이상 (비용예시 정수는 없는 문서도 있어 소수만 본다)
        first_data = None
        for gi, r in enumerate(grid):
            ndec = sum(1 for v in r["cells"].values()
                       if DECIMAL_RE.match(v.replace(" ", "").rstrip("%")))
            if ndec >= 2:
                first_data = gi
                break
        # 헤더만 있고 값 행은 통째로 다음 페이지에 있는 문서가 있다
        # (KR5118420036/KR5113450111 실측: 4쪽에 "총보수/판매보수/1년..."
        # 헤더만 있고 클래스 행은 5쪽부터다). 예전엔 이런 표를 그냥
        # 버려서 다음 페이지가 물려받을 열 구성이 없었고, 결국 문서
        # 전체가 폴백이었다. 값 행이 없어도 열 구성은 넘겨준다.
        header_only = first_data is None
        if header_only:
            first_data = len(grid)

        # 여러 칸을 아우르는 묶음 헤더는 칸 이름이 아니므로 뺀다
        # ("1,000만원 투자시 ... 총보수•비용 예시(단위:천원)"가 비용예시
        #  칸들 위에 걸쳐 있어서, 그대로 쓰면 "총보수•비용"이 들어 있다는
        #  이유로 1년 칸이 total_fee_and_cost로 잘못 매칭된다).
        # 처음엔 "길이가 길면 묶음 헤더"로 걸렀는데, 그러면 짧은 묶음
        # 헤더("예시 (단위:천원)")는 못 걸러 이름이 "예시(단위:천원)1년"이
        # 되고, 반대로 진짜 칸 이름의 조각("비용")이 잘려 "총보수·"만
        # 남는 일이 생겼다(실측 22개 문서가 이 때문에 폴백). 길이가 아니라
        # 묶음 헤더에만 나오는 문구로 거른다.
        header_names = {}
        for r in grid[:first_data]:
            for ci, v in r["cells"].items():
                flat = v.replace(" ", "")
                if len(flat) > 24:
                    continue
                if any(k in flat for k in ("투자시", "단위", "예시", "투자자가", "투자기간")):
                    continue
                header_names.setdefault(ci, []).append(v)
        field_by_col = {}
        inherited_need = 0
        for ci, parts in header_names.items():
            # 같은 칸 이름이 헤더 두 줄에 겹쳐 그려진 표가 있다
            # (KR5157420003 실측: "2년"이 두 띠에 다 잡혀 "2년2년"이 됐다).
            # 이어 붙인 이름이 안 맞으면 중복을 걷어낸 이름으로 다시 본다.
            uniq = list(dict.fromkeys(parts))
            for cand in ("".join(parts), "".join(uniq)):
                f = _summary_column_field(cand)
                if f and f not in field_by_col.values():
                    field_by_col[ci] = f
                    break

        # 세로줄이 값 구간에서 끊겨 있어 데이터 행에 그 열의 칸이 아예
        # 안 생기는 표가 있다(KR5127420034 실측: 헤더엔 "10년" 칸이 있는데
        # 값 행엔 그 칸이 없어 10년 비용예시가 통째로 빠졌다 - 폴백 문서
        # 20개가 이 한 가지 이유였다). 칸이 안 그려졌을 뿐 열의 x범위와
        # 행의 y범위는 표에서 이미 알고 있으니, 그 사각형 안의 글자를
        # 읽어 채운다. 값 칸(숫자)일 때만 채워서 병합 셀의 글자가 여러
        # 행에 복제되지 않게 한다.
        table_x1 = max(c[2] for c in cells)

        def fill_missing(field_map):
            for r in grid:
                if sum(1 for v in r["cells"].values()
                       if DECIMAL_RE.match(v.replace(" ", "").rstrip("%"))) < 2:
                    continue
                for ci in field_map:
                    if ci in r["cells"] or ci + 1 > len(col_x0s):
                        continue
                    lo = col_x0s[ci]
                    hi = col_x0s[ci + 1] if ci + 1 < len(col_x0s) else table_x1
                    # 이 행의 다른 칸이 이미 가져간 글자는 뺀다. 안 그러면
                    # 좁은 칸 두 개에 걸쳐 가운데 정렬된 숫자가 양쪽 열에
                    # 복제돼(KR5129420031 실측: 판매보수 0.18이 7·8번 열에
                    # 모두 들어갔다), "이 열엔 값이 없다"를 근거로 어긋난
                    # 열을 고치는 아래 보정이 통째로 무력해진다.
                    ws = [w for w in words
                          if id(w) not in r["used"]
                          and lo - 1 <= (w["x0"] + w["x1"]) / 2 < hi
                          and r["top"] - 1 <= (w["top"] + w["bottom"]) / 2 <= r["bottom"] + 1]
                    if not ws:
                        continue
                    ws.sort(key=lambda w: (round(w["top"], 1), w["x0"]))
                    txt = " ".join(w["text"] for w in ws).strip()
                    t = txt.replace(" ", "").rstrip("%").replace(",", "")
                    if DECIMAL_RE.match(t) or t.isdigit():
                        r["cells"][ci] = txt
                        r["used"].update(id(w) for w in ws)

        fill_missing(field_by_col)

        def _isnum(v):
            t = v.replace(" ", "").rstrip("%").replace(",", "")
            return bool(DECIMAL_RE.match(t) or t.isdigit())

        def realign(field_map):
            """헤더가 가리키는 열과 값이 실제로 들어 있는 열이 어긋난 것을
            바로잡는다. 헤더 이름으로 잡을 때든 앞 페이지에서 물려받을
            때든 같은 어긋남이 생기므로 두 경우 모두 이 함수를 쓴다."""
            # (KR5129420025/KR5144450095 실측: "판매보수" 헤더는 7번 열인데
            # 값은 6번 열에 있다 - 열 병합 허용치(6pt)를 넘게 벌어져서다.)
            # 헤더가 가리키는 열에 값이 하나도 없고 바로 옆 열에 값이 있으면
            # 그쪽으로 옮긴다(다른 필드가 이미 쓰는 열은 건드리지 않는다).
            value_cols = {ci for r in grid[first_data:]
                          for ci, v in r["cells"].items() if _isnum(v)}

            def move(ci, ok_cols):
                for nb in (ci - 1, ci + 1):
                    if nb in ok_cols and nb not in field_map:
                        field_map[nb] = field_map.pop(ci)
                        return nb
                return ci

            # 먼저 총보수 칸만 맞춘다. 이 칸이 어긋나 있으면 아래에서 진짜
            # 보수 행을 못 골라 나머지 보정이 전부 엉뚱해진다.
            tf_col = next((c for c, f in field_map.items() if f == "total_fee"), None)
            if tf_col is not None and tf_col not in value_cols:
                tf_col = move(tf_col, value_cols)

            # 같은 격자 안에 수익률표·운용전문인력표가 이어 붙어 있는 문서가
            # 있다(KR5153420022/KR5118420036 실측). 그 행들의 숫자까지
            # 근거로 삼으면 "그 칸에도 값이 있다"고 착각해 필드를 엉뚱한
            # 옆 칸으로 옮긴다. 총보수 칸에 값이 있는 행만 진짜 보수 행으로
            # 보고, 그 행들이 실제로 쓰는 칸만 근거로 쓴다.
            # 총보수 칸에 숫자가 있다는 것만으론 부족하다 - 수익률표 행에도
            # 우연히 같은 칸에 숫자가 있다(KR5118420036 실측: 그 행들 때문에
            # 동종유형이 반대쪽 옆 칸으로 끌려갔다). 잡아 둔 칸들이 한 행에
            # 대부분 채워져 있어야 진짜 보수 행이다.
            need_row = max(2, int(len(field_map) * 0.6))
            fee_rows = [r for r in grid[first_data:]
                        if tf_col is not None and _isnum(r["cells"].get(tf_col, ""))
                        and sum(1 for ci in field_map if ci in r["cells"]) >= need_row]
            if not fee_rows:
                # 보수 행을 아예 못 고르면 예전처럼 전체 행을 근거로 쓴다
                for ci in sorted(field_map):
                    if ci not in value_cols:
                        move(ci, value_cols)
                return
            fee_cols = {ci for r in fee_rows for ci, v in r["cells"].items()
                        if _isnum(v)}
            # 원본이 "값 없음"을 "-"로 찍은 칸도 그 필드가 쓰는 칸이다
            # (동종유형 비교가 없는 상품이 그렇다) - 숫자만 보면 그 칸을
            # 빈 칸으로 오해해 필드를 엉뚱한 데로 옮기게 된다.
            dash_cols = {ci for r in fee_rows for ci, v in r["cells"].items()
                         if v.replace(" ", "") in ("-", "–", "—")}
            for ci in sorted(field_map):
                if ci in fee_cols or ci in dash_cols:
                    continue
                for nb in (ci - 1, ci + 1):
                    if nb in (fee_cols | dash_cols) and nb not in field_map:
                        field_map[nb] = field_map.pop(ci)
                        break

        realign(field_by_col)

        # 표가 다음 페이지로 이어지면 그 페이지엔 헤더가 반복되지 않는다
        # (KR5113470030/KR5118420006 등 실측: A-e/C-e/C-P 같은 클래스가
        # 이어지는 페이지에 있는데 헤더가 없어 그 페이지를 통째로 버렸다 -
        # 폴백의 가장 흔한 원인이었다). 앞 페이지에서 잡은 열 구성이 이
        # 표와 같은 모양이면(열 개수·x좌표가 거의 같으면) 그대로 물려받는다.
        fields = set(field_by_col.values())
        if not {"total_fee", "distribution_fee"} <= fields and inherited:
            prev_fields, prev_cols = inherited
            # 이어지는 페이지에선 맨 왼쪽 클래스명 열이 표 인식에서
            # 빠지기도 해서 열 개수가 달라진다(KR5113470030 실측: 5페이지
            # 13열 → 6페이지 11열). 개수를 맞추라고 요구하지 말고, 각
            # 열의 x좌표로 앞 페이지 열에 대응시킨다.
            # 앞 페이지의 한 열에 이 페이지 열 두 개가 나란히 걸리는 일이
            # 흔하다(KR5118420036 실측: 앞 장 239.0에 232.3과 239.1이 둘 다
            # 8pt 안에 든다). 먼저 만난 쪽을 쓰면 값이 없는 칸을 총보수로
            # 잡아 모든 행이 버려지므로, 열마다 가장 가까운 하나만 쓴다.
            mapped = {}
            matched_cols = 0
            best = {}
            for ci, x in enumerate(col_x0s):
                near = min(range(len(prev_cols)),
                           key=lambda k: abs(prev_cols[k] - x))
                dist = abs(prev_cols[near] - x)
                if dist > 8:
                    continue
                matched_cols += 1
                if near in prev_fields and (near not in best or dist < best[near][0]):
                    best[near] = (dist, ci)
            for near, (_, ci) in best.items():
                mapped[ci] = prev_fields[near]
            # 같은 표의 연속인지 엄격히 본다: 앞 페이지가 쓰던 값 칸이
            # 거의 다(80% 이상) 같은 x에 다시 나타나야 한다. 느슨하게 두면
            # 같은 페이지의 다른 표(운용전문인력 표 등)에 요약표 열 매핑이
            # 씌워져 "책임운용 정재환 1979..." 같은 행이 클래스로 잡힌다
            # (KR5122420005 실측 - 5행이어야 하는데 7행이 됐다).
            enough = matched_cols >= max(4, int(len(prev_fields) * 0.8))
            # x좌표가 겹치는 것만으로는 부족하다 - 같은 페이지의 "투자실적
            # 추이" 수익률표도 열 위치가 비슷해서 통과해 버린다(실측:
            # "비교지수(%)"와 "1981 8"이 클래스로 잡히고, 수익률 3.22가
            # 진짜 C의 총보수 0.45를 덮어썼다). 진짜 이어지는 표라면 앞
            # 장에서 쓰던 칸들이 한 행에 대부분 다시 채워져 있어야 한다.
            need = max(3, int(len(mapped) * 0.7))
            same_shape = any(sum(1 for ci in mapped if ci in r["cells"]) >= need
                             for r in grid[first_data:])
            if enough and same_shape and \
                    {"total_fee", "distribution_fee"} <= set(mapped.values()):
                field_by_col = mapped
                fields = set(field_by_col.values())
                inherited_need = need
        if not {"total_fee", "distribution_fee"} <= fields:
            continue
        # 이어받은 열 구성으로 확정된 뒤에도, 안 그려진 칸을 채우고 한 칸씩
        # 밀린 열을 다시 맞춘다. 헤더 페이지와 값 페이지의 격자 모양이 달라
        # 이어받은 매핑이 통째로 어긋나는 문서가 있다(KR5118420036 실측:
        # 총보수가 빈 칸을 가리켜 모든 행이 버려졌다).
        if inherited_need:
            realign(field_by_col)
        fill_missing(field_by_col)

        data_rows = grid[first_data:]
        # 클래스명은 값 행과 다른 y구간에 그려지는 경우가 많다(상세표와
        # 같은 문제) - 값 행과 세로로 겹치는 맨 왼쪽 열 글자를 라벨로 붙인다.
        # 클래스명 칸이 격자상 여러 열에 걸쳐 있는 문서가 있다(글자가
        # 좁은 칸에서 줄바꿈되며 쪼개짐 - KR5114420046 실측: 한 열만 보면
        # "라인-퇴"처럼 조각만 잡힌다). 첫 값 칸보다 왼쪽 전체를 이 클래스의
        # 이름 영역으로 본다.
        label_col = max(0, min(field_by_col) - 1) if field_by_col else 0
        first_field_x = col_x0s[min(field_by_col)] if field_by_col else 1e9
        label_ws = [w for w in words if (w["x0"] + w["x1"]) / 2 < first_field_x - 2]
        real_rows = [r for r in data_rows
                     if sum(1 for v in r["cells"].values()
                            if DECIMAL_RE.match(v.replace(" ", "").rstrip("%"))) >= 2]
        # 열 구성을 앞 장에서 물려받은 경우엔 이 표가 정말 그 표의 연장인지
        # 행 단위로 다시 본다. 표 전체로만 보면 수익률표·운용전문인력표에도
        # 매핑이 씌워져 "비교지수(%)"나 "운용책임...전문인력"이 클래스로
        # 잡힌다(KR5118420006/KR5118420036 실측). 앞 장에서 쓰던 칸이
        # 대부분 다시 채워진 행만 남긴다.
        if inherited_need:
            real_rows = [r for r in real_rows
                         if sum(1 for ci in field_by_col if ci in r["cells"])
                         >= inherited_need]
        out = []
        for i, r in enumerate(real_rows):
            label = r["cells"].get(label_col, "")
            # 이미 코드가 읽히는 라벨은 넓히지 않는다 - 마지막 행에서
            # 표 끝까지 훑다가 각주("(주1) 1,000만원 지불하게 되는...")를
            # 끌어와 코드를 못 찾게 되는 사고가 있었다(KR5125450023 실측).
            if _label_class_code(label) is None:
                # 클래스명이 값 행보다 세로로 길게 이어지는 문서가 있다
                # (KR5113470030 실측: 값 행 구간만 보면 "수수료미 징구-오"
                # 처럼 잘려 클래스 코드를 못 찾는다). 이 행 시작부터 다음
                # 데이터 행 시작 직전까지를 이 클래스의 이름 구간으로 본다.
                # 마지막 행은 다음 행이 없으니 표 아래 끝까지 본다
                # (+40 같은 고정폭으로 자르면 이름이 길게 이어지는 문서에서
                # 여전히 잘린다 - KR5113470030 실측).
                hi = (real_rows[i + 1]["top"] if i + 1 < len(real_rows)
                      else max(c[3] for c in cells) + 1)
                ws = [w for w in label_ws
                      if r["top"] - 1 <= (w["top"] + w["bottom"]) / 2 < hi - 1]
                if ws:
                    ws.sort(key=lambda w: (round(w["top"], 1), w["x0"]))
                    # 마지막 행은 아래로 표 끝까지 훑게 되는데, 그 아래에
                    # 각주와 다른 표(수익률표 등)가 이어지는 문서가 있다
                    # (KR5114420046 실측: "...퇴직연금(Cf) - 종류형
                    # 집합투자기구의 ... 퇴직연금(C) 비교지수(%) ..."까지
                    # 딸려와 코드가 Cf가 아니라 각주 속 C로 잡혔다).
                    # 각주 시작 표시를 만나면 거기서 끊는다.
                    # 클래스명 안에도 "-"가 별도 토큰으로 들어간다
                    # ("수수료미징 구 - 온 라 인-퇴직...") - 그래서 "-"를
                    # 무조건 경계로 보면 이름이 잘려 코드를 놓친다. 이미
                    # 코드를 찾은 뒤에 나오는 "-"부터 각주로 본다.
                    parts = []
                    for w in ws:
                        t = w["text"]
                        got = _label_class_code(" ".join(parts)) is not None
                        # 각주 번호는 "주)" 말고 "주1)" 꼴도 쓴다
                        # (KR5118420036 실측: 마지막 행 라벨이 주1)~주4)와
                        # 그 아래 수익률표까지 삼켜 C-P가 C로 잡혔다).
                        if got and (t == "-" or FOOTNOTE_RE.match(t)):
                            break
                        parts.append(t)
                    label = " ".join(parts).strip() or label
            if _label_class_code(label) is None and i == len(real_rows) - 1 and next_page is not None:
                # 클래스명이 페이지 경계를 넘어가는 문서가 있다
                # (KR5116501001 실측: "수수료미징구- 온라인-"에서 페이지가
                # 끝나고 "퇴직연금(C-Pe)"가 다음 장 맨 위에 있다). 마지막
                # 행의 코드를 못 찾았을 때만 다음 페이지 첫머리를 이어본다.
                nws = next_page.extract_words(x_tolerance=2, keep_blank_chars=False)
                if nws:
                    top0 = min(w["top"] for w in nws)
                    head = [w for w in nws
                            if w["top"] <= top0 + 30
                            and (w["x0"] + w["x1"]) / 2 < first_field_x - 2]
                    head.sort(key=lambda w: (round(w["top"], 1), w["x0"]))
                    if head:
                        cand = (label + " " + " ".join(w["text"] for w in head)).strip()
                        if _label_class_code(cand) is not None:
                            label = cand
            out.append({"label": label, "cells": r["cells"]})
        if out:
            return field_by_col, out, col_x0s
        if header_only and header_carry is None and \
                {"total_fee", "distribution_fee"} <= set(field_by_col.values()):
            header_carry = (field_by_col, col_x0s)
    if header_carry:
        return header_carry[0], [], header_carry[1]
    return None


SUMMARY_COST_KEYS = ["1y", "2y", "3y", "5y", "10y"]


def summary_rows_for_doc(doc_id, pdf, pages):
    """요약표를 셀 격자로 읽어 class_fees 레코드로 만든다(좌표 방식
    find_fee_rows_on_page의 대체). 페이지마다 표를 찾고, 헤더 이름으로
    잡은 열 매핑에 따라 값을 담는다."""
    rows = []
    inherited = None
    prev_page = None
    for page_num in pages:
        if page_num < 1 or page_num > len(pdf.pages):
            continue
        nxt = pdf.pages[page_num] if page_num < len(pdf.pages) else None
        # 열 구성 이어받기는 "바로 다음 페이지"에서만 허용한다 - 떨어진
        # 페이지의 무관한 표에까지 요약표 열 매핑을 씌우면 엉뚱한 행이
        # 클래스로 잡힌다(KR5122420005 실측: 5행이어야 하는데 7행이 됨).
        use_inherited = inherited if prev_page == page_num - 1 else None
        got = _summary_grid(pdf.pages[page_num - 1], nxt, use_inherited)
        if not got:
            continue
        field_by_col, grid_rows, col_x0s = got
        inherited = (field_by_col, col_x0s)
        prev_page = page_num
        for r in grid_rows:
            label = r["label"]
            flat = label.replace(" ", "")
            code = _label_class_code(label)
            if code is None:
                # 클래스가 하나뿐이라 코드 표기 자체가 없는 문서가 있다
                # (KR5123365001 실측: "투자신탁" 라벨 하나) - 코드를 지어
                # 내지 않고 원본 라벨을 그대로 이름으로 쓴다.
                code = flat or None
            if not code:
                continue

            vals = {}
            for ci, v in r["cells"].items():
                f = field_by_col.get(ci)
                if f:
                    vals[f] = v.strip()

            total_fee = vals.get("total_fee")
            if total_fee is None:
                continue
            cost = {}
            for k in SUMMARY_COST_KEYS:
                v = vals.get(f"cost_{k}")
                if v:
                    cost[k] = v.replace(",", "")

            raw_comm = vals.get("sales_commission_desc")
            if raw_comm is None:
                desc = None
            elif raw_comm.replace(" ", "") in ("없음", "-"):
                desc = "-"
            else:
                desc = _ga_commission_desc(raw_comm) or None

            def clean(f):
                v = vals.get(f)
                if v is None:
                    return None
                v = v.replace(" ", "").rstrip("%")
                return v or None

            # 원본이 소수점 자리에 쉼표를 찍은 문서가 있다(KR5169950018
            # 실측: 총보수·비용이 "1,807"로 찍혀 있는데 총보수 1.805 +
            # 기타비용이라 1.807이 맞다 - 조판 오타). 셀에서 그대로 읽으면
            # 1807이 돼 값이 1000배가 된다. 총보수·비용은 총보수보다
            # 아주 조금 큰 값이라는 성질로 안전하게 되돌린다(다른 자릿수
            # 조합엔 손대지 않는다).
            corrections = []

            def fix_comma_decimal(v, ref):
                if v is None or ref is None:
                    return v
                mm = re.fullmatch(r"(\d)[,](\d{3})", v)
                if not mm:
                    return v
                try:
                    cand = float(f"{mm.group(1)}.{mm.group(2)}")
                    r = float(ref)
                except ValueError:
                    return v
                if r <= cand <= r + 0.5:
                    fixed = f"{mm.group(1)}.{mm.group(2)}"
                    # 보정했다는 사실과 원문 표기를 남긴다. 운영진이 "원문
                    # 정오 확인은 제공하지 않고 해석은 팀의 설계 판단"이라고
                    # 밝혔으므로, 값은 계산 가능한 형태로 두되 근거(원문이
                    # 실제로 어떻게 찍혀 있었는지)를 잃지 않게 한다 -
                    # 답변할 때 "원문은 X로 표기, 오타로 판단해 Y로 봄"을
                    # 밝힐 수 있어야 채점의 정확성/근거 기준을 둘 다 만족한다.
                    corrections.append({
                        "field": "total_fee_and_cost",
                        "raw": v, "used": fixed,
                        "reason": "소수점이 쉼표로 표기된 것으로 판단"
                                  f"(같은 행 총보수 {ref} 대비)",
                    })
                    return fixed
                return v

            rows.append({
                "class_code": code,
                "sales_commission_desc": desc,
                "total_fee": total_fee.replace(" ", "").rstrip("%"),
                "distribution_fee": clean("distribution_fee"),
                "peer_avg_fee": clean("peer_avg_fee"),
                "total_fee_and_cost": fix_comma_decimal(
                    clean("total_fee_and_cost"),
                    total_fee.replace(" ", "").rstrip("%")),
                "cost_projection_per_10m": cost,
                "page": page_num,
                "evidence": f"클래스명: {label} | {raw_comm or '-'} "
                            + " ".join(v for _, v in sorted(r["cells"].items())),
                "method": "cell_grid",
                "confidence": 1.0,
                "product_code": doc_id,
                **({"source_corrections": corrections} if corrections else {}),
            })
    return rows


def _summary_cells_lose_anything(coord_rows, cell_rows):
    """셀 결과가 좌표 결과에 비해 무엇이든 잃었는지 본다(클래스든 개별
    필드든). 처음엔 클래스 목록만 비교했는데, 클래스는 다 나오면서 특정
    칸만 비는 경우를 못 걸렀다 - 헤더 이름 변형을 못 알아본 문서에서
    총보수·비용/판매보수/동종유형총보수가 통째로 None이 되거나, "1년"
    비용예시만 빠지는 일이 실제로 있었다(실측 137건). 6축 값이 조용히
    사라지는 게 가장 나쁘므로, 하나라도 잃으면 좌표 결과를 쓴다."""
    cell = {r["class_code"]: r for r in cell_rows if r.get("class_code")}
    for c in coord_rows:
        code = c.get("class_code")
        if not code:
            continue
        # 좌표 방식이 만든 쓰레기 행(class_code가 "-"처럼 글자·숫자가
        # 하나도 없는 것)은 재현 대상이 아니다 - 이걸 "잃었다"고 보면
        # 멀쩡한 문서가 폴백된다(KR5116501001 실측).
        if not any(ch.isalnum() for ch in code):
            continue
        n = cell.get(code)
        if n is None:
            return True
        # 판매수수료 문구는 셀 쪽이 더 정확한 경우가 있어(좌표 방식이
        # 옆 칸을 잘못 읽어 "-"로 넣은 사례 실측) 값이 달라지는 것 자체는
        # 허용하고, 있던 게 사라지는 것만 막는다.
        if c.get("sales_commission_desc") is not None and n.get("sales_commission_desc") is None:
            return True
        # 숫자 4개는 이 표에서 그대로 재현돼야 한다 - 값이 달라지면
        # 어느 쪽이 맞는지 여기서 알 수 없으므로 안전하게 좌표 결과를
        # 쓴다(실측: 소수점이 쉼표로 찍힌 문서에서 좌표 방식은 보정을
        # 했는데 셀은 원문 "1,807"을 그대로 읽었다 - KR5169950018).
        for f in ("total_fee", "distribution_fee", "peer_avg_fee",
                  "total_fee_and_cost"):
            ov, nv = c.get(f), n.get(f)
            if ov is None:
                continue
            if nv is None or str(ov).replace(",", "") != str(nv).replace(",", ""):
                return True
        oc = c.get("cost_projection_per_10m") or {}
        nc = n.get("cost_projection_per_10m") or {}
        if set(oc) - set(nc):
            return True
        # 값 자체가 달라진 경우도 잃은 것으로 본다(쉼표 정규화는 제외 -
        # "1,041"과 "1041"은 같은 값이다).
        for k, v in oc.items():
            if str(v).replace(",", "") != str(nc.get(k, "")).replace(",", ""):
                return True
    return False


_SUMMARY_FALLBACK_DOCS = set()


def process_doc(doc_id):
    pdf_candidates = glob.glob(os.path.join(DATA_DIR, doc_id, "*.pdf"))
    if not pdf_candidates:
        return []

    results = []
    with pdfplumber.open(pdf_candidates[0]) as pdf:
        pages = candidate_pages_for_doc(doc_id, len(pdf.pages))
        if not pages:
            return []

        valid_pages = [(p, pdf.pages[p - 1]) for p in pages if 1 <= p <= len(pdf.pages)]
        page_words_lines = {
            p: (page.extract_words(x_tolerance=2, keep_blank_chars=False), None)
            for p, page in valid_pages
        }
        for p in page_words_lines:
            w = page_words_lines[p][0]
            page_words_lines[p] = (w, cluster_lines(w))

        # 표가 여러 페이지에 걸쳐 있을 때, 이어지는 페이지에는 헤더가
        # 반복되지 않는 경우가 있다(KR5118420006 실측: 4페이지 헤더엔
        # "총보수ㆍ비용"이 있는데 이어지는 5페이지엔 헤더 없이 데이터 행만
        # 있음 - 페이지 단위로 다시 판별하면 5페이지 행만 이 칸이 없다고
        # 잘못 판단함). 같은 표는 모든 페이지에서 구조가 같으므로, 문서
        # 전체(모든 후보 페이지)에서 한 번이라도 헤더가 보이면 True로 본다.
        has_cost_column = any(
            page_has_cost_column_header(w, l) for w, l in page_words_lines.values()
        )
        # 비용예시가 3개년뿐인 문서(위 page_cost_projection_years 참고)도
        # 같은 이유로 문서 전체 후보 페이지를 같이 본다. 문서 안에 5개년
        # 표(정상)와 3개년 표(운용전환일 전/후 등)가 섞여 있을 수 있어,
        # 5개년이 하나라도 보이면 그쪽을 우선한다(더 안전한 기본값).
        detected_years = [page_cost_projection_years(w, l) for w, l in page_words_lines.values()]
        if any(y == ["1y", "2y", "3y", "5y", "10y"] for y in detected_years):
            doc_cost_years = ["1y", "2y", "3y", "5y", "10y"]
        elif any(y == ["1y", "2y", "3y"] for y in detected_years):
            doc_cost_years = ["1y", "2y", "3y"]
        else:
            doc_cost_years = None

        for page_num, page in valid_pages:
            next_page_lines = page_words_lines.get(page_num + 1)
            if next_page_lines:
                # 클래스명 닫는 괄호 조각이 다음 페이지 맨 앞 1줄이 아니라
                # 2줄째에 걸치는 경우도 있다(KR5113470030 실측: "프라인"/
                # "(C)"가 다음 페이지 첫 두 줄에 나뉘어 있음) - 다른 클래스의
                # 완전한 데이터 행을 만나기 전까지만 최대 3줄을 후보로 준다.
                head = []
                for hl in next_page_lines[1][:3]:
                    if sum(1 for w in hl if DECIMAL_RE.match(w["text"])) >= 3:
                        break
                    head.append(hl)
                next_page_head_lines = head
            else:
                next_page_head_lines = None
            rows = find_fee_rows_on_page(
                page, page_num, has_cost_column, next_page_head_lines, doc_cost_years
            )
            for r in rows:
                r["product_code"] = doc_id
                results.append(r)

        # 요약표를 셀 격자로도 읽어 대조한다. 셀 방식이 성공하면 그쪽을
        # 쓴다 - 좌표 방식은 "이 단어가 어느 칸인지"를 매번 추측해야 해서
        # 문서별 예외가 계속 붙었고, 실제로 옆 칸 값을 잘못 가져오는 오류도
        # 있었다(다른 표에서 실측). 셀 경계는 PDF가 직접 알려주는 정보라
        # 그런 착각이 안 생긴다. 셀 격자를 못 얻은 문서(표 테두리가 없는
        # 등)에서만 좌표 결과를 그대로 쓴다.
        # 셀 결과가 좌표 결과의 클래스를 하나도 잃지 않을 때만 채택한다.
        # 대부분 문서에선 셀 쪽이 같거나 더 완전하지만, 구조가 특수한
        # 문서에선 셀 격자가 어긋난다 - 실측 두 가지:
        #   - 한 클래스가 운용전환일 전/후 두 행으로 나뉜 표에서 두 행을
        #     서로 다른 클래스로 오인(KR5147430065)
        #   - 같은 페이지의 수익률표/운용전문인력표가 같은 격자에 섞임
        #     (KR5123365001)
        # 이런 문서는 조용히 값이 틀리는 게 가장 나쁘므로, 손실이 감지되면
        # 좌표 결과를 그대로 둔다(폴백 문서 수는 main()에서 세어 출력한다).
        cell_rows = summary_rows_for_doc(doc_id, pdf, pages)
        if cell_rows and not _summary_cells_lose_anything(results, cell_rows):
            results = cell_rows
        else:
            _SUMMARY_FALLBACK_DOCS.add(doc_id)

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
    final_rows = list(dedup.values()) + unlabeled
    # total_fee_after_conversion이 있는 행에만 conversion_trigger_nav_price
    # 키를 붙인다(없는 행은 키 자체를 안 만듦 - 위 참고). 이 펀드 자신의
    # 기준가격이 이 값(원) 이상이 되면 운용전환이 일어난다는 뜻 - 고정
    # 날짜가 아니다.
    if any("total_fee_after_conversion" in r for r in final_rows):
        nav_price = conversion_trigger_nav_price(doc_id)
        for r in final_rows:
            if "total_fee_after_conversion" in r:
                r["conversion_trigger_nav_price"] = nav_price
    return final_rows


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
    detail_enriched = 0
    for doc_id in doc_ids:
        rows = process_doc(doc_id)
        if rows:
            docs_with_hits += 1
            if any(r["confidence"] < 0.7 for r in rows):
                docs_with_missing_class_code += 1
        # 요약표엔 없고 상세표("나.집합투자기구에 부과되는 보수 및 비용")
        # 에만 있는 클래스를 보강한다 - README "class_fees.json 코퍼스
        # 전체 완전성 문제" 참고. 요약표에서 뽑힌 클래스가 2개 미만이면
        # 대조 기준이 없어 조용히 그대로 넘어간다.
        before = len(rows)
        rows = enrich_with_detail_fee_table(doc_id, rows)
        if len(rows) > before:
            detail_enriched += 1
        # "나" 상세표 보강으로 새로 생긴 클래스들의 sales_commission_desc
        # null을 "가.투자자에게 직접 부과되는 수수료" 표에서 채운다(확실한
        # "없음"만 - 위 enrich_sales_commission_from_ga_table 주석 참고).
        rows = enrich_sales_commission_from_ga_table(doc_id, rows)
        # 출처 필드는 모든 행이 갖도록 맞춘다(class_returns.json과 같은
        # 규칙) - 합쳐진 행만 갖고 있으면 조회하는 쪽이 매번 존재 여부를
        # 따져야 한다. 보강 함수 안에서 하면 그 함수가 일찍 return하는
        # 문서(클래스가 1개뿐이라 대조 기준이 없는 KR5123365001 등)가
        # 빠지므로 여기서 한다.
        for r in rows:
            r.setdefault("source_pages", [r["page"]])
            r.setdefault("field_source_pages", {})
        all_rows.extend(rows)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(all_rows, f, ensure_ascii=False, indent=2)

    print(f"{len(all_rows)}개 클래스 레코드 ({docs_with_hits}개 문서) → {args.output}")
    print(f"클래스 코드 인식 실패(confidence<0.7): {docs_with_missing_class_code}개 문서")
    print(f"상세표 보강으로 클래스 추가된 문서: {detail_enriched}개")
    print(f"요약표 좌표 방식 폴백 문서: {len(_SUMMARY_FALLBACK_DOCS)}개 {sorted(_SUMMARY_FALLBACK_DOCS)}")


if __name__ == "__main__":
    main()
