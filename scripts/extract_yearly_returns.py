"""해마다 얼마를 벌었는지 뽑는다 ("나. 연도별 수익률 추이").

    "작년에 얼마 벌었어요?"
    "2023년에는 어땠나요?"

지금은 답하지 못한다. class_returns에는 연평균(누적) 수익률만 있어서
"최근 3년 -31.08%"까지만 말할 수 있는데, 그건 3년을 묶은 값이라
"작년 성과"와는 다른 것이다. 해마다의 값은 따로 실려 있다.

    나. 연도별 수익률 추이(단위:%)
    연도       최근 1년차   최근 2년차   최근 3년차   최근 4년차   최근 5년차
    (기간)     24.01.01~   23.01.01~   22.01.01~   21.01.01~   20.01.01~
               24.12.31    23.12.31    22.12.31    21.12.31    20.12.31
    종류A ...   -23.62      35.66      -31.08       18.99       33.38

연평균 표와 헷갈리면 안 된다. 문서에 따라 연평균 표도 "1년차"라고 쓴다.
가르는 법은 기간이다.

    연평균: 24.03.26~25.03.25 / 23.03.26~25.03.25 ... 끝나는 날이 같다
    연도별: 24.01.01~24.12.31 / 23.01.01~23.12.31 ... 끝나는 날이 다르다

값은 표의 칸에서 읽는다. 클래스 이름이 이름표째 들어 있어서
("종류A 수수료선취-오프라인") class_meaning의 파서를 그대로 쓴다.

실행:
    python3 scripts/extract_yearly_returns.py
    python3 scripts/extract_yearly_returns.py --check
"""

import argparse
import json
import os
import re
import sqlite3

from extract_class_meaning import _parse_row, _squash

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "structured_store.db")
OUTPUT_JSON = os.path.join(REPO_ROOT, "yearly_returns.json")

RE_YEAR_COL = re.compile(r"최근\s*(\d+)\s*년차")
# "24.01.01~ 24.12.31" 처럼 물결로 이은 기간
# 물결표를 문서마다 다르게 쓴다(~ ∼ 〜 ～ -). 아스키 물결만 받다가
# 상품 하나를 통째로 놓쳤다.
RE_PERIOD = re.compile(
    r"(\d{2,4}[.\-]\d{1,2}[.\-]\d{1,2})\s*[~∼〜～]\s*"
    r"(\d{2,4}[.\-]\d{1,2}[.\-]\d{1,2})")
RE_NUM = re.compile(r"^-?\d+(?:\.\d+)?$")

# 클래스 행이 아니라 펀드 전체/비교지수를 나타내는 이름
WHOLE_FUND = ("투자신탁", "집합투자기구", "펀드")
BENCHMARK = ("비교지수", "벤치마크", "BM")


def _clean_num(v):
    # "9.53 %"처럼 단위를 붙여 쓰는 표가 있다. 안 떼면 숫자로 못 읽는다.
    v = (v or "").replace(",", "").replace(" ", "").rstrip("%")
    return float(v) if RE_NUM.match(v) else None


def _year_columns(rows):
    """'최근 N년차' 열이 어디인지. (열번호 -> N)"""
    for i, row in enumerate(rows):
        cols = {}
        for j, cell in enumerate(row):
            m = RE_YEAR_COL.search(_squash(cell or ""))
            if m:
                cols[j] = int(m.group(1))
        if len(cols) >= 2:
            return cols, i
    return {}, -1


def _periods(rows, header_row, cols, need=2):
    """(기간) 줄에서 열마다의 기간을 읽는다.

    시작일과 끝일을 위아래 두 줄로 나눠 싣는 표가 있어서
    ("19.12.30" / "~20.12.29") 한 줄만 보면 못 읽는다. 이어지는 줄을
    열마다 이어 붙여 본다."""
    window = rows[header_row: header_row + 4]
    for span in (1, 2, 3):
        for start in range(len(window) - span + 1):
            joined = {}
            for row in window[start: start + span]:
                for j, cell in enumerate(row):
                    if (cell or "").strip():
                        joined[j] = joined.get(j, "") + " " + cell
            got = {}
            for j, text in joined.items():
                if j not in cols:
                    continue
                m = RE_PERIOD.search(" ".join(text.split()))
                if m:
                    got[j] = f"{m.group(1)}~{m.group(2)}"
            if len(got) >= need:
                return got
    return {}


def _is_yearly(periods):
    """연도별 표인가. 연평균 표는 기간이 모두 같은 날 끝난다."""
    ends = {p.split("~")[1] for p in periods.values()}
    return len(ends) > 1


def _row_label(cells):
    """행 앞머리에서 클래스 코드나 '투자신탁'/'비교지수'를 읽는다."""
    head = ""
    for cell in cells:
        if (cell or "").strip():
            head = cell
            break
    flat = _squash(head)
    if any(b in flat for b in BENCHMARK):
        return "benchmark", None
    got = _parse_row(cells)
    if len(got) == 1:
        return "class_return", next(iter(got))
    if any(w in flat for w in WHOLE_FUND) and not got:
        return "fund", None
    return None, None


def _parse_table(rows):
    cols, hdr = _year_columns(rows)
    if not cols:
        return None
    periods = _periods(rows, hdr, cols) or _periods(rows, hdr, cols, need=1)
    if not periods:
        return None
    if not _is_yearly(periods):
        # 기간이 하나뿐이면 끝나는 날을 견줄 수가 없다(설정한 지 얼마 안 된
        # 펀드는 1년차만 있고 나머지가 "-"다). 그럴 땐 표 제목에 기대서
        # 연도별 표인지 가린다.
        flat = _squash(" ".join((x or "") for r in rows for x in r))
        if len(periods) > 1 or "연도별" not in flat:
            return None

    out = []
    for row in rows[hdr + 1:]:
        kind, code = _row_label(row)
        if not kind:
            continue
        vals = {}
        for j, rank in sorted(cols.items()):
            if j < len(row):
                v = _clean_num(row[j])
                if v is not None:
                    vals[rank] = (v, periods.get(j))
        # 헤더 열 번호와 데이터 열 번호가 어긋난 표가 있다(헤더 2/5/8,
        # 데이터 1/4/7). 열 번호로 아무것도 못 읽었으면 값이 있는 칸을
        # 왼쪽부터 순서대로 년차에 맞춘다.
        if not vals:
            ranks = [r for _j, r in sorted(cols.items())]
            per = [periods.get(j) for j, _r in sorted(cols.items())]
            raw = [x for x in row[1:] if (x or "").strip()]
            nums = [_clean_num(x) for x in raw]
            nums = [n for n in nums if n is not None]
            for rank, v, pd in zip(ranks, nums, per):
                vals[rank] = (v, pd)

        for rank, (val, period) in sorted(vals.items()):
            out.append({
                "row_kind": kind,
                "class_code": code,
                "year_rank": rank,
                "period": period,
                "return_pct": val,
            })
    return out or None


def extract(db_path=DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT product_code FROM class_fees WHERE product_code IS NOT NULL")]

    out = []
    for code in codes:
        seen = set()
        for page, dj in conn.execute(
                "SELECT page, data_json FROM tables WHERE doc_id = ? ORDER BY page",
                (code,)):
            try:
                rows = json.loads(dj)
            except (ValueError, TypeError):
                continue
            got = _parse_table(rows)
            if not got:
                continue
            for r in got:
                key = (r["row_kind"], r["class_code"], r["year_rank"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(dict(r, product_code=code, page=page))
    conn.close()
    return out


def report(rows):
    prods = {r["product_code"] for r in rows}
    kinds = {}
    for r in rows:
        kinds[r["row_kind"]] = kinds.get(r["row_kind"], 0) + 1
    print(f"연도별 수익률 {len(rows)}건 / 상품 {len(prods)}개")
    for k, v in sorted(kinds.items()):
        print(f"  {k}: {v}건")
    if rows:
        s = [r for r in rows if r["row_kind"] == "class_return"][:6]
        print("\n  예시:")
        for r in s:
            print(f"    {r['product_code']} {r['class_code']} "
                  f"{r['year_rank']}년차({r['period']}) {r['return_pct']}%")


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
