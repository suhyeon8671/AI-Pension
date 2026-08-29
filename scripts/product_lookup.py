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

import difflib
import os
import re
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "structured_store.db")

PRODUCT_CODE_RE = re.compile(r"KR[0-9A-Z]{10}", re.IGNORECASE)

# 상품 종류를 나타내는 상투어. 이 말들만으로 이뤄진 일치는 상품을
# 가리키지 못한다("증권투자신탁"은 100개 상품 대부분에 들어 있다).
GENERIC_WORDS = (
    "증권", "자투자신탁", "모투자신탁", "투자신탁", "투자회사", "집합투자기구",
    "주식형", "채권형", "혼합형", "주식", "채권", "혼합", "재간접", "파생형",
    "인덱스", "전환형", "단위형", "추가형", "개방형", "폐쇄형", "종류형",
    "연금저축", "연금", "펀드", "호",
)

# "A클래스" / "종류C-e" / "클래스 C-P" 처럼 클래스를 지목하는 표현
CLASS_IN_QUERY_RE = re.compile(
    r"(?:종류|클래스)\s*([A-Za-z][A-Za-z0-9\-]{0,7})|([A-Za-z][A-Za-z0-9\-]{0,7})\s*클래스")


def _norm(s):
    return re.sub(r"[\s.,·ㆍ\-_()\[\]:]", "", s or "")


def _is_generic(text):
    """일치한 조각이 상투어만으로 이뤄졌는지. 그렇다면 상품을 못 가린다."""
    t = text
    for w in sorted(GENERIC_WORDS, key=len, reverse=True):
        t = t.replace(w, "")
    return len(t) < 2


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
    return [(code, name, _norm(name)) for code, name in rows if name]


_CACHE = None


def find_products(question, db_path=DEFAULT_DB_PATH, limit=4):
    """질문에서 상품을 찾는다. 코드가 직접 적혀 있으면 그게 우선.

    처음엔 상품명에서 상투어("증권자투자신탁1호(주식)")를 지운 "핵심
    이름"이 질문에 통째로 들어 있는지로 맞췄는데, 상투어 목록에 있는
    "전환형"이 "목표전환형"의 일부라 잘려 나가는 등 단어를 깎는 방식
    자체가 취약했다(KCGI코리아목표전환형 -> KCGI코리아목표). 그래서
    질문과 상품명의 "가장 긴 공통 문자열"로 바꿨다 - 어디를 어떻게
    부르든 겹치는 만큼 점수가 된다. 다만 그 겹친 부분이 상투어뿐이면
    상품을 못 가리므로 버린다.

    돌려주는 것: [(product_code, product_name, 겹친 글자수)]"""
    global _CACHE
    if _CACHE is None:
        _CACHE = load_products(db_path)

    codes = list(dict.fromkeys(
        c.upper() for c in PRODUCT_CODE_RE.findall(question or "")))
    by_code = {c: n for c, n, _ in _CACHE}
    if codes:
        return [(c, by_code.get(c), 99) for c in codes if c in by_code][:limit]

    q = _norm(question)
    if len(q) < 3:
        return []
    best = {}
    for code, name, nname in _CACHE:
        m = difflib.SequenceMatcher(None, q, nname, autojunk=False)\
            .find_longest_match(0, len(q), 0, len(nname))
        if m.size < 4:
            continue
        piece = q[m.a:m.a + m.size]
        if _is_generic(piece):
            continue
        if code not in best or m.size > best[code][2]:
            best[code] = (code, name, m.size)
    out = sorted(best.values(), key=lambda h: h[2], reverse=True)
    # 1등과 많이 차이 나는 후보는 잡음이다(상투어 일부만 겹친 것)
    if out:
        top = out[0][2]
        out = [h for h in out if h[2] >= max(4, top - 2)]
    return out[:limit]


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
