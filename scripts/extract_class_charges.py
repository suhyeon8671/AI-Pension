"""투자자가 직접 부담하는 수수료와 클래스별 가입자격을 뽑는다.

투자설명서에는 "가. 투자자에게 직접 부과되는 수수료" 표가 있고, 여기에
지금까지 우리가 안 갖고 있던 것들이 한꺼번에 들어 있다.

    구분              가입자격            선취판매수수료   후취판매수수료  환매수수료  전환수수료
    종류A             제한 없음           납입금액의 1.0% 이내   -          -         -
    종류A-e           온라인 투자자        납입금액의 0.5% 이내   -          -         -
    종류C2(보수체감)   1년이상 종류C1가입자   -                 -          -         -

세 가지가 새로 생긴다.

1. 환매수수료. 지금 class_fees에는 칸 자체가 없었다. 100개 문서 전부
   '환매수수료'라는 말이 나오지만 대부분은 잡음이다 - 연혁표의 "환매수수료
   삭제", 용어집의 정의, 투자위험 설명. 실제 값은 이 표에만 있다.

2. 가입자격. class_meaning은 이름표의 속성(기관/고액/랩)으로 "일반 고객이
   살 수 있나"를 판정하는데, 이 표는 문서가 직접 "제한 없음" / "온라인
   투자자" / "1년이상 종류C1가입자"라고 적어 둔다. 추론이 아니라 원문이다.

3. 클래스 전환 관계. "1년이상 종류C1가입자"처럼 보수체감 클래스가 어떤
   조건으로 넘어가는지가 여기 적혀 있다.

값을 숫자로 바꾸지 않고 문장을 통째로 담는 이유:

    수수료미징구-오프라인-퇴직연금(C) | 90일미만 이익금의 30%.
        다만, 2013년1월17일 이후 환매 청구하는 경우에는 환매수수료를 부과하지 않음

숫자만 뽑으면 "90일 안에 팔면 이익금의 30%를 뗀다"가 되는데 실제로는 아무도
안 뗀다. 조건문을 값 하나로 줄이면 틀린 답이 된다.

실행:
    python3 scripts/extract_class_charges.py
    python3 scripts/extract_class_charges.py --check
"""

import argparse
import json
import os
import sqlite3

from extract_class_meaning import _parse, _squash

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "structured_store.db")
OUTPUT_JSON = os.path.join(REPO_ROOT, "class_charges.json")

# 열 이름 -> 우리가 쓸 이름. 헤더가 두 줄로 쪼개져 있어서(구분/가입자격/수수료율
# 밑에 선취판매수수료/후취판매수수료/환매수수료/전환수수료) 열마다 위아래를
# 이어 붙인 뒤 찾는다.
COLUMNS = (
    ("eligibility", ("가입자격",)),
    ("front_load_fee", ("선취판매", "선취")),
    ("back_load_fee", ("후취판매", "후취")),
    ("redemption_fee", ("환매수수료", "환매")),
    ("switch_fee", ("전환수수료", "전환")),
)

MAX_HEADER_ROWS = 5
# 헤더 칸은 이름표라 짧다. 이보다 길면 본문 문장으로 본다.
MAX_HEADER_CELL = 14
# "-"와 "없음"은 빈칸이 아니라 "이 수수료는 없다"는 답이다. 버리면
# "환매수수료 없습니다"라고 말할 수 있는 걸 "모릅니다"로 답하게 된다.
# 문서가 없다고 적은 것과 우리가 모르는 것은 다르다.
NONE_MARKS = {"-", "–", "—", "−", "없음", "해당없음", "해당사항없음", "미부과"}

# 헤더 글자가 데이터 칸으로 흘러드는 표가 있다(열이 밀린 경우).
# 값이 열 이름 그 자체면 값이 아니다.
HEADER_WORDS = {"선취판매수수료", "후취판매수수료", "환매수수료", "전환수수료",
                "가입자격", "구분", "수수료율", "매입시", "환매시"}


def _clean(v):
    v = " ".join((v or "").split())
    if not v or _squash(v) in HEADER_WORDS:
        return None
    return "없음" if _squash(v) in NONE_MARKS else v


def _header_map(rows):
    """열 번호 -> 우리가 쓸 이름. 못 찾으면 빈 dict.

    헤더 줄은 "환매수수료"가 칸 하나로 짧게 들어 있는 줄로 찾는다.
    처음엔 '가입자격'이나 '선취' 같은 말이 있는 줄을 다 헤더로 봤는데,
    표 첫 줄의 본문 문장("...가입자격에 따라 수수료가 다릅니다")이 걸려서
    엉뚱한 열이 가입자격으로 잡혔다. 헤더 칸은 문장이 아니라 이름표라
    짧다는 점을 쓴다."""
    ncols = max((len(r) for r in rows), default=0)
    if not ncols:
        return {}, 0

    # 표 위쪽에 설명 문단이 여러 줄 붙는 문서가 많아서 앞부분만 보면
    # 헤더를 놓친다. 전체를 훑는다.
    anchor = None
    for i, row in enumerate(rows):
        for cell in row:
            s = _squash(cell or "")
            if s.startswith("환매수수료") and len(s) <= MAX_HEADER_CELL:
                anchor = i
                break
        if anchor is not None:
            break
    if anchor is None:
        return {}, 0

    # 헤더는 그 줄과 바로 윗줄까지만 본다(구분/가입자격/수수료율 밑에
    # 선취판매수수료/후취판매수수료/환매수수료/전환수수료가 오는 두 줄 구조).
    joined = [""] * ncols
    for row in rows[max(0, anchor - 1): anchor + 1]:
        for j, cell in enumerate(row[:ncols]):
            s = _squash(cell or "")
            if len(s) <= MAX_HEADER_CELL:
                joined[j] += s

    mapping = {}
    used = set()
    for name, keys in COLUMNS:
        for j, h in enumerate(joined):
            if j in used or not h:
                continue
            if any(k in h for k in keys):
                mapping[j] = name
                used.add(j)
                break
    return mapping, anchor + 1


def _row_class_code(cell):
    """첫 칸에서 클래스 코드를 읽는다.

    "종류A\\n수수료선취-\\n오프라인" 이나 "수수료미징구-오프라인-퇴직연금(C)"
    같은 모양이라, class_meaning에서 쓰는 이름표 파서를 그대로 쓴다."""
    found = _parse(cell or "")
    if len(found) == 1:
        return next(iter(found))
    return None


def _parse_table(rows):
    mapping, header_end = _header_map(rows)
    if not mapping or "redemption_fee" not in mapping.values():
        return {}
    # 열 이름이 첫 칸(클래스 칸)에만 걸린 표는 수수료 표가 아니다.
    if set(mapping) == {0}:
        return {}

    order = [name for _j, name in sorted(mapping.items())]
    out = {}
    for row in rows[header_end:]:
        if not row:
            continue
        code = _row_class_code(row[0])
        if not code or code in out:
            continue

        rec = {}
        for j, name in mapping.items():
            if j < len(row):
                v = _clean(row[j])
                if v:
                    rec[name] = v

        # 헤더 칸 번호와 데이터 칸 번호가 어긋난 표가 많다(헤더는 4/7/10,
        # 데이터는 3/6에 들어 있는 식). 열 번호로 아무것도 못 읽었으면
        # 값이 있는 칸을 왼쪽부터 순서대로 열 이름에 맞춘다.
        if not rec:
            raw = [x for x in row[1:] if (x or "").strip()]
            for name, v in zip(order, raw):
                cv = _clean(v)
                if cv:
                    rec[name] = cv
        if rec:
            out[code] = rec
    return out


def extract(db_path=DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT product_code FROM class_fees WHERE product_code IS NOT NULL")]

    out = []
    for code in codes:
        merged, pages = {}, {}
        for page, dj in conn.execute(
                "SELECT page, data_json FROM tables WHERE doc_id = ? "
                "AND row_text LIKE '%환매수수료%' ORDER BY page", (code,)):
            try:
                rows = json.loads(dj)
            except (ValueError, TypeError):
                continue
            for cc, rec in _parse_table(rows).items():
                if cc not in merged:
                    merged[cc] = rec
                    pages[cc] = page
        for cc, rec in sorted(merged.items()):
            out.append({
                "product_code": code,
                "class_code": cc,
                "eligibility": rec.get("eligibility"),
                "front_load_fee": rec.get("front_load_fee"),
                "back_load_fee": rec.get("back_load_fee"),
                "redemption_fee": rec.get("redemption_fee"),
                "switch_fee": rec.get("switch_fee"),
                "page": pages.get(cc),
            })
    conn.close()
    return out


def report(rows, db_path=DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    have = {}
    for pc, cc in conn.execute("SELECT product_code, class_code FROM class_fees"):
        have.setdefault(pc, set()).add(cc)
    conn.close()

    got = {}
    for r in rows:
        got.setdefault(r["product_code"], set()).add(r["class_code"])
    total = sum(len(v) for v in have.values())
    matched = sum(len(v & got.get(pc, set())) for pc, v in have.items())

    print(f"수수료·가입자격 {len(rows)}건 / 상품 {len(got)}개")
    print(f"class_fees의 클래스 {total}개 중 채워진 것: {matched}개 "
          f"({matched * 100 // max(total, 1)}%)")
    for field in ("eligibility", "redemption_fee", "front_load_fee",
                  "back_load_fee", "switch_fee"):
        n = sum(1 for r in rows if r.get(field))
        print(f"  {field}: {n}건")
    charged = [r for r in rows if r.get("redemption_fee")]
    print(f"\n환매수수료가 실제로 적힌 클래스 {len(charged)}건 "
          f"({len({r['product_code'] for r in charged})}개 상품)")
    for r in charged[:5]:
        print(f"    {r['product_code']} {r['class_code']}: {r['redemption_fee'][:90]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument("--check", action="store_true")
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
