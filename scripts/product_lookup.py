"""질문 글자에서 상품(과 클래스)을 찾아낸다.

지금까지 상품 인식은 질문 안에 상품코드(KR5127420034)가 문자 그대로
들어 있을 때만 됐다(router.PRODUCT_CODE_RE). 그런데 실제 질문은
"미래에셋장기성장포커스 총보수 얼마야?"처럼 상품 이름으로 오기 때문에,
그 경로로는 구조화 DB(class_fees/class_returns)에 아예 닿지 못하고
텍스트 청크 검색으로 빠진다 - 정확한 숫자를 갖고 있으면서도 못 쓰는 셈.

상품명은 "미래에셋장기성장포커스증권자투자신탁1호(주식)"처럼 뒤에
상품 종류를 나타내는 상투적인 말이 길게 붙는다. 사람은 그 앞의
고유한 부분만 부르므로, 상투어를 걷어낸 "핵심 이름"으로 맞춘다.
"""

import os
import re
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "structured_store.db")

PRODUCT_CODE_RE = re.compile(r"KR[0-9A-Z]{10}", re.IGNORECASE)

# 상품명 뒤에 거의 항상 붙는 상투어. 사람이 상품을 부를 때 잘 쓰지 않아서
# 이걸 걷어내면 남는 게 그 상품의 고유한 이름이 된다.
GENERIC_RE = re.compile(
    r"(증권|자투자신탁|모투자신탁|투자신탁|투자회사|집합투자기구|"
    r"제?\d+호|주식형|채권형|혼합형|주식|채권|혼합|재간접|파생형|"
    r"인덱스형|전환형|단위형|추가형|개방형|폐쇄형|종류형|"
    r"\[.*?\]|\(.*?\))")

# "A클래스" / "종류C-e" / "클래스 C-P" 처럼 클래스를 지목하는 표현
CLASS_IN_QUERY_RE = re.compile(
    r"(?:종류|클래스)\s*([A-Za-z][A-Za-z0-9\-]{0,7})|([A-Za-z][A-Za-z0-9\-]{0,7})\s*클래스")


def _norm(s):
    return re.sub(r"[\s.,·ㆍ\-_]", "", s or "")


def core_name(product_name):
    """상품명에서 상투어를 걷어낸 핵심 이름."""
    return _norm(GENERIC_RE.sub("", product_name or ""))


def load_products(db_path=DEFAULT_DB_PATH):
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT product_code, product_name FROM product_master"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for code, name in rows:
        core = core_name(name)
        if len(core) >= 3:
            out.append((code, name, core))
    # 긴 핵심 이름부터 본다 - "미래에셋코어밸류"와 "미래에셋코어밸류연금저축"이
    # 둘 다 있으면 더 길게 맞는 쪽이 맞다.
    out.sort(key=lambda x: len(x[2]), reverse=True)
    return out


_CACHE = None


def find_products(question, db_path=DEFAULT_DB_PATH, limit=4):
    """질문에서 상품을 찾는다. 코드가 직접 적혀 있으면 그게 우선.
    돌려주는 것: [(product_code, product_name, 맞은 글자수)]"""
    global _CACHE
    codes = list(dict.fromkeys(
        c.upper() for c in PRODUCT_CODE_RE.findall(question or "")))
    if _CACHE is None:
        _CACHE = load_products(db_path)
    by_code = {c: (c, n) for c, n, _ in _CACHE}
    hits = [(c, by_code[c][1] if c in by_code else None, len(c))
            for c in codes if c in by_code]

    q = _norm(question)
    if q:
        for code, name, core in _CACHE:
            if any(h[0] == code for h in hits):
                continue
            # 핵심 이름이 통째로 들어 있으면 확실하다. 사람이 줄여 부르는
            # 경우("미래에셋장기성장포커스" -> "장기성장포커스")도 있어서,
            # 6글자 이상이면 앞을 잘라낸 형태도 본다.
            if core in q:
                hits.append((code, name, len(core)))
                continue
            if len(core) >= 8:
                for cut in range(2, min(6, len(core) - 5)):
                    if core[cut:] in q:
                        hits.append((code, name, len(core) - cut))
                        break
    hits.sort(key=lambda h: h[2], reverse=True)
    return hits[:limit]


def find_class_code(question):
    """질문이 특정 클래스를 지목하면 그 코드. 없으면 None."""
    m = CLASS_IN_QUERY_RE.search(question or "")
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip() or None


if __name__ == "__main__":
    import sys
    for q in sys.argv[1:]:
        print(f"{q!r}")
        for code, name, n in find_products(q):
            print(f"   {code}  {name}  (맞은 글자 {n})")
        print(f"   클래스: {find_class_code(q)}")
