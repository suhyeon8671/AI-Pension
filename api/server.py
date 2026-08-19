"""
연금 Agent 과제 - 평가용 API 서버

주최측 스펙:
    GET {endpoint}/answer?question_id={id}&question={질의}
    -> 200 application/json
    {
        "question_id": "Q-001",
        "question": "평가 질의 원문",
        "retrieved_context": "답변 생성에 참고한 검색 문서",
        "think_trace": "사고, 추론, 도구 사용 과정",
        "answer": "최종 생성 답변"
    }
    (모든 필드는 string. 헤더/인증 없음. GET만 지원.)

현재 상태: 검색(라우팅 + semantic/table search)까지는 실제로 동작한다.
"answer"는 아직 HyperCLOVA X API 키가 없어서 실제 LLM 생성이 아니라
검색된 근거를 그대로 요약해서 보여주는 발췌형 스텁이다
(generate_answer()에 명시). 키가 발급되면 이 함수만 HCX 호출로
교체하면 되고, 그 외 라우팅/검색/응답 스키마는 그대로 재사용한다.

실행:
    uvicorn api.server:app --host 0.0.0.0 --port 8000
    (배포 시 표준 포트: HTTP 80 / HTTPS 443. 80 포트 바인딩은 root 권한 필요)

테스트:
    curl -G "http://127.0.0.1:8000/answer" \
        --data-urlencode "question_id=Q-001" \
        --data-urlencode "question=DC와 DB, 운용 주체가 어떻게 다른가요?"
"""

import os
import sys

from fastapi import FastAPI
from fastapi.responses import JSONResponse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from router import route_search  # noqa: E402

app = FastAPI(title="연금 Agent 평가용 API")

MAX_CONTEXT_CHUNKS = 6
MAX_TABLE_ROWS_SHOWN = 3


def format_retrieved_context(route_result: dict) -> str:
    """근거 문서를 소스 태그와 함께 하나의 문자열로 합친다 (근거 표시 요구사항)."""
    parts = []
    for hit in route_result["semantic_hits"][:MAX_CONTEXT_CHUNKS]:
        tag = f"[{hit.get('doc_type')}/{hit.get('doc_id')} p.{hit.get('page')}]"
        parts.append(f"{tag}\n{hit['text']}")
    for hit in route_result["table_hits"]:
        tag = f"[{hit.get('doc_type')}/{hit.get('doc_id')} p.{hit.get('page')} 표]"
        rows = hit["data"][:MAX_TABLE_ROWS_SHOWN]
        row_text = "\n".join(" | ".join(cell for cell in row if cell) for row in rows)
        parts.append(f"{tag}\n{row_text}")
    if not parts:
        return "(검색된 근거 문서 없음)"
    return "\n\n---\n\n".join(parts)


def format_think_trace(query: str, route_result: dict) -> str:
    """질의 분류/검색 과정을 사고 과정으로 기록 (think_trace 필드)."""
    c = route_result["classification"]
    lines = [
        f"1. 질의 분류: {c['category']} (institution={c['use_institution']}, products={c['use_products']}, table_search={c['use_table_search']})",
    ]
    if c["matched_institution_keywords"]:
        lines.append(f"   - 매칭된 제도/세제 키워드: {c['matched_institution_keywords']}")
    if c["matched_product_keywords"]:
        lines.append(f"   - 매칭된 상품 키워드: {c['matched_product_keywords']}")
    if c["product_codes"]:
        lines.append(f"   - 인식된 상품코드: {c['product_codes']}")
    if c["ambiguous"]:
        lines.append("   - 키워드로 분류가 애매해 institution/products 양쪽 다 검색함")
    lines.append(f"2. 의미 검색(semantic_search) 결과 {len(route_result['semantic_hits'])}건")
    for hit in route_result["semantic_hits"][:MAX_CONTEXT_CHUNKS]:
        lines.append(f"   - {hit.get('doc_id')} p.{hit.get('page')} (유사도 {hit.get('score'):.3f})")
    if route_result["table_hits"]:
        lines.append(f"3. 표 검색(table_search) 결과 {len(route_result['table_hits'])}건")
        for hit in route_result["table_hits"]:
            lines.append(f"   - {hit.get('doc_id')} p.{hit.get('page')}")
    lines.append(
        "4. [주의] 답변 생성 단계는 아직 HyperCLOVA X API 키가 없어 실제 LLM 호출이 아니라 "
        "검색된 근거를 발췌/요약하는 임시 로직으로 대체되어 있음 (generate_answer 참고)"
    )
    return "\n".join(lines)


def generate_answer(query: str, route_result: dict) -> str:
    """
    TODO(HyperCLOVA X 키 발급 후): 이 함수를 HCX Chat Completions 호출로 교체.
    지금은 검색된 상위 근거를 그대로 이어붙인 발췌형 스텁 — 실제 자연어 답변 생성이 아님.
    """
    hits = route_result["semantic_hits"]
    if not hits:
        return (
            "죄송합니다, 보유한 데이터에서 이 질문에 답할 수 있는 근거를 찾지 못했습니다. "
            "질문을 더 구체적으로 말씀해 주시거나, 관련 제도/상품명을 알려주시면 다시 찾아보겠습니다."
        )
    top = hits[0]
    excerpt = top["text"][:300]
    return (
        f"[임시 답변 — HyperCLOVA X 연동 전 발췌 결과] "
        f"관련 근거({top.get('doc_id')} p.{top.get('page')})에 따르면:\n{excerpt}"
    )


@app.get("/answer")
def answer(question_id: str, question: str):
    route_result = route_search(question, k=5)
    body = {
        "question_id": question_id,
        "question": question,
        "retrieved_context": format_retrieved_context(route_result),
        "think_trace": format_think_trace(question, route_result),
        "answer": generate_answer(question, route_result),
    }
    return JSONResponse(content=body)


@app.get("/health")
def health():
    return {"status": "ok"}
