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
        "use_table_search": bool(tax_hits),
        "product_codes": product_codes,
        "matched_institution_keywords": inst_hits,
        "matched_product_keywords": prod_hits,
        "ambiguous": ambiguous,
    }


def route_search(query: str, k: int = 5) -> dict:
    """분류 결과에 따라 semantic_search / table_search를 호출하고 결과를 합쳐서 반환.

    반환값:
        {
            "classification": classify()의 결과,
            "semantic_hits": [...],
            "table_hits": [...],
        }
    """
    classification = classify(query)
    semantic_hits = []
    table_hits = []

    doc_types = []
    if classification["use_institution"]:
        doc_types.append("institution")
    if classification["use_products"]:
        doc_types.append("products")

    for doc_type in doc_types:
        product_code = classification["product_codes"][0] if (
            doc_type == "products" and classification["product_codes"]
        ) else None
        semantic_hits.extend(
            semantic_search(query, k=k, doc_type=doc_type, product_code=product_code)
        )

    semantic_hits.sort(key=lambda h: h["score"], reverse=True)

    if classification["use_table_search"]:
        fts_query = _build_fts_query(classification)
        if fts_query:
            for doc_type in doc_types:
                try:
                    table_hits.extend(table_search(fts_query, k=k, doc_type=doc_type))
                except ValueError:
                    pass  # FTS5 쿼리 문법에 안 맞으면 표 검색 스킵

    return {
        "classification": classification,
        "semantic_hits": semantic_hits[: max(k, 5)],
        "table_hits": table_hits[:k],
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
