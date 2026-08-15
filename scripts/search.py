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
import os
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
