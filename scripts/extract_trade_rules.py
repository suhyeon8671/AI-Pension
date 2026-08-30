"""매입·환매할 때 어느 날 기준가격이 적용되고 돈이 언제 들어오는지 뽑는다.

    "17시 50분에 환매 청구하면 어떻게 되나요?"
    "판 돈은 언제 들어와요?"

고객이 실제로 많이 하는 질문인데 지금은 아예 답하지 못한다. 문서에는
89/100, 88/100에 들어 있다.

    (2) 매입청구시 적용되는 기준가격
    (가) 15시 30분 이전에 자금을 납입한 경우 : 납입일로부터 제2영업일에
         공고되는 기준가격을 적용
    (나) 15시 30분 경과 후 자금을 납입한 경우 : 납입일로부터 제3영업일에
         공고되는 기준가격을 적용

    (3) 환매청구시 적용되는 기준가격
    (가) 15시 30분 이전에 환매를 청구한 경우 : 환매청구일로부터 제2영업일에
         공고되는 기준가격을 적용하여 제4영업일에 관련세금등을 공제한 후
         환매대금을 지급합니다.

숫자로 바꾸지 않고 문장을 통째로 담는다. "제2영업일"만 뽑으면 그게 몇 시
기준인지, 지급은 언제인지가 날아간다. 기준시각도 문서마다 다르다
(15시 30분 / 오후 5시 / 17시). 조건문을 값 하나로 줄이면 틀린 답이 된다.

실행:
    python3 scripts/extract_trade_rules.py
    python3 scripts/extract_trade_rules.py --check
"""

import argparse
import functools
import glob
import json
import os
import re
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "structured_store.db")
OUTPUT_JSON = os.path.join(REPO_ROOT, "trade_rules.json")

# 뽑을 절과 그 절을 여는 말. 문서마다 표현이 조금씩 다르다.
SECTIONS = (
    ("매입기준가", (r"매입청구\s*시?\s*적용되는\s*기준가격",
                    r"매입\s*시?\s*적용\s*기준가격",
                    r"매입청구\s*시?\s*기준가격")),
    ("환매기준가", (r"환매청구\s*시?\s*적용되는\s*기준가격",
                    r"환매\s*시?\s*적용\s*기준가격",
                    r"환매청구\s*시?\s*기준가격")),
)

# 절이 끝나는 자리. 다음 번호 항목이나 새 제목이 나오면 거기까지다.
# "가."/"나." 같은 항목 표시는 앞에 공백이 와야 한다. 그 조건이 없으면
# "지급합니다. (나) ..."의 "다."가 절 제목으로 잡혀서 (나) 항목이 통째로
# 잘려 나간다 - 15시 30분 경과 후에 어떻게 되는지가 사라진다.
RE_SECTION_END = re.compile(
    r"\([0-9]\)|(?<=\s)[나-하]\.\s|제\s*\d+\s*부|■|◆|◇|【")

# 항목 표시 뒤가 시각 조건이면 그건 새 절이 아니라 같은 규칙의 두 번째
# 갈래다. 매입/환매를 "가./나."로 나눠 놓고 그 안의 시각 갈래도 다시
# "가./나."로 쓰는 문서가 있어서(KR5144420020 실측), 표시만 보고 자르면
# "5시 넘겨서 넣으면 어떻게 되나"가 통째로 사라진다.
RE_TIME_BRANCH = re.compile(r"\s*(?:오전|오후)?\s*\d+\s*시")

# 쪽 아래 머리글이 규칙 뒤에 딸려 오는 문서가 있다
# ("... 환매대금을 지급합니다. 30 NH-Amundi Asset Management").
# 우리말 규칙 문장 뒤에 쪽번호와 로마자만 남으면 그건 머리글이다.
# 쪽번호만 덩그러니 남기도 한다("... 기준가격을 적용 24"). 규칙 문장이
# 숫자로 끝나는 일은 없으니(늘 "적용/매입/지급합니다"로 끝난다) 떼도 된다.
RE_PAGE_FOOTER = re.compile(
    r"\s\d{1,3}(?:\s+[A-Za-z][\w.\-]*){0,5}\s*$")


def _drop_diagram_junk(body):
    """도표 안 글자가 한 자씩 흩어져 딸려 오는 문서가 있다(KR5118420006
    실측: "기준가격 ( 자 1 7 금 시 D 납 이 입 전 ) 수 기 익 준 D 증 가"
    - 매입 흐름을 그린 그림의 글자다). 한 글자 토막이 대여섯 개 넘게
    이어지면 문장이 아니라 도표 잔해다."""
    out, run = [], []
    for tk in body.split():
        if len(tk) == 1:
            run.append(tk)
            continue
        if len(run) <= 5:
            out.extend(run)
        run = []
        out.append(tk)
    if len(run) <= 5:
        out.extend(run)
    return " ".join(out)


def _trim(body):
    # 앞머리의 "·"/"-"는 갈래를 여는 글머리표라 남긴다 - 떼면 첫 갈래만
    # 표시가 없어져 둘째 갈래와 짝이 안 맞아 보인다.
    body = RE_PAGE_FOOTER.sub("", body)
    return _drop_diagram_junk(body).rstrip(" :：-·").strip()


def _running_headers(texts):
    """쪽마다 되풀이되는 머리글(문서 제목 줄)을 찾는다.

    쪽을 넘어가는 규칙을 이어 붙이면 이 머리글이 문장 한가운데로 끼어든다
    ("... 환매대금을 지급합니다. NH-Amundi 필승 코리아 증권투자신탁[주식]
    나. 오후 3시 30분 경과 후 ..."). 여러 조각의 첫 줄에 똑같이 나오는
    줄이 곧 머리글이다."""
    seen = {}
    for t in texts:
        line = (t or "").strip().split("\n")[0].strip()
        if 4 <= len(line) <= 60:
            seen[line] = seen.get(line, 0) + 1
    return {ln for ln, n in seen.items() if n >= 3}


def _strip_furniture(text, heads):
    """조각 하나에서 머리글·쪽번호를 덜어낸다(이어 붙이기 전에)."""
    flat = _flat(text)
    for h in heads:
        flat = flat.replace(_flat(h), " ")
    return RE_PAGE_FOOTER.sub("", _flat(flat)).strip()


def _join(a, b, max_overlap=200):
    """조각끼리 끝과 앞이 겹쳐 있는 문서가 있다. 그냥 이으면 같은 문장이
    두 번 나온다 - 겹치는 만큼 덜고 잇는다."""
    if not a:
        return b
    if not b:
        return a
    for n in range(min(len(a), len(b), max_overlap), 7, -1):
        if a[-n:] == b[:n]:
            return a + b[n:]
    return f"{a} {b}"

# 이만큼 넘으면 절 하나가 아니라 여러 절을 삼킨 것이다.
MAX_SECTION_CHARS = 600
# 청크 경계에서 잘린 규칙을 되붙일 때 한 번에 이어 보는 조각 수.
CHUNK_WINDOW = 3
# 온전한 규칙에는 기준시각과 며칠 뒤인지가 둘 다 있다. 하나라도 없으면
# 청크 경계에서 잘린 것이다("15시 30분 이전에 자금을 납입한 경우 :
# 납입일로부터" 하고 끊긴 것을 담고 있었다). 그럴 땐 다른 데서 다시 찾는다.
RE_HAS_TIME = re.compile(r"\d+\s*시")
RE_HAS_DAYS = re.compile(r"영업일|D\s*\+\s*\d")


def _is_complete(body):
    return bool(RE_HAS_TIME.search(body) and RE_HAS_DAYS.search(body))


# 단을 갈라 읽으면 줄이 바뀐 자리에 공백이 생긴다("제3영 업일에").
# 흔한 모양만 되붙인다.
RE_WRAPPED_DAY = re.compile(r"제\s*(\d+)\s*영\s+업일")


def _flat(text):
    out = " ".join((text or "").split())
    return RE_WRAPPED_DAY.sub(r"제\1영업일", out)


def _section(text, patterns):
    """절을 열고 그 안의 글을 다음 절 직전까지 잘라 온다."""
    flat = _flat(text)
    for pat in patterns:
        m = re.search(pat, flat)
        if not m:
            continue
        # 절 제목이 문장의 주어인 문서가 있다("매입청구시 적용되는
        # 기준가격은 아래와 같으며, ..."). 제목 뒤부터만 담으면 "은 아래와
        # 같으며,"로 시작하는 토막이 된다 - 그럴 땐 제목까지 같이 담는다.
        start = m.end()
        if flat[start: start + 1] in ("은", "는", "이", "가"):
            start = m.start()
        body = flat[start: start + MAX_SECTION_CHARS]
        pos = 4  # 바로 뒤 "(가)"는 넘긴다
        while True:
            end = RE_SECTION_END.search(body, pos)
            if not end:
                break
            if RE_TIME_BRANCH.match(body, end.end()):
                pos = end.end()
                continue
            body = body[: end.start()]
            break
        body = _trim(body)
        if len(body) >= 20 and _is_complete(body):
            return body
    return None


# 매입/환매 규칙을 좌우 2단 표로 싣는 문서가 18개 있다. 본문 글자로
# 읽으면 두 단이 한 줄씩 번갈아 섞여서 알아볼 수가 없다.
#
#   ㆍ오후 5시 이전 자금을 납입한 경우 : 자금   ㆍ오후5시 이전 환매를 청구한
#   을 납입한 영업일의 다음 영업일(D+1)에      경우 : 환매를 청구한 날로부터
#
# 표의 칸으로 보면 제대로 나뉘어 있다.
#
#   ['매입 방법', '<매입 규칙>', '환매 방법', '<환매 규칙>']
CELL_LABELS = {"매입방법": "매입기준가", "환매방법": "환매기준가",
               "매입": "매입기준가", "환매": "환매기준가"}


# 두 갈래 중 뒤엣것("경과 후"/"이후"/"초과")이 있는지. 앞 갈래만 담고
# 끝내면 "17시 50분에 청구하면요?"에 엉뚱한 답을 하게 된다.
RE_SECOND_BRANCH = re.compile(r"경과\s*후|이\s*후|초과")


def _has_both_branches(body):
    # 둘째 갈래를 열어 놓고 며칠 뒤인지는 안 적힌 채 끝나는 조각이 있다
    # (KR5118420006 실측: "... 매입 오후 5시 경과 후 자금을 납입한 경우"
    # 에서 끊긴다). 여는 말만 있는 건 없느니만 못하다 - 열었으면 며칠
    # 뒤인지까지 있어야 온전한 규칙이다.
    m = RE_SECOND_BRANCH.search(body)
    return bool(m and RE_HAS_DAYS.search(body, m.end()))


def _done(found):
    """두 절을 다 찾았고 둘 다 갈래가 온전한가."""
    return all(kind in found and _has_both_branches(found[kind][0])
               for kind, _pats in SECTIONS)


def _continuation(rows, ri, col, body):
    """규칙의 두 번째 갈래가 아랫줄 칸에 따로 놓인 표가 있다
    (KR5174420011 실측: '17시 이전...'과 '17시 경과 후에...'가 라벨 없는
    두 줄로 나뉘어 있어 앞 갈래만 담고 있었다). 같은 열의 다음 줄이
    시각 조건으로 시작하면 같은 규칙의 이어지는 갈래로 본다."""
    if _has_both_branches(body):
        return body
    for row in rows[ri + 1:]:
        cells = [(x or "").strip() for x in row]
        if any(CELL_LABELS.get(_flat(c).replace(" ", "")) for c in cells):
            break
        nxt = _flat(cells[col]) if col < len(cells) else ""
        if not nxt:
            continue
        if RE_TIME_BRANCH.match(nxt) and RE_HAS_DAYS.search(nxt):
            return f"{body} {nxt}"
        break
    return body


def _from_cells(rows):
    """표의 '매입 방법 | 규칙' 짝에서 규칙을 읽는다."""
    out = {}
    for ri, row in enumerate(rows):
        cells = [(x or "").strip() for x in row]
        for i, cell in enumerate(cells):
            kind = CELL_LABELS.get(_flat(cell).replace(" ", ""))
            if not kind or kind in out:
                continue
            for j in range(i + 1, len(cells)):
                if not cells[j].strip():
                    continue
                body = _trim(_flat(cells[j]))
                if len(body) >= 20 and _is_complete(body):
                    out[kind] = _continuation(rows, ri, j, body)[:MAX_SECTION_CHARS]
                break
    return out


# 매입/환매를 좌우 2단으로 싣되 표로도 안 잡히는 문서가 8개 있다(KR5127 계열).
# 글자만 보면 두 단이 줄 단위로 번갈아 나오고 한쪽이 두 줄로 쪼개져서,
# 어느 줄이 어느 단의 이어지는 부분인지 짐작할 수가 없다. 짐작으로 붙였더니
# "제3영" + "4영업일에" = "제3영4영업일에" 같은 엉터리가 나왔다.
#
# 좌표를 보면 답이 분명하다. 매입은 x 112~260, 환매는 x 383~540에 있다.
# "환매방법" 라벨의 x를 경계로 삼아 두 단을 갈라 읽는다.
LABEL_X = {"매입방법": "매입기준가", "환매방법": "환매기준가"}
# 좌표로 단을 갈라 읽으면 위아래 다른 절(투자위험 설명 등)도 딸려 온다.
# 시각 조건으로 시작해서 "매입" 또는 "대금 지급"으로 끝나는 대목만 추린다.
RE_RULE_SPAN = re.compile(
    r"\d+\s*시(?:\s*\d+\s*분)?\s*(?:이전|경과\s*후|이후)\s*:?.{0,70}?"
    r"(?:으로\s*매입|대금\s*지급)")
# 라벨 줄에서 위아래로 이만큼 안에 있는 글자만 본다(다른 절을 삼키지 않게).
COLUMN_ROW_WINDOW = 80


def _from_pdf_columns(pdf_path):
    """2단으로 놓인 매입/환매 규칙을 x좌표로 갈라 읽는다."""
    import pdfplumber

    out = {}
    with pdfplumber.open(pdf_path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            if "매입방법" not in text or "환매방법" not in text:
                continue
            words = page.extract_words()
            labels = {}
            for w in words:
                key = w["text"].replace(" ", "")
                if key in LABEL_X and key not in labels:
                    labels[key] = w
            if len(labels) < 2:
                continue
            split_x = labels["환매방법"]["x0"]
            base_top = labels["매입방법"]["top"]

            cols = {"매입기준가": [], "환매기준가": []}
            for w in words:
                if abs(w["top"] - base_top) > COLUMN_ROW_WINDOW:
                    continue
                if w["text"].replace(" ", "") in LABEL_X:
                    continue
                kind = "매입기준가" if w["x0"] < split_x else "환매기준가"
                cols[kind].append(w)

            for kind, ws in cols.items():
                rows = {}
                for w in ws:
                    rows.setdefault(round(w["top"]), []).append(w)
                body = " ".join(
                    " ".join(x["text"] for x in sorted(v, key=lambda x: x["x0"]))
                    for _t, v in sorted(rows.items()))
                spans = RE_RULE_SPAN.findall(_flat(body))
                if not spans:
                    continue
                body = " / ".join(_flat(x) for x in spans)
                if _is_complete(body) and kind not in out:
                    out[kind] = (body[:MAX_SECTION_CHARS], pno)
            if len(out) == len(LABEL_X):
                break
    return out


def extract(db_path=DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT product_code FROM class_fees WHERE product_code IS NOT NULL")]

    out = []
    for code in codes:
        found = {}
        # 표의 칸을 먼저 본다. 예전엔 본문 글자를 먼저 봤는데, 같은 상품을
        # 두 방식으로 뽑아 견줘 보니 글자 쪽이 조건 하나를 통째로 빠뜨리고
        # 도표 잔해까지 딸려 오는 경우가 있었다.
        #
        #   글자: "(1) 오후 5시 이전에 ... 2영업일 ... T일 T+1일 자금납입일
        #          집합투자증권 매입일 (매입청구일) (기준가적용일)"
        #          <- 경과 후 조건이 없고 도표 글자가 섞였다
        #   셀  : "· 17시 이전 : 2영업일 기준가 매입
        #          · 17시 경과 후 : 3영업일 기준가 매입"
        #
        # 값이 서로 어긋나는 건 아니었지만(영업일 숫자는 같다) 셀 쪽이
        # 더 온전하고 짧아서 답변에 그대로 쓰기 좋다.
        for page, dj in conn.execute(
                "SELECT page, data_json FROM tables WHERE doc_id = ? ORDER BY page",
                (code,)):
            try:
                rows = json.loads(dj)
            except (ValueError, TypeError):
                continue
            for kind, body in _from_cells(rows).items():
                if kind not in found:
                    found[kind] = (body, page)
            if len(found) == len(SECTIONS):
                break

        # 표로 안 잡힌 문서는 본문 절을 읽는다(원래 서술형인 문서가 있다).
        # 칸에서 읽어 놓은 규칙이 갈래 하나뿐이면 그것도 여기서 다시 찾는다
        # - 갈래 하나만 담은 규칙은 없느니만 못하다.
        if not _done(found):
            for sql, col in (("SELECT page, text FROM chunks WHERE doc_id = ? ORDER BY page", 1),
                             ("SELECT page, row_text FROM tables WHERE doc_id = ? ORDER BY page", 1)):
                seq = list(conn.execute(sql, (code,)))
                heads = _running_headers([r[col] for r in seq])
                clean = [_strip_furniture(r[col], heads) for r in seq]
                for i, row in enumerate(seq):
                    # 규칙이 청크 경계에서 잘리는 문서가 있다(KR5144420020
                    # 실측: "나. 오후 5시 경과"에서 끊기고 나머지 "후에
                    # 자금을 납입한 경우..."는 다음 청크에 있다). 조각만
                    # 담으면 "5시 넘겨서 넣으면?"에 답할 수 없다. 이어지는
                    # 조각까지 붙여 놓고 읽는다. 조각들이 서로 겹쳐 있어서
                    # 규칙이 두 번째 조각에서 시작할 수도 있으니 둘이 아니라
                    # 셋을 붙인다.
                    text = functools.reduce(_join, clean[i: i + CHUNK_WINDOW])
                    for kind, patterns in SECTIONS:
                        if kind in found and _has_both_branches(found[kind][0]):
                            continue
                        body = _section(text, patterns)
                        if not body:
                            continue
                        if kind not in found or _has_both_branches(body):
                            found[kind] = (body, row[0])
                if _done(found):
                    break

        if not _done(found):
            pdfs = glob.glob(os.path.join(
                REPO_ROOT, "data", "products", code, "*.pdf"))
            for pdf_path in pdfs:
                try:
                    for kind, (body, page) in _from_pdf_columns(pdf_path).items():
                        if kind in found and _has_both_branches(found[kind][0]):
                            continue
                        if kind not in found or _has_both_branches(body):
                            found[kind] = (body, page)
                except Exception:
                    pass  # PDF를 못 읽으면 그냥 없는 대로 둔다
                if _done(found):
                    break

        for kind, (body, page) in found.items():
            out.append({
                "product_code": code,
                "kind": kind,
                "text": body,
                "page": page,
            })
    conn.close()
    return out


def report(rows):
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r["kind"], set()).add(r["product_code"])
    print(f"규칙 {len(rows)}건")
    for kind, codes in sorted(by_kind.items()):
        print(f"  {kind}: {len(codes)}개 상품")
    both = set.intersection(*by_kind.values()) if len(by_kind) > 1 else set()
    print(f"  둘 다 있는 상품: {len(both)}개")
    for r in rows[:3]:
        print(f"\n  [{r['product_code']} {r['kind']} p{r['page']}]\n    {r['text'][:200]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    rows = extract(args.db)
    report(rows)
    if args.check:
        return
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"\n→ {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
