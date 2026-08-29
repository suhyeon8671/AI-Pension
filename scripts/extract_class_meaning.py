"""클래스 코드가 무슨 뜻인지 문서에서 뽑는다.

왜 필요한가
-----------
지금 답변은 "총보수(C클래스) 0.544%"처럼 클래스 코드를 그대로 내보낸다.
고객은 C가 뭔지 모른다. 그런데 클래스를 안 밝히면 답이 아예 틀린 게 되는데,
한 펀드 안에서 총보수가 중앙값 0.4%p, 최대 1.5%p(0.7% <-> 2.2%)까지 벌어지기
때문이다.

그렇다고 "대표 클래스"를 정해 둘 수도 없다. 상품 100개의 클래스 구성이
70가지로 제각각이라, 어떤 코드를 대표로 잡아도 최소 30개 상품에는 그 코드가
아예 없다.

무엇보다 코드의 뜻이 운용사마다 다르다:

    미래에셋   C-P  = 개인연금        C-P2 = 퇴직연금
    교보악사   CP   = 퇴직연금        C-P2 = 개인연금     (정반대)
    한국투자   C    = 개인연금        C-R  = 퇴직연금

코드로 뜻을 짐작하면 연금계좌에 못 담는 클래스를 담을 수 있다고 답하게 되고,
비교할 때도 개인연금과 보수체감을 나란히 놓고 "같은 클래스끼리 비교했다"고
표시하게 된다. 그래서 문서가 직접 적어 둔 이름표를 그대로 읽는다.

이름표는 투자설명서 앞부분 "집합투자기구의 명칭" 표에 있고, 형식이
`수수료방식-판매경로[-속성...]` 로 표준화되어 있다:

    수수료선취-오프라인(A)
    수수료미징구-온라인-개인연금(C-Pe)
    수수료미징구-오프라인-퇴직연금,기관(C-RF)
    종류C1(수수료미징구-오프라인-보수체감)      <- 순서가 뒤집힌 형식도 있다

이걸 뽑아 두면 세 가지가 한꺼번에 풀린다.
1. 코드 대신 말로 답할 수 있다("연금저축·창구 가입 기준").
2. 일반 고객이 못 사는 클래스(기관/고액/랩)를 답에서 뺄 수 있다. 교보악사
   Tomorrow장기우량은 제일 싼 A(0.1195%)가 고액자산가 전용이라, 이걸 모르면
   살 수도 없는 걸 "제일 싸다"고 안내하게 된다.
3. 비교를 코드가 아니라 뜻으로 맞출 수 있다.

실행:
    python3 scripts/extract_class_meaning.py
    python3 scripts/extract_class_meaning.py --check   # 커버리지만 확인
"""

import argparse
import json
import os
import re
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "structured_store.db")
OUTPUT_JSON = os.path.join(REPO_ROOT, "class_meaning.json")

FEE_TYPES = ("선취", "미징구", "후취")

# 판매경로. 긴 것부터 찾아야 "온라인슈퍼"가 "온라인"으로 잘리지 않는다.
CHANNELS = ("온라인슈퍼", "오프라인", "온라인", "직판")

# 속성 중 "이 계좌라야 살 수 있다"를 뜻하는 것
ACCOUNT_TYPES = ("개인연금", "퇴직연금", "주택마련", "금전신탁")

# 일반 개인 고객이 살 수 없는 클래스를 가려내는 말. 이게 붙어 있으면
# 보수가 아무리 싸도 답변에서 빼야 한다(살 수가 없으므로).
# "펀드"/"펀드등"은 이 펀드에 투자하는 다른 펀드(모집합투자기구)용이라는
# 뜻이라 개인이 살 수 없다. 빼먹었더니 미래에셋솔로몬중장기국공채의
# C-PI(0.13%)가 일반 클래스로 잡혀, 형제 펀드 비교에서 혼자 3분의 1 값으로
# 나왔다. 가입자격 원문도 "…자집합투자기구로 두고 있는 모집합투자기구"라고
# 적고 있다.
RESTRICTED = ("기관", "고액", "랩", "펀드", "금전신탁", "임직원", "협회")

# 같은 말이 "기관/기관형/기관등", "개인연금/개인연금형"처럼 조금씩 다르게
# 적혀 있어서 정확히 같은지로 보면 17개쯤을 놓친다. 놓치면 기관 전용
# 클래스가 "일반 가입 가능"으로 표시되므로 부분 일치로 본다.


def _match_any(attr, words):
    return any(w in attr for w in words)

# 공백을 다 지운 뒤 찾는다. PDF에서 라벨이 줄바꿈으로 잘리기 때문이다
# ("수수료미징구-오프라인-보\n수체감"). 라벨 안에는 원래 띄어쓰기가 없다.
#
# 그런데 표에서는 라벨이 숫자 칸에 통째로 끊기기도 한다:
#     수수료선취-  납입금액의0.17%이내  0.1736 0.10 ...  오프라인(A)
# 그래서 "수수료방식-채널(코드)"를 한 덩어리로 보는 정규식으로는 못 잡는다.
# 수수료방식을 찾은 뒤 그 뒤쪽을 훑어 채널을 만나면 거기서 코드를 읽는다.
RE_FEE = re.compile(r"수수료(선취|미징구|후취)-")
# 판매경로 뒤에 "형"을 붙여 쓰는 문서가 있다("수수료미징구-오프라인형(C)").
# 이걸 안 넘기면 코드를 못 읽어 그 문서의 이름표를 통째로 놓친다.
RE_CHANNEL = re.compile(r"(온라인슈퍼|오프라인|온라인|직판)형?")
# 속성은 1글자짜리도 있다("랩"). {2,8}로 잡았더니 "-랩,펀드등"이 통째로
# 떨어져 나가서, 랩 전용인 F클래스가 "일반 고객도 가입 가능"으로 표시됐다.
RE_ATTRS = re.compile(r"^((?:[-,][가-힣]{1,8})*)")
# 채널 뒤에 바로 붙는 (코드) -> 라벨이 먼저 나오는 형식
RE_CODE_AFTER = re.compile(r"^\(([A-Za-z0-9][A-Za-z0-9\-]{0,12}(?:\([^)]{1,10}\))?)\)")
# 코드가 먼저 나오는 형식: "종류C1(수수료...오프라인)" / "A(수수료...오프라인)"
RE_CODE_BEFORE = re.compile(r"(?:종류)?([A-Za-z0-9][A-Za-z0-9\-]{0,12})\($")
# 괄호가 아예 없는 형식: "종류C2수수료미징구-오프라인-보수체감98295"
# (투자설명서 앞쪽 "집합투자기구의 명칭" 표가 이 모양이다)
RE_CODE_BEFORE_BARE = re.compile(r"종류([A-Za-z0-9][A-Za-z0-9\-]{0,12})$")

# 수수료방식과 채널 사이에 끼어들 수 있는 글자 수. 표에서 숫자 칸이
# 통째로 들어오므로 넉넉히 잡되, 다음 클래스 라벨까지 넘어가지 않게 제한한다.
_GAP = 150


def _squash(text):
    return re.sub(r"\s+", "", text or "")


def _split_label(body):
    """'오프라인-퇴직연금,기관' -> ('오프라인', ['퇴직연금', '기관'])"""
    channel = None
    for ch in CHANNELS:
        if body.startswith(ch):
            channel = ch
            body = body[len(ch):]
            break
    rest = [p for p in re.split(r"[-,]", body) if p]
    return channel, rest


def _parse(text):
    """한 덩어리 글에서 (코드, 수수료방식, 판매경로, 속성들)을 모두 찾는다."""
    t = _squash(text)
    found = {}

    for fee in RE_FEE.finditer(t):
        fee_type = fee.group(1)
        tail = t[fee.end(): fee.end() + _GAP]
        ch = RE_CHANNEL.search(tail)
        if not ch:
            continue
        channel = ch.group(1)
        after = tail[ch.end():]
        attrs_raw = RE_ATTRS.match(after).group(1)
        rest = after[len(attrs_raw):]

        code = None
        m = RE_CODE_AFTER.match(rest)
        if m:
            code = m.group(1)
        elif rest.startswith(")"):
            # 코드가 앞에 있는 형식: "종류C1(수수료미징구-오프라인-보수체감)"
            m = RE_CODE_BEFORE.search(t[: fee.start()])
            if m:
                code = m.group(1)
        else:
            # 괄호 없는 형식: "종류C2수수료미징구-오프라인-보수체감"
            m = RE_CODE_BEFORE_BARE.search(t[: fee.start()])
            if m:
                code = m.group(1)
        if not code:
            continue
        code = code.strip("-")
        if not code or code in found:
            continue

        attrs = [p for p in re.split(r"[-,]", attrs_raw) if p]
        body = channel + attrs_raw
        found[code] = {
            "class_code": code,
            "fee_type": fee_type,
            "channel": channel,
            "attributes": attrs,
            "raw_label": f"수수료{fee_type}-{body}",
        }
    return found


def _describe(rec):
    """고객에게 보여 줄 말. 코드 대신 이걸 쓴다."""
    account = next((a for a in rec["attributes"] if _match_any(a, ACCOUNT_TYPES)), None)
    channel = {"오프라인": "창구", "온라인": "온라인",
               "온라인슈퍼": "온라인슈퍼", "직판": "운용사 직판"}[rec["channel"]]
    if account and "개인연금" in account:
        account = "연금저축"
    elif account and "퇴직연금" in account:
        account = "퇴직연금(DC/IRP)"
    parts = [p for p in (account, channel) if p]
    # 계좌 종류도 제한도 아닌 속성(보수체감/무권유저비용 등)도 붙인다.
    # 안 붙이면 C1~C4가 전부 "창구"로 똑같이 보여서, 보수가 왜 다른지
    # 알 수 없는 답이 된다.
    others = [a for a in rec["attributes"]
              if not _match_any(a, ACCOUNT_TYPES) and not _match_any(a, RESTRICTED)
              and a != rec["channel"]]
    parts.extend(others)
    limits = [a for a in rec["attributes"] if _match_any(a, RESTRICTED)]
    if limits:
        parts.append("/".join(limits) + " 전용")
    return " · ".join(parts)


def extract(db_path=DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT product_code FROM class_fees WHERE product_code IS NOT NULL")]

    out = []
    for code in codes:
        merged, pages = {}, {}
        # 표에서 먼저 찾고, 못 찾으면 본문 청크에서 찾는다. 이름표 표가
        # 표로 안 잡힌 문서가 있다.
        for sql in ("SELECT page, row_text AS t FROM tables WHERE doc_id = ?",
                    "SELECT page, text AS t FROM chunks WHERE doc_id = ?"):
            for row in conn.execute(sql, (code,)):
                for cc, rec in _parse(row["t"]).items():
                    if cc not in merged:
                        merged[cc] = rec
                        pages[cc] = row["page"]
            if merged:
                break

        for cc, rec in sorted(merged.items()):
            out.append({
                "product_code": code,
                "class_code": cc,
                "fee_type": rec["fee_type"],
                "channel": rec["channel"],
                "account_type": next(
                    (a for a in rec["attributes"]
                     if _match_any(a, ACCOUNT_TYPES)), None),
                "attributes": rec["attributes"],
                "retail": not any(_match_any(a, RESTRICTED)
                                  for a in rec["attributes"]),
                "description": _describe(rec),
                "raw_label": rec["raw_label"],
                "page": pages.get(cc),
            })
    conn.close()
    return out


def report(rows, db_path=DEFAULT_DB_PATH):
    """뽑은 이름표가 실제 class_fees의 클래스를 얼마나 덮는지."""
    conn = sqlite3.connect(db_path)
    have = {}
    for pc, cc in conn.execute("SELECT product_code, class_code FROM class_fees"):
        have.setdefault(pc, set()).add(cc)
    conn.close()

    got = {}
    for r in rows:
        got.setdefault(r["product_code"], set()).add(r["class_code"])

    total = matched = 0
    no_label = []
    for pc, classes in have.items():
        total += len(classes)
        m = len(classes & got.get(pc, set()))
        matched += m
        if not got.get(pc):
            no_label.append(pc)
    print(f"이름표 {len(rows)}건 / 상품 {len(got)}개")
    print(f"class_fees의 클래스 {total}개 중 뜻을 아는 것: {matched}개 "
          f"({matched * 100 // max(total, 1)}%)")
    if no_label:
        print(f"이름표를 하나도 못 찾은 상품: {len(no_label)}개 {no_label[:6]}")
    restricted = [r for r in rows if not r["retail"]]
    print(f"일반 고객이 살 수 없는 클래스: {len(restricted)}건 "
          f"(예: {[(r['product_code'], r['class_code'], r['description']) for r in restricted[:3]]})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument("--check", action="store_true", help="저장하지 않고 커버리지만")
    args = ap.parse_args()

    rows = extract(args.db)
    report(rows, args.db)
    if args.check:
        return
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"→ {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
