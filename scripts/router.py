"""
연금 Agent 과제 - 질의 유형 분류 및 검색 라우팅

자연어 질의를 (제도/세제 institution) vs (상품 products) vs (복합)으로 분류하고,
scripts/search.py의 semantic_search / table_search를 적절히 호출해서
근거 후보(retrieved context)를 만든다.

HyperCLOVA X API 키가 아직 없어서, 분류는 지금은 키워드 규칙 기반이다.
키가 생기면 이 모듈의 classify()만 HCX 기반 의도분류로 교체하고,
route_search()의 인터페이스(반환 스키마)는 그대로 재사용하면 된다.

사용법(CLI, 수동 점검용):
    python scripts/router.py --query "DC와 DB, 운용 주체가 어떻게 다른가요?"
"""

import argparse
import json
import re

from search import semantic_search, table_search

# ---------------------------------------------------------------------------
# 키워드 규칙 (HCX 의도분류로 교체 전까지의 임시 휴리스틱)
# ---------------------------------------------------------------------------

INSTITUTION_KEYWORDS = [
    "db", "dc", "irp", "연금저축", "퇴직연금", "퇴직금", "개인연금",
    "세액공제", "연금소득세", "퇴직소득세", "기타소득세", "과세", "비과세", "세율",
    "중도인출", "해지", "수령", "개시", "이전", "이체", "승계", "가입", "인출",
    "부득이한 사유", "수령한도", "연금수령", "제도", "소득세법", "근로자퇴직급여보장법",
    "디폴트옵션", "명퇴", "퇴직", "연금계좌", "이연퇴직소득", "종합과세", "분리과세",
]

PRODUCT_KEYWORDS = [
    "펀드", "etf", "상품", "위험등급", "보수", "수수료", "수익률", "클래스",
    "리츠", "인프라펀드", "추천", "비교", "tdf", "mp", "포트폴리오", "종목",
    "투자설명서", "투자전략", "자산배분", "원리금보장", "국공채", "채권형",
    "주식형", "혼합형", "운용사", "설정액", "판매사",
]

TAX_KEYWORDS = [
    "세액공제", "연금소득세", "퇴직소득세", "기타소득세", "과세", "비과세",
    "세율", "종합과세", "분리과세", "한도",
]

# 세제뿐 아니라 "표로 정리된 명확한 사실값"을 묻는 질의는 table_search도 같이
# 태워야 근거가 잡힌다. 세액공제/한도 같은 세제 키워드에 더해, 위험등급/보수/
# 수수료/수익률처럼 상품 표에 그대로 나오는 수치성 키워드를 포함한다.
TABLE_FACT_KEYWORDS = TAX_KEYWORDS + [
    "위험등급", "보수", "수수료", "수익률", "설정액",
]

PRODUCT_CODE_RE = re.compile(r"KR[0-9A-Z]{10}", re.IGNORECASE)


def _build_fts_query(classification: dict) -> str | None:
    """자연어 문장을 그대로 FTS5 MATCH에 넘기면 (묵시적 AND라) 거의 안 걸린다.
    매칭된 키워드들을 OR로 묶어서 표 검색에 쓸 질의를 만든다."""
    terms = list(dict.fromkeys(
        classification["matched_institution_keywords"]
        + classification["matched_product_keywords"]
        + classification["product_codes"]
    ))
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in terms)


def classify(query: str) -> dict:
    """질의를 규칙 기반으로 분류. institution/products 검색 여부와 세제 여부를 판단."""
    q = query.lower()

    inst_hits = [kw for kw in INSTITUTION_KEYWORDS if kw in q]
    prod_hits = [kw for kw in PRODUCT_KEYWORDS if kw in q]
    tax_hits = [kw for kw in TAX_KEYWORDS if kw in q]
    table_fact_hits = [kw for kw in TABLE_FACT_KEYWORDS if kw in q]
    product_codes = PRODUCT_CODE_RE.findall(query)

    use_institution = bool(inst_hits) or bool(tax_hits)
    use_products = bool(prod_hits) or bool(product_codes)

    # 둘 다 안 걸리면(짧은 질의, 애매한 질의 등) 폭넓게 양쪽 다 검색 -
    # 정보가 부족한 채로 좁혀서 검색하면 근거 누락 위험이 더 크다고 판단.
    ambiguous = not use_institution and not use_products
    if ambiguous:
        use_institution = True
        use_products = True

    if use_institution and use_products:
        category = "복합"
    elif use_institution:
        category = "제도/세제"
    elif use_products:
        category = "상품"
    else:
        category = "복합"  # unreachable given ambiguous fallback above, kept for clarity

    return {
        "category": category,
        "use_institution": use_institution,
        "use_products": use_products,
        "use_table_search": bool(table_fact_hits),
        "product_codes": product_codes,
        "matched_institution_keywords": inst_hits,
        "matched_product_keywords": prod_hits,
        "ambiguous": ambiguous,
    }


# 이 점수 밑이면 "근거로 쓰기엔 부실하다"고 보고 검색 범위를 넓혀 재시도한다.
# tfidf/LSA 코사인 유사도 기준 임시값 — 실제 질의로 튜닝된 값은 아니라 조정 여지 있음.
RELEVANCE_RETRY_THRESHOLD = 0.30


def route_search(query: str, k: int = 5) -> dict:
    """분류 결과에 따라 semantic_search / table_search를 호출하고 결과를 합쳐서 반환.

    1차 검색 결과의 최고 유사도가 너무 낮으면(RELEVANCE_RETRY_THRESHOLD 미만),
    분류가 좁게(institution만 또는 products만) 잡았을 가능성을 의심하고
    양쪽 다 검색하는 재시도를 한 번 한다 — "검색 결과가 질문과 안 맞으면
    쿼리 범위를 바꿔서 재시도" 패턴.

    반환값:
        {
            "classification": classify()의 결과,
            "semantic_hits": [...],
            "table_hits": [...],
            "retried": bool,
        }
    """
    classification = classify(query)

    def _search(doc_types):
        hits = []
        for doc_type in doc_types:
            product_code = classification["product_codes"][0] if (
                doc_type == "products" and classification["product_codes"]
            ) else None
            hits.extend(
                semantic_search(query, k=k, doc_type=doc_type, product_code=product_code)
            )
        hits.sort(key=lambda h: h["score"], reverse=True)
        return hits

    doc_types = []
    if classification["use_institution"]:
        doc_types.append("institution")
    if classification["use_products"]:
        doc_types.append("products")

    semantic_hits = _search(doc_types)

    retried = False
    both_types = {"institution", "products"}
    top_score = semantic_hits[0]["score"] if semantic_hits else 0.0
    if top_score < RELEVANCE_RETRY_THRESHOLD and set(doc_types) != both_types:
        retried = True
        doc_types = ["institution", "products"]
        retry_hits = _search(doc_types)
        # 재시도 결과가 원래 결과보다 나을 때만 채택 (더 나쁘면 원래 결과 유지)
        if retry_hits and (not semantic_hits or retry_hits[0]["score"] > top_score):
            semantic_hits = retry_hits

    table_hits = []
    if classification["use_table_search"]:
        fts_query = _build_fts_query(classification)
        if fts_query:
            product_code = classification["product_codes"][0] if classification["product_codes"] else None
            for doc_type in doc_types:
                try:
                    # 상품코드가 특정됐으면 그 상품 표만 우선 검색 (다른 상품의
                    # 변경이력 표 등 무관한 표가 키워드만 겹쳐서 섞여 들어오는 걸 방지)
                    scoped_hits = table_search(
                        fts_query, k=k, doc_type=doc_type,
                        product_code=product_code if doc_type == "products" else None,
                    )
                    if product_code and doc_type == "products" and not scoped_hits:
                        scoped_hits = table_search(fts_query, k=k, doc_type=doc_type)
                    table_hits.extend(scoped_hits)
                except ValueError:
                    pass  # FTS5 쿼리 문법에 안 맞으면 표 검색 스킵

    return {
        "classification": classification,
        "semantic_hits": semantic_hits[: max(k, 5)],
        "table_hits": table_hits[:k],
        "retried": retried,
    }


def main():
    parser = argparse.ArgumentParser(description="질의 분류 + 라우팅 검색 수동 점검 CLI")
    parser.add_argument("--query", required=True)
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()

    result = route_search(args.query, k=args.k)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
