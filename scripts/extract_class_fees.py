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


def find_fee_rows_on_page(page, page_num, has_cost_column):
    words = page.extract_words(x_tolerance=2, keep_blank_chars=False)
    lines = cluster_lines(words)

    rows = []
    for i, line in enumerate(lines):
        # decimals를 NUM_RE로 거른 뒤 다시 추리면 "1.18%"처럼 %가 붙은 토큰이
        # NUM_RE(퍼센트 미허용)에 애초에 안 걸려 통째로 빠진다 - line에서 직접
        # 따로 찾는다.
        decimals = [w for w in line if DECIMAL_RE.match(w["text"])]
        int_like = [w for w in line if NUM_RE.match(w["text"]) and w not in decimals]

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

        if len(decimals) < 3 or len(int_like) < 4:
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

        rows.append({
            "class_code": class_code,
            "sales_commission_desc": sales_commission_desc,
            "total_fee": total_fee["text"].rstrip("%"),
            "distribution_fee": distribution_fee["text"].rstrip("%"),
            "peer_avg_fee": peer_avg_fee_text,
            "total_fee_and_cost": total_fee_and_cost["text"].rstrip("%") if total_fee_and_cost else None,
            "cost_projection_per_10m": cost_projection,
            "page": page_num,
            # 클래스명("수수료미징구-오프라인-개인연금(C-P)")도 판매수수료
            # 문구("납입금액의 1%이내")도 데이터 줄 앞/뒤로 쪼개져 있는 경우가
            # 많아서, 이 행 자신의 줄만 담으면 "오프라인- 없음 ..."처럼 반
            # 토막만 보이고 class_code/sales_commission_desc가 실제로 어디서
            # 나왔는지 확인할 수 없다(사용자가 직접 원본과 대조하다 발견).
            # class_code/sales_commission_desc 판정에 실제로 쓰는 범위(앞뒤
            # 1줄 포함 window_text)를 그대로 evidence로 남긴다.
            "evidence": window_text,
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

        for page_num, page in valid_pages:
            rows = find_fee_rows_on_page(page, page_num, has_cost_column)
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
