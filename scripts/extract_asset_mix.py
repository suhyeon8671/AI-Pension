"""이 펀드가 무엇에 얼마나 투자하고 있는지 뽑는다 ("다. 집합투자기구의 자산구성 현황").

    "이 펀드 뭐에 투자해요?"
    "주식 비중이 얼마나 돼요?"

지금은 못 답한다. 상품명·보수·수익률은 다 있는데 정작 "이게 뭘 담고
있는 펀드인가"가 없다. 위험등급만으로는 주식형인지 채권형인지도 흐릿하다.

문서에는 이렇게 실려 있다.

    다. 집합투자기구의 자산구성 현황 (기준일 : 2025년 05월 02일, 단위: 백만원, %)
    통화별 구분 | 증권                          | 파생상품  | ... | 자산총액
               | 주식   채권  어음  집합투자증권 | 장내 장외 |
    KRW(한국)  | 5,715  -     -     -           | -    -    | ... | 5,856
               | 97.59  -     -     -           | -    -    | ... | 100
    합계       | 5,715  -     -     -           | -    -    | ... | 5,856
               | 97.59  -     -     -           | -    -    | ... | 100

금액 줄 바로 아래가 비율 줄이다. 비율을 괄호로 싸는 문서도 있다((66.40)).

제목으로 찾지 않는다
--------------------
"자산구성"이라는 글자로 표를 찾으면 42개 문서밖에 안 걸린다. 제목이
표 밖에 있거나 다른 표에 들어가 있기 때문이다. 표의 모양으로 찾으면
64개다 - 이 표는 "통화별"과 "자산총액"과 "주식/채권"이 한 표 안에
같이 있는 유일한 표라 모양만으로 확실히 특정된다. 제목 대신 모양을
보는 건 class_returns에서 이미 같은 이유로 택한 방식이다.

표로 안 잡히는 문서
------------------
글자가 한 자씩 떨어져 나오는 문서가 있다("통 화 별 구 분" -
KR5111450067 실측). 그런 페이지는 pdfplumber 기본 설정으로 표가 아예
안 잡혀서(0개), 가로줄을 글자 줄에서 잡는 설정으로 다시 읽는다.

실행:
    python3 scripts/extract_asset_mix.py
    python3 scripts/extract_asset_mix.py --check
"""

import argparse
import glob
import json
import os
import re
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "structured_store.db")
OUTPUT_JSON = os.path.join(REPO_ROOT, "asset_mix.json")
DATA_DIR = os.path.join(REPO_ROOT, "data", "products")

# 이 표를 다른 표와 가르는 낱말들. 넷이 한 표 안에 다 있어야 한다.
SHAPE_WORDS = ("통화", "자산총액", "주식", "채권")

# 큰 묶음 이름(윗줄)과 그 아래 세부 이름(아랫줄)
GROUP_WORDS = ("증권", "파생상품", "부동산", "특별자산",
               "단기대출및예금", "단기대출", "기타", "자산총액")
SUB_WORDS = ("주식", "채권", "어음", "집합투자증권", "장내", "장외",
             "실물자산", "기타")

# 이 표는 금융투자협회 표준 서식이라 값 열의 순서가 문서마다 같다.
# 머리글은 세 줄에 걸쳐 쪼개지고 글자가 한 자씩 떨어져 나오기도 해서
# ("집 합 투 자 증" + "권"이 서로 다른 줄, 다른 칸에 있다 -
# KR5111450067 실측) 이름으로 열을 맞추면 제일 큰 자산을 통째로
# 놓친다. 열 개수가 맞으면 순서로 붙이고, 머리글은 확인용으로만 쓴다.
CANONICAL_COLUMNS = (
    "주식", "채권", "어음", "집합투자증권",
    "파생상품(장내)", "파생상품(장외)", "부동산",
    "특별자산(실물자산)", "특별자산(기타)",
    "단기대출및예금", "기타", "자산총액",
)
# "파생결합증권" 칸을 하나 더 두는 서식도 있다(KR5114420016 실측).
CANONICAL_COLUMNS_13 = (
    "주식", "채권", "어음", "집합투자증권", "파생결합증권",
    "파생상품(장내)", "파생상품(장외)", "부동산",
    "특별자산(실물자산)", "특별자산(기타)",
    "단기대출및예금", "기타", "자산총액",
)
CANONICAL_BY_LEN = {len(CANONICAL_COLUMNS): CANONICAL_COLUMNS,
                    len(CANONICAL_COLUMNS_13): CANONICAL_COLUMNS_13}

RE_NUM = re.compile(r"^\(?-?[\d,]+(?:\.\d+)?\)?$")
RE_PCT_ROW_HINT = re.compile(r"^\(?\d+(?:\.\d+)?\)?$")
# "(기준일 : 2025년 05월 02일" / "[2025.05.02 현재" 둘 다 쓴다.
RE_AS_OF = re.compile(
    r"(20\d{2})\s*[.년]\s*(\d{1,2})\s*[.월]\s*(\d{1,2})")


def _squash(text):
    return re.sub(r"\s+", "", text or "")


def _num(v):
    """숫자 칸을 float으로.

    비율을 괄호로 싸는 문서가 많은데((66.40)) 그 괄호는 음수 표시가
    아니라 그냥 감싼 것이다. 음수는 괄호 밖에 따로 붙는다 - "-(17.58)"
    (KR5127420034 실측). 그래서 괄호는 어디 있든 지우고 본다. 처음엔
    양끝만 떼다가 "-(17.58"이 남아 그 칸을 통째로 잃었고, 자산 비율
    합이 117%가 됐다."""
    t = _squash(v).replace("(", "").replace(")", "").replace("△", "-")
    if not t or t == "-":
        return None
    try:
        return float(t.replace(",", ""))
    except ValueError:
        return None


def _is_shape(rows):
    flat = _squash(" ".join((c or "") for r in rows for c in r))
    return all(w in flat for w in SHAPE_WORDS)


def _header_labels(rows):
    """머리글 두 줄을 겹쳐 열마다의 자산 이름을 만든다.

    윗줄은 큰 묶음("증권", "파생상품"), 아랫줄은 세부("주식", "장내")다.
    묶음 칸은 여러 열에 걸쳐 있고 그 열들엔 빈 칸으로 나오므로, 나온
    묶음 이름을 오른쪽으로 끌고 간다. 돌려주는 것은 (열 -> 이름, 머리글
    마지막 줄 번호)."""
    gi = si = -1
    for i, row in enumerate(rows[:6]):
        flat = _squash(" ".join((c or "") for c in row))
        if gi < 0 and "통화" in flat and "자산총액" in flat:
            gi = i
        elif gi >= 0 and si < 0 and any(w in flat for w in ("주식", "장내")):
            si = i
            break
    if gi < 0:
        return {}, -1

    groups, cur = {}, None
    for j, cell in enumerate(rows[gi]):
        name = _squash(cell)
        if name in GROUP_WORDS or name.startswith("단기대출"):
            cur = name
        groups[j] = cur

    labels = {}
    subs = rows[si] if si >= 0 else []
    for j in range(max(len(rows[gi]), len(subs))):
        sub = _squash(subs[j]) if j < len(subs) else ""
        group = groups.get(j)
        if sub and sub not in SUB_WORDS:
            sub = ""
        if not sub and not group:
            continue
        if group in ("파생상품", "특별자산") and sub:
            labels[j] = f"{group}({sub})"
        elif sub:
            labels[j] = sub
        elif group and group != "통화별구분":
            labels[j] = group
    return labels, max(gi, si)


def _cell_with_wrap(rows, i, j, span=2):
    """한 칸의 글자가 위아래 줄로 쪼개진 경우 이어 붙여 읽는다.

    KR518101012M 실측: 기타 비율 "(-25.47)"이 세 줄에 걸쳐 "(-" / 빈칸 /
    "25.47)"로 나뉘어 있다. 그대로 두면 그 칸만 빠져 자산 비율 합이
    125%가 된다.

    값이 제대로 읽히는 칸에는 쓰지 않는다 - 데이터 줄이 빈 줄 없이
    붙어 있는 표에서는 위아래를 이어 붙이면 다른 자산의 값이 섞인다."""
    parts = []
    for r in range(max(0, i - span), min(len(rows), i + span + 1)):
        if j < len(rows[r]):
            parts.append(_squash(rows[r][j]))
    return _num("".join(parts))


def _numeric_rows(rows, start):
    """값이 두 칸 이상 든 줄만 순서대로 (줄번호, 줄)."""
    out = []
    for i in range(start, len(rows)):
        cols = [j for j, c in enumerate(rows[i]) if j and _num(c) is not None]
        if len(cols) >= 2:
            out.append((i, rows[i]))
    return out


def _amount_and_pct_rows(rows, start):
    """합계(또는 통화) 금액 줄과 그 아래 비율 줄을 고른다.

    두 줄이 딱 붙어 있지 않은 문서가 있다. 줄 앞머리가 다음 줄로
    넘어가면서 빈 줄이 끼기 때문이다(KR5111450067 실측: "대 한 민" /
    "국" / 빈 줄 / 비율 줄). 그래서 바로 다음 줄이 아니라 "값이 든
    다음 줄"을 짝으로 본다.

    비율 줄을 아예 안 싣고 금액만 적는 문서도 있다(KR5114420016 실측:
    KRW/USD/합계 세 줄에 금액만). 그럴 땐 비율 줄 없이 금액 줄만
    돌려준다 - 비율은 자산총액으로 나눠서 만든다.

    합계 줄이 따로 있으면 그쪽을 쓴다 - 통화가 여럿인 펀드는 통화별
    줄만 보면 전체 비중이 안 나온다."""
    numeric = _numeric_rows(rows, start)
    amounts = []          # 비율 줄이 없을 때 쓸 후보
    for k, (i, row) in enumerate(numeric):
        head = _squash("".join((rows[r][0] or "") if rows[r] else ""
                               for r in range(i, numeric[k + 1][0]
                                              if k + 1 < len(numeric) else i + 1)))
        # 이 줄 자신이 비율 줄이면 금액 후보가 아니다.
        last = next((v for v in (_num(c) for c in reversed(row))
                     if v is not None), None)
        if last is not None and abs(last - 100) <= 1.0:
            continue
        amounts.append((k, i, row, head))

    best = None
    for k, i, amt, head in amounts:
        pct = None
        if k + 1 < len(numeric):
            nxt = numeric[k + 1][1]
            # 어느 줄이 비율 줄인지는 맨 오른쪽 "자산총액" 칸이 말해
            # 준다 - 거기가 100이면 비율 줄이다. 처음엔 "100을 넘는
            # 값이 없으면 비율 줄"로 봤는데 그러면 레버리지를 쓰는
            # 펀드를 통째로 잃는다(KR5113420069 실측: 채권 101.32%,
            # 기타 -33.95%로 자산총액만 100이다).
            last_pct = next((v for v in (_num(c) for c in reversed(nxt))
                             if v is not None), None)
            if last_pct is not None and abs(last_pct - 100) <= 1.0:
                pct = nxt
        if "합계" in head or "합" in head:
            return amt, pct
        if best is None:
            best = (amt, pct)
    return best if best else (None, None)


def _as_of(rows, page_text=""):
    flat = _squash(" ".join((c or "") for r in rows for c in r)) + _squash(page_text)
    m = RE_AS_OF.search(flat)
    if not m:
        return None
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


# 잎 이름 -> 답변에 쓸 이름
LEAF_RENAME = {"장내": "파생상품(장내)", "장외": "파생상품(장외)",
               "실물자산": "특별자산(실물자산)"}
LEAF_WORDS = set(SUB_WORDS) | {"부동산", "단기대출및예금", "자산총액"}


def _leaf_sequence(rows, hdr_end):
    """머리글에서 잎 이름만 왼쪽부터 순서대로 뽑는다.

    빈 칸을 잔뜩 끼워 넣은 표가 있다(KR5131420025 실측: 28열인데 값은
    9열뿐이고 나머지는 빈 칸). 열 번호로 이름을 맞추면 묶음 이름("증권")이
    잎 자리로 새어 들어와 자산 이름이 "증권"으로 두 번 나온다. 이름도
    값과 같은 순서로 놓이므로 순서로 맞춘다.

    묶음 이름(증권/파생상품/특별자산)은 잎이 아니라서 뺀다 - 그 아래
    주식·채권·장내 같은 잎이 따로 있다."""
    joined = {}
    for row in rows[:hdr_end + 1]:
        for j, c in enumerate(row):
            t = _squash(c)
            if t:
                joined[j] = joined.get(j, "") + t
    seq = []
    for j in sorted(joined):
        t = joined[j]
        if t in LEAF_WORDS or t.startswith("단기대출"):
            seq.append(LEAF_RENAME.get(t, t))
    return seq


def _column_names(amt_row, rows, hdr_end):
    """값이 든 열 -> 자산 이름.

    표준 서식대로 값 열이 12개(또는 13개)면 순서로 붙인다. 그게 아니면
    머리글의 잎 이름을 순서로 맞춘다. 둘 다 안 되면 포기한다 - 이름을
    잘못 붙이느니 이 상품을 비워 두는 편이 낫다."""
    value_cols = [j for j, c in enumerate(amt_row) if j and _num(c) is not None]
    canon = CANONICAL_BY_LEN.get(len(value_cols))
    if canon:
        return dict(zip(value_cols, canon))
    leaves = _leaf_sequence(rows, hdr_end)
    if len(leaves) == len(value_cols) and len(set(leaves)) == len(leaves):
        return dict(zip(value_cols, leaves))
    # 마지막으로 머리글의 열 번호를 그대로 쓴다. 안 담은 자산을 "-"로
    # 비워 둔 문서는 값 열이 서너 개뿐이라 위 두 길에 안 걸린다.
    labels = {j: n for j, n in _header_labels(rows)[0].items() if j in value_cols}
    if labels and len(set(labels.values())) == len(labels):
        return labels
    return {}


def parse_asset_table(rows, page_text=""):
    """자산구성 표 하나 → {items, total_amount, as_of}. 아니면 None."""
    if not _is_shape(rows):
        return None
    # 머리글은 시작 줄을 잡는 데만 쓴다. 열 이름은 표준 서식의 순서로
    # 붙이므로 머리글을 못 찾아도 진행한다 - "통화별"과 "자산총액"이
    # 서로 다른 줄에 놓인 문서가 있어서(KR5120451001 실측) 머리글을
    # 요구하면 그런 문서를 통째로 잃는다. 잘못 읽는 건 아래 자산총액
    # 100% 검산이 막는다.
    _labels, hdr_end = _header_labels(rows)
    amt_row, pct_row = _amount_and_pct_rows(rows, max(hdr_end + 1, 0))
    pct_idx = next((i for i, r in enumerate(rows) if r is pct_row), -1)
    if amt_row is None:
        return None
    names = _column_names(amt_row, rows, max(hdr_end, 0))
    if not names:
        return None

    # 맨 오른쪽은 자산총액이고 그 비율은 100이어야 한다. 아니면 열을
    # 잘못 짚은 것이니 이 표는 쓰지 않는다 - 답변에 "주식 97.6%"처럼
    # 바로 나가는 값이라 한 칸만 밀려도 그대로 틀린 답이 된다.
    last = max(names)
    if names[last] != "자산총액":
        return None
    total_amount = _num(amt_row[last]) if last < len(amt_row) else None
    if pct_row is not None:
        total_pct = _num(pct_row[last]) if last < len(pct_row) else None
        if total_pct is None or abs(total_pct - 100) > 1.0:
            return None
    elif not total_amount:
        # 비율 줄이 없으면 자산총액으로 나눠 만들어야 하는데, 그 값이
        # 없으면 만들 수가 없다.
        return None

    out, derived = [], False
    for j, name in sorted(names.items()):
        if name == "자산총액":
            continue
        amount = _num(amt_row[j]) if j < len(amt_row) else None
        if pct_row is not None:
            pct = _num(pct_row[j]) if j < len(pct_row) else None
            if pct is None and amount:
                # 금액은 있는데 비율 칸만 비었다 - 글자가 위아래로
                # 쪼개진 경우다.
                pct = _cell_with_wrap(rows, pct_idx, j)
        elif amount is not None and total_amount:
            # 문서가 비율을 안 실은 경우. 자산총액으로 나눠 만든 값이라
            # 문서에 그대로 적힌 숫자가 아니다 - derived로 표시한다.
            pct = round(amount / total_amount * 100, 2)
            derived = True
        else:
            pct = None
        if pct is None and amount is None:
            continue
        # 안 담고 있는 자산은 답변에 낼 필요가 없다.
        if (pct or 0) == 0 and (amount or 0) == 0:
            continue
        out.append({"asset": name, "amount": amount, "pct": pct})
    if not out:
        return None
    # 자산별 비중은 서로 더하면 100이어야 한다. 안 맞으면 칸을 잘못
    # 읽었거나 하나를 빠뜨린 것이다 - 답변에 "채권 112.7%"처럼 그대로
    # 나가는 값이라, 맞출 수 없으면 이 상품은 비워 둔다.
    got = sum(i["pct"] for i in out if i["pct"] is not None)
    if abs(got - 100) > 1.0:
        return None
    return {"items": out, "total_amount": total_amount,
            "pct_derived": derived,
            "as_of": _as_of(rows, page_text)}


def _tables_from_db(conn, code):
    for page, dj in conn.execute(
            "SELECT page, data_json FROM tables WHERE doc_id = ? ORDER BY page",
            (code,)):
        try:
            yield page, json.loads(dj)
        except (ValueError, TypeError):
            continue


def _candidate_pages(conn, code):
    """이 표가 있을 만한 페이지 번호. 본문·표 어디든 "자산구성"이나
    "통화별"이 적힌 쪽과 그 다음 쪽을 후보로 본다(제목과 표가 페이지
    경계로 갈리는 문서가 있다)."""
    pages = set()
    for sql in ("SELECT page FROM chunks WHERE doc_id = ? AND "
                "(text LIKE '%자산구성%' OR text LIKE '%통화별%' "
                "OR text LIKE '%자산총액%')",
                "SELECT page FROM tables WHERE doc_id = ? AND "
                "(row_text LIKE '%자산구성%' OR row_text LIKE '%통화별%' "
                "OR row_text LIKE '%자산총액%')"):
        for (pg,) in conn.execute(sql, (code,)):
            pages.add(pg)
            pages.add(pg + 1)
    return sorted(pages)


def _tables_from_pdf(conn, code):
    """DB의 표로 못 찾은 문서를 PDF에서 다시 읽는다.

    두 가지가 걸린다. 글자가 한 자씩 떨어져 나오는 문서는 기본 설정으로
    표가 아예 안 잡히고(KR5111450067 58쪽 실측: 0개), 표가 한 칸으로
    뭉뚱그려 잡히는 문서도 있다(KR5116501001 43쪽: 2행 1열). 둘 다
    가로줄을 글자 줄에서 잡으면 칸이 살아난다.

    페이지는 DB에서 미리 추린다 - 전 쪽을 여는 건 느리기도 하지만,
    글자가 흩어진 문서는 페이지 글자로 걸러 봐야 "통화별"이 붙어 있지도
    않아서 소용이 없다."""
    import pdfplumber

    pdfs = glob.glob(os.path.join(DATA_DIR, code, "*.pdf"))
    pages = _candidate_pages(conn, code)
    if not pdfs or not pages:
        return
    settings = {"vertical_strategy": "lines", "horizontal_strategy": "text"}
    with pdfplumber.open(pdfs[0]) as pdf:
        for pno in pages:
            if pno < 1 or pno > len(pdf.pages):
                continue
            page = pdf.pages[pno - 1]
            text = page.extract_text() or ""
            for t in page.find_tables(table_settings=settings):
                rows = t.extract()
                if rows:
                    yield pno, rows, text


def extract(db_path=DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    codes = [r[0] for r in conn.execute("SELECT product_code FROM product_master")]

    out, fallback = [], []
    for code in codes:
        got = None
        for page, rows in _tables_from_db(conn, code):
            rec = parse_asset_table(rows)
            if rec:
                got = dict(rec, product_code=code, page=page, method="cell_grid")
                break
        if got is None:
            for page, rows, text in _tables_from_pdf(conn, code):
                rec = parse_asset_table(rows, text)
                if rec:
                    got = dict(rec, product_code=code, page=page,
                               method="pdf_text_rows")
                    fallback.append(code)
                    break
        if got:
            out.append(got)
    conn.close()
    return out, fallback


def report(rows, fallback):
    print(f"자산구성 {len(rows)}개 상품")
    if fallback:
        print(f"  PDF 재읽기로 건진 문서: {len(fallback)}개 {fallback[:6]}")
    for r in rows[:4]:
        items = ", ".join(f"{i['asset']} {i['pct']}%" for i in r["items"])
        print(f"  [{r['product_code']} p{r['page']} {r.get('as_of') or '기준일?'}] {items}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    rows, fallback = extract(args.db)
    report(rows, fallback)
    if args.check:
        return
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"\n→ {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
