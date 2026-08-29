"""
연금 Agent 과제 - 검색 인터페이스 (의미 검색 + 구조화 표 검색)

이후 단계(질의 라우팅, HyperCLOVA X 파이프라인, 평가용 API 서버)가 공통으로
쓸 수 있는 검색 함수를 제공한다:

- semantic_search(query, k, doc_type, product_code)
    Chroma 벡터 스토어에서 의미 기반 top-k 청크 검색.
- table_search(keyword, k, doc_type, product_code)
    SQLite FTS5로 표(세액공제 한도, 위험등급, 총보수 등) 키워드 검색.

두 함수 모두 결과에 근거 문서 메타데이터(doc_id, source_doc, page 등)를
포함해서 "모든 답변에는 근거 문서 표시" 요구사항을 그대로 만족시킨다.

사용법(CLI, 수동 점검용):
    python scripts/search.py --query "DC와 DB, 운용 주체가 어떻게 다른가요?"
    python scripts/search.py --query "세액공제 한도" --mode table
"""

import argparse
import json
import math
import os
import re
import sqlite3

import chromadb

from embeddings import get_provider
from build_vector_store import DEFAULT_STORE_DIR, COLLECTION_NAME, provider_state_path
from build_structured_store import DEFAULT_DB_PATH


def semantic_search(query, k=5, doc_type=None, product_code=None, provider_name=None,
                     store_dir=DEFAULT_STORE_DIR):
    client = chromadb.PersistentClient(path=store_dir)
    collection = client.get_collection(COLLECTION_NAME)

    provider = get_provider(provider_name)
    state_path = provider_state_path(store_dir)
    if os.path.exists(state_path):
        provider.load(state_path)
    query_embedding = provider.embed([query], is_query=True)[0]

    where = {}
    if doc_type:
        where["doc_type"] = doc_type
    if product_code:
        where["product_code"] = product_code

    result = collection.query(
        query_embeddings=[query_embedding],
        n_results=k,
        where=where or None,
    )

    hits = []
    for doc, meta, dist, cid in zip(
        result["documents"][0], result["metadatas"][0], result["distances"][0], result["ids"][0]
    ):
        hits.append({
            "chunk_id": cid,
            "text": doc,
            "score": 1 - dist,  # cosine distance -> similarity
            **meta,
        })
    return hits


# 질의에서 뽑아 쓰지 않을 말들. 어느 문서에나 있어서 검색을 넓히기만 한다.
LEXICAL_STOPWORDS = {
    "무엇", "뭔가요", "뭐야", "어떻게", "어떤", "얼마", "얼마나", "언제", "누가",
    "가능", "가능한가요", "인가요", "입니까", "있나요", "되나요", "하나요", "알려줘",
    "경우", "관련", "대해", "대한", "그리고", "하지만", "때문", "정도", "정말",
}
# 한글/영문/숫자가 섞인 덩어리를 한 낱말로 본다("DC형에서", "1,000만원").
_TERM_RE = re.compile(r"[가-힣A-Za-z0-9][가-힣A-Za-z0-9,.]*")

# 전체 청크의 이 비율을 넘게 나오는 낱말은 너무 흔해서 찾는 대상에서 뺀다.
COMMON_TERM_RATIO = 0.05

# 낱말 끝에 붙는 조사와 어미. 긴 것부터 떼어 낸다.
#
# 왜 필요한가: trigram 색인은 글자 그대로 찾으므로 "연금저축을"은 원문의
# "연금저축은"/"연금저축에"와 안 맞는다. 검증 세트에서 "연금저축을
# 중도해지하면 세금이..." 질문이 정답 청크(기타소득세 16.5%)를 못 찾은
# 이유가 이것이었다. 형태소 분석기를 붙이지 않고, 붙는 말만 떼어 낸다.
_PARTICLES = (
    "에서는", "에게는", "으로는", "이라는", "이라고", "입니다", "합니다",
    "하면서", "에서", "에게", "으로", "까지", "부터", "보다", "라도",
    "이나", "든지", "조차", "마저", "처럼", "만큼", "하면", "하는", "되면",
    "되는", "이란", "인가", "인지", "한테",
    "은", "는", "이", "가", "을", "를", "의", "에", "와", "과", "도", "만",
    "로", "라", "한", "할", "된", "됨", "임",
)


def _strip_particles(term):
    """낱말 뒤에 붙은 조사/어미를 뗀다. 3글자 밑으로 줄면 그만둔다."""
    changed = True
    while changed and len(term) > 3:
        changed = False
        for p in _PARTICLES:
            if term.endswith(p) and len(term) - len(p) >= 3:
                term = term[: -len(p)]
                changed = True
                break
    return term


def lexical_term_groups(query):
    """질의의 낱말을 [조사 붙은 형태, 뗀 형태]씩 묶어서 돌려준다.

    묶는 이유: 한 낱말을 두 형태로 넣어 두고 각각 점수를 주면 같은 말을
    두 번 세게 된다. "DC형에서"(8.9) + "DC형"(7.8) = 16.7이 "위험자산"
    (5.6) 하나를 압도해서, 정작 위험자산 얘기가 없는 청크가 1등이 됐다.
    한 묶음은 한 낱말로 치고 한 번만 점수를 준다.

    trigram 색인은 3글자 미만을 못 찾으므로 3글자 이상만 남긴다."""
    groups, seen = [], set()
    for raw in _TERM_RE.findall(query or ""):
        raw = raw.strip(",.")
        variants = []
        for t in (raw, _strip_particles(raw)):
            if len(t) < 3 or t in LEXICAL_STOPWORDS or t in seen:
                continue
            seen.add(t)
            variants.append(t)
        if variants:
            groups.append(variants)
    return groups


def lexical_terms(query):
    """lexical_term_groups를 펼친 것 (수동 점검·디버깅용)."""
    return [t for g in lexical_term_groups(query) for t in g]


def lexical_search(query, k=5, doc_type=None, product_code=None, db_path=DEFAULT_DB_PATH):
    """청크 본문을 글자 그대로 찾는다 (FTS5 trigram + bm25).

    의미 검색과 짝을 이룬다. 의미 검색은 "비슷한 얘기"를 잘 찾지만 정확한
    용어를 놓치고("기타소득세"를 물었는데 ISA 전환 FAQ가 1등), 이쪽은 정확한
    용어를 놓치지 않는 대신 말을 바꿔 물으면 못 찾는다. 둘을 합쳐 쓴다."""
    groups = lexical_term_groups(query)
    if not groups:
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if not total:
            return []

        # 낱말마다 몇 개 청크에 나오는지 세서 무게를 매긴다. 흔한 말일수록
        # 가볍다. 이게 없으면 OR로 묶인 질의에서 가장 흔한 낱말 하나만
        # 걸린 긴 청크가 1등으로 올라온다 - "DC형에서 위험자산은 최대 몇
        # 퍼센트"에 '위험자산'이 아예 없는 청크가 뽑히던 이유다.
        #
        # 한 묶음의 무게는 그 안에서 가장 가벼운 형태의 것을 쓴다. 조사가
        # 붙은 "DC형에서"는 조사 때문에 드물 뿐 "DC형"보다 알맹이가 더
        # 있는 말이 아니므로, 드물다고 무겁게 쳐 주면 안 된다.
        weighted, query_terms = [], []
        for variants in groups:
            found = []
            for t in variants:
                try:
                    df = conn.execute(
                        "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
                        [f'"{t}"']).fetchone()[0]
                except sqlite3.OperationalError:
                    continue
                if df:
                    found.append((t, df, math.log(1 + total / df)))
            if not found:
                continue
            t, df, w = min(found, key=lambda f: f[2])
            weighted.append((w, [f[0] for f in found]))
            # 너무 흔한 말은 찾는 대상에서 뺀다(가중치 계산에는 그대로 쓴다).
            if df <= total * COMMON_TERM_RATIO:
                query_terms.extend(f[0] for f in found)
        if not weighted:
            return []
        if not query_terms:
            # 다 흔하면 그중 가장 드문 묶음만 쓴다
            query_terms = max(weighted, key=lambda g: g[0])[1]

        # 낱말 묶음마다 따로 찾아서 후보를 모은다.
        #
        # 처음엔 낱말을 전부 OR로 묶어 한 번에 찾고 상위 100개만 받아서
        # 다시 줄 세웠는데, 흔한 낱말("DC형")에 걸린 청크가 100자리를 다
        # 채워서 정작 "위험자산"이 있는 청크는 후보에 들지도 못했다.
        # 묶음마다 자리를 따로 주면 어느 낱말도 밀려나지 않는다.
        rows, seen_ids = [], set()
        for _w, variants in weighted:
            if not any(t in query_terms for t in variants):
                continue
            sql = """
                SELECT c.id, c.chunk_id, c.doc_type, c.doc_id, c.source_doc,
                       c.product_code, c.page, c.text, bm25(chunks_fts) AS rank
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                WHERE chunks_fts MATCH ?
            """
            params = [" OR ".join(f'"{t}"' for t in variants)]
            if doc_type:
                sql += " AND c.doc_type = ?"
                params.append(doc_type)
            if product_code:
                sql += " AND c.product_code = ?"
                params.append(product_code)
            sql += " ORDER BY rank LIMIT ?"
            params.append(max(k * 10, 50))
            try:
                for r in conn.execute(sql, params):
                    if r["id"] not in seen_ids:
                        seen_ids.add(r["id"])
                        rows.append(r)
            except sqlite3.OperationalError:
                continue  # 색인이 없거나 질의 문법이 안 맞으면 조용히 넘어간다
    finally:
        conn.close()

    scored = []
    for r in rows:
        text = r["text"]
        covered = sum(w for w, variants in weighted
                      if any(t in text for t in variants))
        if not covered:
            continue
        scored.append((covered, -r["rank"], r))
    scored.sort(key=lambda s: (-s[0], -s[1]))

    return [{
        "chunk_id": r["chunk_id"],
        "text": r["text"],
        "doc_type": r["doc_type"],
        "doc_id": r["doc_id"],
        "source_doc": r["source_doc"],
        "product_code": r["product_code"],
        "page": r["page"],
        # 의미 검색의 코사인 유사도와는 척도가 달라서 그대로 견주면 안 된다
        # (router에서 순위로만 쓴다).
        "lexical_score": covered,
        "bm25": bm,
    } for covered, bm, r in scored[:k]]


def table_search(keyword, k=5, doc_type=None, product_code=None, db_path=DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    sql = """
        SELECT t.doc_type, t.doc_id, t.source_doc, t.product_code, t.page,
               t.table_index, t.data_json, t.row_text,
               bm25(tables_fts) AS rank
        FROM tables_fts
        JOIN tables t ON t.id = tables_fts.rowid
        WHERE tables_fts MATCH ?
    """
    params = [keyword]
    if doc_type:
        sql += " AND t.doc_type = ?"
        params.append(doc_type)
    if product_code:
        sql += " AND t.product_code = ?"
        params.append(product_code)
    sql += " ORDER BY rank LIMIT ?"
    params.append(k)

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        conn.close()
        raise ValueError(f"FTS5 쿼리 실패 (키워드 문법 확인): {e}") from e

    hits = []
    for r in rows:
        hits.append({
            "doc_type": r["doc_type"],
            "doc_id": r["doc_id"],
            "source_doc": r["source_doc"],
            "product_code": r["product_code"],
            "page": r["page"],
            "table_index": r["table_index"],
            "data": json.loads(r["data_json"]),
        })
    conn.close()
    return hits


def main():
    parser = argparse.ArgumentParser(description="연금 Agent 검색 인프라 수동 점검 CLI")
    parser.add_argument("--query", required=True)
    parser.add_argument("--mode", choices=["semantic", "table"], default="semantic")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--doc-type", choices=["institution", "products"], default=None)
    parser.add_argument("--product-code", default=None)
    args = parser.parse_args()

    if args.mode == "semantic":
        hits = semantic_search(args.query, k=args.k, doc_type=args.doc_type,
                                product_code=args.product_code)
    else:
        hits = table_search(args.query, k=args.k, doc_type=args.doc_type,
                             product_code=args.product_code)

    print(json.dumps(hits, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
