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

# router는 벡터 검색(chromadb)을 끌어오므로 모듈 로드 시점에 import하면
# 벡터 스토어가 없는 환경에서 서버가 아예 안 뜬다. 구조화 DB로 답하는
# 경로는 그것과 무관하게 동작해야 하므로 필요할 때 늦게 불러온다.
from compare_products import is_comparison_query, compare_products  # noqa: E402
from product_lookup import find_products, find_class_code  # noqa: E402
from product_facts import detect_intents, product_facts  # noqa: E402

app = FastAPI(title="연금 Agent 평가용 API")

MAX_CONTEXT_CHUNKS = 6
MAX_TABLE_ROWS_SHOWN = 3
# 청크당 컨텍스트에 넣는 최대 글자 수 — 토큰(크레딧) 절약을 위해 chunk_text.py의
# 최대 청크 길이(500자)보다 더 짧게 자른다. LLM에 원문 전체를 통째로 넘기지 않고
# 필요한 만큼만 파싱해서 전달하는 "Parser" 패턴.
CONTEXT_CHUNK_CHAR_LIMIT = 350


def _rank_score(hit):
    """router가 매긴 최종 순위 점수. 의미 검색과 글자 검색을 순위로 합친
    rrf가 있으면 그걸 쓴다 - 코사인 유사도(score)로 다시 줄 세우면 글자
    검색으로만 올라온 청크가 도로 뒤로 밀려 합친 의미가 없어진다."""
    return hit.get("rrf", hit.get("score") or 0.0)


def _dedupe_by_page(hits):
    """같은 (doc_id, page)에서 나온 청크가 여러 개면 가장 점수 높은 것만 남긴다.
    한 페이지 안의 인접 청크들이 겹치는 내용을 반복해서 토큰을 낭비하는 걸 막는다."""
    best = {}
    for hit in hits:
        key = (hit.get("doc_id"), hit.get("page"))
        if key not in best or _rank_score(hit) > _rank_score(best[key]):
            best[key] = hit
    return sorted(best.values(), key=_rank_score, reverse=True)


def format_retrieved_context(route_result: dict) -> str:
    """근거 문서를 소스 태그와 함께 하나의 문자열로 합친다 (근거 표시 요구사항)."""
    parts = []
    deduped = _dedupe_by_page(route_result["semantic_hits"])
    for hit in deduped[:MAX_CONTEXT_CHUNKS]:
        tag = f"[{hit.get('doc_type')}/{hit.get('doc_id')} p.{hit.get('page')}]"
        text = hit["text"]
        if len(text) > CONTEXT_CHUNK_CHAR_LIMIT:
            text = text[:CONTEXT_CHUNK_CHAR_LIMIT] + "…"
        parts.append(f"{tag}\n{text}")
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
    if route_result.get("retried"):
        lines.append(
            "   - 1차 검색 결과의 유사도가 낮아 검색 범위를 institution+products 양쪽으로 넓혀 재검색함"
        )
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


def answer_payload(question_id: str, question: str) -> dict:
    """질의 하나에 대한 응답 본문을 만든다.

    /answer 핸들러에서 떼어낸 이유: 답변 품질을 검증하려면 서버를 띄우지
    않고도 실제 답변 경로를 그대로 호출할 수 있어야 한다(scripts/eval_answers.py).
    검증이 실제와 다른 코드를 지나가면 검증이 아니다.

    route 필드는 어느 경로로 답했는지(comparison/single_product/rag)를
    남긴다. 응답 스펙에 없는 필드라 /answer에서는 빼고 내보낸다."""
    # 상품을 이름으로도 찾는다. 예전엔 질의에 상품코드(KR...)가 문자
    # 그대로 있을 때만 인식해서, "미래에셋장기성장포커스 총보수 얼마야?"
    # 같은 실제 질문이 구조화 DB에 못 닿고 텍스트 검색으로 빠졌다.
    hits = find_products(question)
    product_codes = [h[0] for h in hits]
    if is_comparison_query(question, product_codes) and len(product_codes) >= 2:
        summary, evidence = compare_products(product_codes)
        body = {
            "question_id": question_id,
            "question": question,
            "retrieved_context": summary,
            "think_trace": (
                "1. 질의 분류: 상품 비교 (상품코드 2개 이상 인식)\n"
                f"   - 인식된 상품코드: {product_codes}\n"
                "2. semantic_search 대신 구조화 DB(product_master/class_fees/"
                "class_returns) 직접 조회로 처리 (토큰 절약)\n"
                f"   - 조회 근거: {evidence}\n"
                "3. [주의] 답변 생성 단계는 아직 HyperCLOVA X API 키가 없어 "
                "실제 LLM 호출이 아니라 조회 결과를 그대로 보여주는 임시 로직임"
            ),
            "answer": f"[임시 답변 — HyperCLOVA X 연동 전 조회 결과]\n{summary}",
            "route": "comparison",
        }
        return body

    # 상품 하나에 대한 정량 질문(총보수/수익률/위험등급/규모)은 구조화
    # DB에서 바로 답한다. 텍스트 청크로 답하면 숫자가 청크 경계에 잘리거나
    # 다른 클래스 값을 집어올 수 있는데, 이 표들은 이미 클래스 단위로
    # 정확히 뽑아 두었다.
    if len(product_codes) == 1:
        intents = detect_intents(question)
        if set(intents) & {"fee", "return", "risk", "aum", "cost_projection"}:
            code = product_codes[0]
            summary, ev = product_facts(code, find_class_code(question), intents)
            body = {
                "question_id": question_id,
                "question": question,
                "retrieved_context": summary,
                "think_trace": (
                    f"1. 질의 분류: 단일 상품 정량 질의 (의도: {intents})\n"
                    f"   - 인식된 상품: {code} ({hits[0][1]})\n"
                    f"   - 지목된 클래스: {find_class_code(question) or '없음(전체)'}\n"
                    "2. semantic_search 대신 구조화 DB(product_master/class_fees/"
                    "class_returns/fund_aum) 직접 조회\n"
                    f"   - 조회 근거: {ev[:5]}\n"
                    "3. [주의] 답변 생성 단계는 아직 HyperCLOVA X API 키가 없어 "
                    "실제 LLM 호출이 아니라 조회 결과를 그대로 보여주는 임시 로직임"
                ),
                "answer": f"[임시 답변 — HyperCLOVA X 연동 전 조회 결과]\n{summary}",
                "route": "single_product",
            }
            return body

    from router import route_search  # 벡터 검색은 여기서만 필요하다

    # k=5로는 후보가 너무 얕다. 청크가 2만 개가 넘는데 검색기마다 5개씩만
    # 받으면 순위 합산(RRF)이 고를 게 없다. 검증 세트에서 k를 10으로 올리자
    # 정답이 든 청크가 상위 6개 안에 들어오는 질문이 늘었다. 답변에 실제로
    # 넣는 청크 수는 MAX_CONTEXT_CHUNKS로 여전히 6개라 토큰은 안 늘어난다.
    route_result = route_search(question, k=10)
    return {
        "question_id": question_id,
        "question": question,
        "retrieved_context": format_retrieved_context(route_result),
        "think_trace": format_think_trace(question, route_result),
        "answer": generate_answer(question, route_result),
        "route": "rag",
    }


@app.get("/answer")
def answer(question_id: str, question: str):
    body = dict(answer_payload(question_id, question))
    body.pop("route", None)  # 주최측 응답 스펙에 없는 필드라 빼고 내보낸다
    return JSONResponse(content=body)


@app.get("/health")
def health():
    return {"status": "ok"}
