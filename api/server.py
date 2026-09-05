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
import re
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
import input_guard  # noqa: E402
import query_analyzer  # noqa: E402
import tax_calculator  # noqa: E402
import product_ranking  # noqa: E402
import institution_facts  # noqa: E402

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
    return "\n".join(lines)


def _analysis_trace_line(analysis, analysis_how):
    """LLM 구조화 질의분석 결과를 think_trace 한 줄로 요약.

    missing_slots(질문에 안 밝혀진 조건)는 되묻지 않고(단일 턴 평가라
    되물으면 답을 못 받는다 - answer_llm.SYSTEM_PROMPT 규칙 6 참고),
    "이 조건이 안 밝혀져 있었다"는 근거로만 남긴다. 실제로 어떤 기본값을
    썼는지는 LLM이 답을 쓸 때 규칙 6에 따라 알아서 밝힌다."""
    if not analysis:
        return f"   - LLM 질의분석: {analysis_how}"
    line = (f"   - LLM 질의분석: intent={analysis.get('intent')}, "
            f"entities={analysis.get('entities')}")
    missing = analysis.get("missing_slots")
    if missing:
        line += f"\n   - 질문에 안 밝혀진 조건(되묻지 않고 무난한 기본값으로 답함): {missing}"
    return line


NO_EVIDENCE = (
    "가지고 있는 자료로는 이 질문에 답할 수 있는 근거를 찾지 못했습니다. "
    "질문을 더 구체적으로 말씀해 주시거나, 관련 제도나 상품명을 알려주시면 다시 찾아보겠습니다."
)


def compose_answer(question: str, context: str, fallback: str):
    """근거를 사람 말로 옮긴다. (답변, 어떻게 만들었는지 한 줄)

    LLM은 문장만 만든다. 숫자를 고르고 클래스를 가리고 근거 페이지를 다는
    일은 이미 앞에서 끝났고, 그 결과가 context다. LLM 답에 근거에 없는
    숫자가 섞이면 그 답은 버리고 fallback(=조회 결과 원문)을 내보낸다 -
    투박한 근거가 그럴듯한 오답보다 낫다(answer_llm.check_numbers).

    쓰지 못하는 상황(키 없음/호출 실패/검산 실패)을 조용히 넘기지 않고
    think_trace에 적는 이유는, 그때 나간 답이 LLM이 쓴 문장이 아니라
    조회 결과라는 걸 채점자가 알 수 있어야 하기 때문이다."""
    from answer_llm import generate  # HCX가 없어도 서버가 떠야 한다

    text, how = generate(question, context)
    if text:
        return text, how
    return fallback, how


# HCX가 없거나 실패했을 때 쓰는 fallback(=조회 결과 원문)이 표지·목차
# 줄을 답으로 내보내는 사고가 있었다("퇴직연금 장외채권 매수 신청
# 어떻게 해?" 실측: "퇴직연금 장외채권 매수 모바일 신청 가이드"(23자,
# 표지)가 유사도 1등(0.941)이고, 진짜 절차가 적힌 페이지("1. 금융상품
# 매매 메뉴...", 259자)는 0.638로 밀렸다 - 표지·목차처럼 짧고 질문
# 낱말만 고밀도로 담은 줄이, 낱말이 여러 문장에 흩어진 진짜 본문보다
# TF-IDF 유사도가 더 높게 나오기 쉽다).
#
# 처음엔 "글자 수 40 미만이면 제외"로 막았는데, 이것도 틀렸다 - 짧아도
# 정답인 문장("가입대상: 만 55세 이상", "위험자산 투자한도 70%")을
# 같이 버리고, 길어도 무관한 주의문·목차는 못 거른다. 그래서 "확실한
# 표지/목차 신호"만 하드 제외하고, 나머지는 "검색 관련성 + 정답이
# 실려 있을 만한 구조"로 점수를 매겨 고른다. 구조 신호 중 일부(번호
# 단계·메뉴경로 등)는 질문 유형에 따라 가산 여부가 갈린다 - 아래
# _INTENT_PATTERNS/_intent_bonus 참고.
_COVER_MARKER_RE = re.compile(r"\((?:섹션\s*)?표지\)|목차|Contents")
# "1. 퇴직연금 유상청약 신청"처럼 번호 + 명사구 한 줄로 끝나는 목차 줄의
# 모양(뒤에 콜론·화살표·구체값이 붙어 실제 설명으로 이어지면 아래
# _excerpt_score에서 목차로 안 본다).
_TOC_LINE_RE = re.compile(r"^\s*[1-9][.)]\s*[^\n:：>→]{1,30}$")
_SENTENCE_END_RE = re.compile(r"(다|요|니다|함|음|까)[.?!]\s*$")
_KEY_VALUE_RE = re.compile(r"[가-힣A-Za-z0-9()·/ ]{1,20}\s*[:：]\s*\S")
_MENU_PATH_RE = re.compile(r"\S\s*[>→]\s*\S")
_STEP_MARKER_RE = re.compile(r"(?:^|\n)\s*[1-9][.)]\s*\S{3,}")
_ACTION_WORD_RE = re.compile(
    r"선택|이동|검색|클릭|입력|신청|가능|조회|매수|매도|이체|해지|등록|접수")
_SPECIFIC_VALUE_RE = re.compile(r"\d+\s*(?:%|세|원|만원|억원|년|개월|회|건|등급)")
_EXCEPTION_MARKER_RE = re.compile(r"다만|단,|예외|제외하고|해당하지\s*않|아니면")
_RISK_ASSET_RE = re.compile(
    r"위험등급|\d\s*등급|주식형|채권형|혼합형|파생상품|MMF|자산유형")
# 비교 질문("DB와 DC 차이")에서, 실제로 비교 대상 둘 다 언급하는 문장에만
# 가산점을 준다 - 한쪽만 설명하는 문장은 "차이"에 대한 완전한 답이 못 된다.
_DUAL_SUBJECT_RE = re.compile(r"DB|DC|IRP|연금저축")

# 질문 유형 감지. 하나의 질문에 여러 유형이 동시에 걸릴 수 있다(복합
# 질문 - 아래 _pick_fallback_hits 참고). "제도 설명" 칸의 "대상·조건"도
# 여기 definition에 포함한다("가입대상이 뭐야?"는 절차가 아니라 제도
# 설명 질문이다).
_INTENT_PATTERNS = {
    "procedure": re.compile(
        r"어떻게|방법|절차|하는\s*법|신청|하려면|하는거|하려|해야\s*(?:하|되)"),
    "definition": re.compile(
        r"뭐야|무엇|이란|정의|뜻이|대상이|가입\s*대상|가입\s*조건|자격|어떤\s*사람"),
    "tax_limit": re.compile(r"세액공제|한도|얼마|몇\s*%|몇\s*퍼센트|공제"),
    "product_info": re.compile(r"위험등급|자산유형|클래스|어떤\s*상품|상품명"),
    "fee_return": re.compile(r"보수|수수료|수익률|비용"),
    "comparison": re.compile(r"차이|비교|보다|중\s*어디|어느\s*쪽|둘\s*다"),
    "recommendation": re.compile(r"추천|골라|뭐가\s*좋"),
}


def _detect_intents(question):
    q = question or ""
    return {name for name, pat in _INTENT_PATTERNS.items() if pat.search(q)}


def _intent_bonus(t, intent, has, question):
    """질문 유형별 가산점. has는 _excerpt_score가 이미 계산해 둔 공통
    신호 dict(문장종결/항목값/메뉴/단계/행동/구체값) - 유형마다 새로
    정규식을 돌리지 않고 재사용한다."""
    if intent == "procedure":
        b = 0.0
        if has["menu"]:
            b += 0.3
        if has["step"] or has["action"]:
            b += 0.3
        return b
    if intent == "definition":
        # "다만/예외" 같은 단서 절이 붙어 있으면 대상·조건·예외까지
        # 완전한 설명일 가능성이 높다.
        return 0.2 if _EXCEPTION_MARKER_RE.search(t) else 0.0
    if intent in ("tax_limit", "fee_return"):
        # 값 자체는 공통 점수에서 이미 +0.2를 받는다 - 세제·보수 질문
        # 에서는 그 값이 핵심이므로 추가로 더 얹는다.
        return 0.15 if has["value"] else 0.0
    if intent == "product_info":
        return 0.2 if _RISK_ASSET_RE.search(t) else 0.0
    if intent == "comparison":
        subs = set(_DUAL_SUBJECT_RE.findall((question or "").upper()))
        if len(subs) < 2:
            return 0.0
        present = sum(1 for s in subs if s in t.upper())
        return 0.3 if present >= 2 else 0.0
    if intent == "recommendation":
        # "사용자 조건과 상품 특성이 함께 있는 근거" - 항목:값과 구체
        # 수치가 같이 있으면 조건·특성이 둘 다 적힌 문장일 가능성이 있다.
        return 0.2 if (has["kv"] and has["value"]) else 0.0
    return 0.0


# 구조 점수만으로 고르면 검색 관련성을 사실상 무시하게 된다(무관한
# 수수료표가 항목:값+구체수치로 0.4를 받아, 정답 문장(문장종결 0.1)을
# 구조 점수만으로 이길 수 있었다). 그래서 검색 유사도(retrieval_score,
# 0~1대)를 기본값으로 깔고 구조 점수를 그 위에 더하는 보정으로 쓴다 -
# 관련성이 먼저고, 구조는 "관련성이 비슷한 후보들 중에 표지·목차를
# 밀어내고 실제 설명을 우선"시키는 재정렬용이다. 이 코퍼스의 검색
# 유사도는 관련 문서끼리도 0.4~0.9대로 흩어져 있어(실측), 0.4 근처를
# 문턱으로 잡으면 구조 보정 없이도 상당수가 걸러진다.
MIN_FALLBACK_SCORE = 0.35
_BARE_PHRASE_PENALTY = 0.25


def _excerpt_score(text, retrieval_score=0.0, page=None, intents=frozenset(), question=""):
    """이 히트를 fallback 발췌로 쓸 만한지 채점한다(검색 유사도 +
    공통 구조 보정 + 질문 유형별 가산). 확실히 표지/목차면 None(하드
    제외).

    "글자 수가 짧으면 표지"가 아니다 - PDF 추출에서 마침표가 곧잘
    사라져 "확정급여형은 회사가 적립금을 운용하는 제도"처럼 종결어미도
    콜론도 없는 짧은 정답 문장이 실제로 나온다. 그래서 이런 "벌거벗은
    명사구"는 감점만 하고 걸러내지는 않는다 - 검색 유사도가 충분히
    높으면(=질문과 실제로 관련 있으면) 감점을 받고도 살아남을 수
    있다.

    반면 "번호. 명사구" 목차 줄(예: "1. 퇴직연금 유상청약 신청")은
    감점이 아니라 그대로 하드 제외한다 - 이 모양은 "정답 문장에서
    마침표만 빠진 것"과 다르다, 애초에 절 제목을 가리키는 목차
    항목이라는 게 구조적으로 명확하고, 이 코퍼스에서 목차 줄은 질문
    낱말과 고밀도로 겹쳐 검색 유사도가 실제 정답 페이지보다도 높게
    나오기 쉬워서(실측) 감점만으로는 다 못 눌러낸다. 콜론·메뉴경로·
    구체값이 붙어 실제 설명으로 이어지면(예: "1. 가입대상: 만 55세
    이상") 이 하드 제외에서 빠진다.

    표지도 마찬가지로 명시적 마커, 그리고 "1쪽 + 벌거벗은 명사구"
    (문서 1쪽이 정확히 이 모양이면 표지 페이지일 확률이 매우 높다 -
    실측 "퇴직연금 장외채권 매수 모바일 신청 가이드", 1쪽)는 하드
    제외한다."""
    t = (text or "").strip()
    if not t:
        return None
    if _COVER_MARKER_RE.search(t):
        return None
    has = {
        "end": bool(_SENTENCE_END_RE.search(t)),
        "kv": bool(_KEY_VALUE_RE.search(t)),
        "menu": bool(_MENU_PATH_RE.search(t)),
        "step": bool(_STEP_MARKER_RE.search(t)),
        "action": bool(_ACTION_WORD_RE.search(t)),
        "value": bool(_SPECIFIC_VALUE_RE.search(t)),
    }

    single_line = "\n" not in t
    is_bare_phrase = (single_line and len(t) <= 35
                       and not (has["end"] or has["kv"] or has["menu"] or has["value"]))
    is_bare_toc = (single_line and _TOC_LINE_RE.match(t)
                   and not (has["kv"] or has["menu"] or has["value"]))

    if is_bare_toc:
        return None  # 목차 줄 - 감점이 아니라 하드 제외
    if is_bare_phrase and page == 1:
        return None  # 1쪽짜리 벌거벗은 명사구 - 표지일 확률이 매우 높다

    score = retrieval_score
    if has["end"]:
        score += 0.1
    if has["kv"]:
        score += 0.2
    if has["value"]:
        score += 0.2
    for intent in intents:
        score += _intent_bonus(t, intent, has, question)
    if is_bare_phrase:
        # 1쪽이 아니라 하드 제외는 피했지만, 그래도 벌거벗은 명사구는
        # 감점한다 - 검색 유사도가 충분히 높은 진짜 정답만 이 감점을
        # 견디고 살아남는다.
        score -= _BARE_PHRASE_PENALTY
    return score


def _best_scored_hit(hits, query, intents, exclude_ids=frozenset()):
    """intents 기준으로 채점해 최고점 히트 하나를 고른다. 문턱 미달이거나
    후보가 없으면 None. exclude_ids는 이미 다른 사실에 쓴 히트를
    복합질문 선택에서 다시 뽑지 않게 뺀다."""
    scored = []
    for rank, h in enumerate(hits):
        if id(h) in exclude_ids:
            continue
        s = _excerpt_score(h.get("text"), h.get("score", 0.0) or 0.0,
                            h.get("page"), intents, query)
        if s is not None:
            scored.append((s, -rank, h))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best_score, _, best_hit = scored[0]
    # 부동소수점 오차(0.7-0.25가 정확히 0.45가 아니라 0.44999...로 나오는
    # 등)로 문턱 바로 위 점수가 억울하게 탈락하지 않도록 아주 작은
    # 여유(epsilon)를 둔다.
    if best_score < MIN_FALLBACK_SCORE - 1e-9:
        return None
    return best_hit


def _pick_fallback_hits(hits, query="", max_chunks=3):
    """표지·목차를 하드 제외하고, 남은 것 중 (검색 유사도+구조+질문
    유형) 점수가 가장 높은 히트를 고른다. 문턱 미달이거나 후보가 아예
    없으면 빈 리스트 - 관련성 낮은 아무 문장이나 억지로 답인 척 내보내지
    않는다.

    질문이 서로 다른 사실을 두 개 이상 요구하면("IRP 가입 대상과
    중도인출 방법을 알려줘" - 가입대상=definition, 중도인출 방법=
    procedure) 청크 하나로 다 답하려 하지 않는다 - 사실(=감지된 질문
    유형)마다 제일 잘 맞는 청크를 따로 골라 최대 max_chunks개까지
    모은다. 한 사실도 문턱을 못 넘으면 그 사실은 건너뛴다."""
    intents = _detect_intents(query)
    if len(intents) >= 2:
        picked, seen = [], set()
        for intent in sorted(intents):
            hit = _best_scored_hit(hits, query, {intent}, seen)
            if hit is not None:
                picked.append(hit)
                seen.add(id(hit))
            if len(picked) >= max_chunks:
                break
        if picked:
            return picked
        # 사실별로 하나도 못 골랐으면(전부 문턱 미달) 아래 단일 선택
        # 경로로 넘어간다 - 그래도 감지된 의도는 전부 가산점에 반영한다.
    hit = _best_scored_hit(hits, query, intents)
    return [hit] if hit is not None else []


def generate_answer(query: str, route_result: dict):
    """검색(rag) 경로의 답변. (답변, 생성 방식 한 줄)"""
    hits = route_result["semantic_hits"]
    if not hits:
        return NO_EVIDENCE, "근거가 없어 정보한계로 답함"
    context = format_retrieved_context(route_result)
    picked = _pick_fallback_hits(hits, query)
    if not picked:
        # 표지가 확실하거나(1쪽 벌거벗은 명사구 등), 남은 후보의 점수가
        # 다 MIN_FALLBACK_SCORE에 못 미치면(=검색 관련성부터 낮으면)
        # 그 문장을 억지로 답인 척 내보내지 않는다 - 근거 부족을
        # 정직하게 말하는 쪽이 낫다.
        return NO_EVIDENCE, "상위 검색 결과 중 관련성 있는 근거를 찾지 못해 정보한계로 답함"
    if len(picked) == 1:
        top = picked[0]
        fallback = (f"검색된 근거({top.get('doc_id')} p.{top.get('page')})에 따르면:\n"
                    f"{top['text'][:300]}")
    else:
        # 복합 질문 - 사실별로 고른 청크를 각자 출처를 단 채로 이어붙인다.
        parts = [f"[{h.get('doc_id')} p.{h.get('page')}] {h['text'][:300]}"
                 for h in picked]
        fallback = "질문에 필요한 사실을 근거별로 나눠 찾았습니다:\n\n" + "\n\n".join(parts)
    return compose_answer(query, context, fallback)


def answer_payload(question_id: str, question: str) -> dict:
    """질의 하나에 대한 응답 본문을 만든다.

    /answer 핸들러에서 떼어낸 이유: 답변 품질을 검증하려면 서버를 띄우지
    않고도 실제 답변 경로를 그대로 호출할 수 있어야 한다(scripts/eval_answers.py).
    검증이 실제와 다른 코드를 지나가면 검증이 아니다.

    route 필드는 어느 경로로 답했는지(comparison/single_product/rag)를
    남긴다. 응답 스펙에 없는 필드라 /answer에서는 빼고 내보낸다."""
    blocked = input_guard.check(question_id, question)
    if blocked is not None:
        return blocked

    # 세제 계산 질의(세액공제/연금소득세/퇴직소득세감면/기타소득세)는
    # 상품과 무관하고 답이 순전히 규칙 계산이라, 상품 조회·검색보다
    # 먼저 본다 - 계산에 필요한 숫자(금액/나이/연차)를 질문에서 못 찾으면
    # None을 돌려주므로, 여기 안 걸리면 그냥 아래 경로로 그대로 이어진다.
    tax_summary, tax_evidence = tax_calculator.answer_from_question(question)
    if tax_summary is not None:
        answer, how = compose_answer(question, tax_summary, tax_summary)
        return {
            "question_id": question_id,
            "question": question,
            "retrieved_context": tax_summary,
            "think_trace": (
                "1. 질의 분류: 세제 계산 (질문에서 계산에 필요한 금액/나이/연차 인식)\n"
                f"   - 계산 근거(세율·한도 출처): {tax_evidence}\n"
                "2. semantic_search 대신 세제 규칙 계산기(tax_calculator) 직접 계산\n"
                f"3. 답변 생성: {how}"
            ),
            "answer": answer,
            "route": "tax_calculation",
        }

    # 상품을 이름으로도 찾는다. 예전엔 질의에 상품코드(KR...)가 문자
    # 그대로 있을 때만 인식해서, "미래에셋장기성장포커스 총보수 얼마야?"
    # 같은 실제 질문이 구조화 DB에 못 닿고 텍스트 검색으로 빠졌다.
    # 랭킹 질의 판단보다 먼저 하는 이유: "솔로몬 단기·중장기·장기
    # 국공채 중 위험도가 가장 낮은 상품은?"처럼 "여러 상품 중에서"가
    # 카테고리가 아니라 이름으로 지목한 상품들을 가리킬 수 있어서,
    # 랭킹 쪽에 그 상품코드들을 넘겨줘야 한다.
    hits = find_products(question)
    product_codes = [h[0] for h in hits]
    analysis, analysis_how = (None, "규칙 기반 상품명 매칭으로 이미 찾아 LLM 질의분석 생략")
    if not product_codes:
        # 규칙 기반 이름 매칭이 하나도 못 찾았을 때만 LLM 질의분석을
        # 부른다 - 이미 찾았으면 토큰을 쓸 이유가 없다. HCX가 뽑은
        # entities.product_names로 find_products를 다시 시도해서, 질문
        # 원문 표현이 상품명과 너무 달라(줄임말/오탈자) 규칙 매칭이
        # 놓친 경우를 보강한다.
        analysis, analysis_how = query_analyzer.analyze(question)
        if analysis:
            for cand in analysis.get("entities", {}).get("product_names", []):
                for code, name, n in find_products(cand):
                    if code not in product_codes:
                        product_codes.append(code)
                        hits.append((code, name, n))

    # 제도 비교/사실 질의(DB·DC·IRP·연금저축의 운용주체/부담금/손실부담/
    # 가입대상/중도인출/이전전환/세액공제/위험자산한도 등)는 상품과
    # 무관하고 원자적 사실 하나로 답이 정해지므로 상품 조회 다음(상품이
    # 하나도 안 걸렸을 때)에 본다. product_codes가 이미 있으면 건너뛴다 -
    # "연금저축용 클래스는 뭐야?"처럼 상품 질문에 "연금저축"이 섞여
    # 있으면 subject 인식만으로 이 경로를 먼저 타서 상품 질문을 통째로
    # 가로챈 적이 있다(실측, PROD-22 회귀).
    #
    # RAG는 TF-IDF 코퍼스가 바뀔 때마다 순위가 재계산돼 같은 질문의
    # 답이 흔들릴 수 있는데(실측: institution 문서 2개를 추가했더니
    # "DC와 DB 운용주체 차이"의 1등 근거가 통째로 바뀌었다), 이 경로는
    # 문서에서 직접 확인해 둔 값만 그대로 꺼내 쓰므로 그 위험이 없다.
    # subject(DB/DC/IRP/연금저축)나 predicate를 하나도 못 알아보면
    # None이라 아래 경로로 그대로 이어진다.
    if not product_codes:
        inst_summary, inst_evidence = institution_facts.institution_facts_answer(question)
        if inst_summary is not None:
            answer, how = compose_answer(question, inst_summary, inst_summary)
            return {
                "question_id": question_id,
                "question": question,
                "retrieved_context": inst_summary,
                "think_trace": (
                    "1. 질의 분류: 제도 비교/사실 질의 "
                    f"(인식된 제도: {institution_facts.detect_subjects(question)})\n"
                    "2. semantic_search 대신 institution_facts.json(원자적 사실) 직접 조회\n"
                    f"   - 조회 근거: {inst_evidence[:6]}\n"
                    f"3. 답변 생성: {how}"
                ),
                "answer": answer,
                "route": "institution_facts",
            }

    # 여러 상품을 조건으로 걸러 정렬하는 질의("총보수가 가장 낮은 상품
    # 5개", "위험등급 4 이하이고 총보수가 낮은 상품", "솔로몬 셋 중
    # 위험도가 가장 낮은 상품은?")는 랭킹을 비교보다 먼저 본다 - "중에서
    # 가장 낮은/제일 좋은 하나"를 묻는 건 나란히 늘어놓는 비교가 아니라
    # 하나를 골라내라는 뜻이라, is_comparison_query가 상품코드 2개
    # 이상이면 그냥 True를 주는 것과 충돌한다. product_codes가 2개
    # 이상이면 그 상품들 안에서만, 아니면(카테고리 질문이라 이름으로는
    # 못 찾음) 코퍼스 전체에서 정렬한다.
    rank_conditions = product_ranking.detect(question)
    if rank_conditions is not None:
        named = product_codes if len(product_codes) >= 2 else None
        summary, evidence = product_ranking.rank_products(rank_conditions, named_codes=named)
        answer, how = compose_answer(question, summary, summary)
        return {
            "question_id": question_id,
            "question": question,
            "retrieved_context": summary,
            "think_trace": (
                f"1. 질의 분류: 상품 랭킹/조건 검색 (조건: {rank_conditions})\n"
                + (f"   - 질문에서 지목한 상품코드: {product_codes}\n" if named else "")
                + "2. semantic_search 대신 구조화 DB(product_master/class_fees/"
                "class_returns/fund_aum)를 조건으로 걸러 정렬 (일반 고객이 "
                "가입 가능한 클래스만 사용)\n"
                f"   - 조회 근거: {evidence[:5]}\n"
                f"3. 답변 생성: {how}"
            ),
            "answer": answer,
            "route": "ranking",
        }

    if is_comparison_query(question, product_codes) and len(product_codes) >= 2:
        summary, evidence = compare_products(product_codes)
        answer, how = compose_answer(question, summary, summary)
        body = {
            "question_id": question_id,
            "question": question,
            "retrieved_context": summary,
            "think_trace": (
                "1. 질의 분류: 상품 비교 (상품코드 2개 이상 인식)\n"
                f"   - 인식된 상품코드: {product_codes}\n"
                f"{_analysis_trace_line(analysis, analysis_how)}\n"
                "2. semantic_search 대신 구조화 DB(product_master/class_fees/"
                "class_returns) 직접 조회로 처리 (토큰 절약)\n"
                f"   - 조회 근거: {evidence}\n"
                f"3. 답변 생성: {how}"
            ),
            "answer": answer,
            "route": "comparison",
        }
        return body

    # 상품 하나에 대한 정량 질문(총보수/수익률/위험등급/규모)은 구조화
    # DB에서 바로 답한다. 텍스트 청크로 답하면 숫자가 청크 경계에 잘리거나
    # 다른 클래스 값을 집어올 수 있는데, 이 표들은 이미 클래스 단위로
    # 정확히 뽑아 두었다.
    if len(product_codes) == 1:
        intents = detect_intents(question)
        if set(intents) & {"fee", "return", "risk", "aum", "cost_projection",
                          "fee_breakdown", "eligibility", "identity"}:
            code = product_codes[0]
            summary, ev = product_facts(code, find_class_code(question), intents)
            answer, how = compose_answer(question, summary, summary)
            body = {
                "question_id": question_id,
                "question": question,
                "retrieved_context": summary,
                "think_trace": (
                    f"1. 질의 분류: 단일 상품 정량 질의 (의도: {intents})\n"
                    f"   - 인식된 상품: {code} ({hits[0][1]})\n"
                    f"   - 지목된 클래스: {find_class_code(question) or '없음(전체)'}\n"
                    f"{_analysis_trace_line(analysis, analysis_how)}\n"
                    "2. semantic_search 대신 구조화 DB(product_master/class_fees/"
                    "class_returns/fund_aum) 직접 조회\n"
                    f"   - 조회 근거: {ev[:5]}\n"
                    f"3. 답변 생성: {how}"
                ),
                "answer": answer,
                "route": "single_product",
            }
            return body

    from router import route_search  # 벡터 검색은 여기서만 필요하다

    # k=5로는 후보가 너무 얕다. 청크가 2만 개가 넘는데 검색기마다 5개씩만
    # 받으면 순위 합산(RRF)이 고를 게 없다. 검증 세트에서 k를 10으로 올리자
    # 정답이 든 청크가 상위 6개 안에 들어오는 질문이 늘었다. 답변에 실제로
    # 넣는 청크 수는 MAX_CONTEXT_CHUNKS로 여전히 6개라 토큰은 안 늘어난다.
    route_result = route_search(question, k=10)
    answer, how = generate_answer(question, route_result)
    return {
        "question_id": question_id,
        "question": question,
        "retrieved_context": format_retrieved_context(route_result),
        "think_trace": (format_think_trace(question, route_result)
                        + f"\n{_analysis_trace_line(analysis, analysis_how)}"
                        + f"\n4. 답변 생성: {how}"),
        "answer": answer,
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
