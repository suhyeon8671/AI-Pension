"""근거를 사람 말로 옮기는 단계.

무엇을 LLM에 맡기고 무엇을 안 맡기나
------------------------------------
어려운 일은 이미 앞에서 다 했다. 어떤 상품인지 찾고, 어느 클래스의
숫자인지 고르고, 일반 고객이 못 사는 클래스를 빼고, 근거 페이지를 다는
것은 구조화 DB 쪽 몫이다. 여기서 LLM이 하는 일은 그 결과를 읽기 좋은
말로 옮기는 것뿐이다.

이렇게 나눈 이유는 평가 기준이 정확성·근거 완전성·근거 기반(지어내지
않기)이기 때문이다. 숫자를 LLM이 고르게 하면 이 세 가지가 전부 LLM의
운에 걸린다. 숫자를 우리가 고르고 LLM은 문장만 만들면, 틀릴 수 있는
자리가 문장 하나로 줄어든다.

그래도 문장 만들다가 숫자를 흘릴 수 있어서, 답이 나온 뒤에 한 번 더 센다
(check_numbers). 근거에 없는 숫자가 답에 있으면 그 답은 버리고 근거를
그대로 내보낸다. 이 프로젝트가 데이터에 대해 해 온 것과 같은 원칙이다 -
값으로 검산할 수 있는 기준을 두고, 검산이 안 되면 안 담는다.

실행:
    python3 scripts/answer_llm.py --demo       # 프롬프트가 어떻게 생겼는지
    python3 scripts/answer_llm.py --check      # 숫자 검산기만 시험
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hcx import HcxError, chat, is_configured  # noqa: E402

SYSTEM_PROMPT = """\
너는 연금 상품·제도 안내 도우미다. 아래 규칙을 어기면 안 된다.

1. 오직 <근거>에 있는 내용만으로 답한다. <근거>에 없는 사실, 숫자,
   상품명, 제도 내용은 어떤 경우에도 쓰지 않는다. 아는 것 같아도 쓰지 않는다.
2. 숫자는 <근거>에 적힌 그대로 옮긴다. 반올림하거나 단위를 바꾸거나
   더하고 빼서 새 숫자를 만들지 않는다.
3. 답변 안에 근거 위치를 반드시 밝힌다. <근거>에 [문서 p.쪽] 표시가 있으면
   그대로 인용하고, 상품 조회 결과면 상품코드와 작성기준일을 밝힌다.
4. <근거>로 답할 수 없으면 "가지고 있는 자료로는 확인할 수 없습니다"라고
   말하고, 무엇이 있으면 답할 수 있는지 한 줄로 알려 준다. 추측하지 않는다.
5. 어떤 상품이 오를지, 어떤 상품을 사야 하는지는 말하지 않는다. 수익률
   예측, 특정 상품 추천, 투자 권유는 하지 않는다. 대신 자료에 있는 사실
   (보수, 과거 수익률, 위험등급)을 알려 주고 판단은 고객 몫으로 남긴다.
6. 질문에 조건이 빠져 있어 답이 달라지는 경우(가입 계좌 종류, 가입 경로
   등)에는 답을 단정하지 말고 무엇을 알려 주면 되는지 되묻는다.
7. 과거 수익률을 말할 때는 그것이 미래를 보장하지 않는다는 점을 한 번
   덧붙인다.

말투는 존댓말로 간결하게. 표 대신 짧은 문장과 목록을 쓴다. 5문장 안팎.
"""

# 답에 있어도 근거와 대조할 필요가 없는 숫자. 순서를 매기는 말이나
# 연금 제도 설명에 늘 따라붙는 표현이라 근거에서 못 찾아도 지어낸 게 아니다.
SAFE_NUMBER_CONTEXTS = ("첫째", "둘째", "셋째", "1)", "2)", "3)")
RE_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _num_forms(tok):
    """같은 수를 문서가 여러 모양으로 적는다. 0.30 / 0.3 / 0.300.
    쉼표도 문서마다 있고 없다(1,789 / 1789). 대조할 때 이걸 맞춘다."""
    plain = tok.replace(",", "")
    forms = {tok, plain}
    try:
        f = float(plain)
    except ValueError:
        return forms
    if f == int(f):
        forms.add(str(int(f)))
    # 뒤에 붙은 0을 뗀 모양도 같은 수다(0.4300 -> 0.43)
    if "." in plain:
        forms.add(plain.rstrip("0").rstrip("."))
    return {x for x in forms if x}


def check_numbers(answer, context):
    """답에 있는데 근거에 없는 숫자를 돌려준다. 비어 있으면 통과."""
    hay = context.replace(",", "") + "\n" + context
    bad = []
    for m in RE_NUMBER.finditer(answer or ""):
        tok = m.group(0)
        window = answer[max(0, m.start() - 3): m.end() + 2]
        if any(s in window for s in SAFE_NUMBER_CONTEXTS):
            continue
        if not any(f in hay for f in _num_forms(tok)):
            bad.append(tok)
    return bad


def build_messages(question, context):
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content":
            f"<근거>\n{context}\n</근거>\n\n<질문>\n{question}\n</질문>\n\n"
            "위 <근거>만 써서 <질문>에 답하라."},
    ]


def generate(question, context, max_tokens=900):
    """(답변 글자, 어떻게 만들었는지 한 줄).

    LLM을 못 쓰거나 답이 근거를 벗어나면 None을 돌려준다. 부르는 쪽이
    근거를 그대로 내보내면 된다 - 틀린 문장보다 투박한 근거가 낫다."""
    if not is_configured():
        return None, "HCX 키가 없어 LLM 생성을 건너뛰고 조회 결과를 그대로 내보냄"
    if not context or context.strip() in ("", "(검색된 근거 문서 없음)"):
        return None, "근거가 비어 있어 LLM을 부르지 않음"
    try:
        text = chat(build_messages(question, context), max_tokens=max_tokens)
    except HcxError as e:
        return None, f"HCX 호출 실패({e}) - 조회 결과를 그대로 내보냄"
    bad = check_numbers(text, context)
    if bad:
        return None, (f"생성된 답에 근거에 없는 숫자 {bad[:5]}가 있어 버리고 "
                      "조회 결과를 그대로 내보냄")
    return text, "HCX가 조회 결과를 문장으로 옮김(숫자 검산 통과)"


def _demo():
    ctx = ("■ 미래에셋솔로몬단기국공채증권자투자신탁1호(채권) (KR5153420063)\n"
           "  [총보수] 연 0.32% ~ 0.65% — 가입 방법에 따라 다릅니다\n"
           "    - 퇴직연금(DC/IRP) · 온라인 (C-P2e): 0.32%, 판매보수 0.12%\n"
           "    - 창구 (A): 0.65%, 판매보수 0.45%\n"
           "    (작성 기준일 2025-02-07)")
    for m in build_messages("이 펀드 총보수 얼마야?", ctx):
        print(f"--- {m['role']}\n{m['content']}\n")


def _check_demo():
    ctx = "총보수 0.4300%, 판매보수 0.300%, 비용예시 1년 44천원 (2025-07-07)"
    cases = [
        ("총보수는 0.43%입니다.", []),
        ("총보수는 0.4300%이고 판매보수는 0.3%입니다.", []),
        ("총보수는 0.55%입니다.", ["0.55"]),
        ("1년 비용은 44천원, 3년은 138천원입니다.", ["138"]),
    ]
    ok = True
    for ans, want in cases:
        got = check_numbers(ans, ctx)
        mark = "OK " if got == want else "!! "
        ok = ok and got == want
        print(f"{mark}{ans!r}\n     근거에 없는 숫자: {got} (기대 {want})")
    return 0 if ok else 1


def _mock_demo():
    """HCX 응답을 가짜로 넣어 답변 경로를 통째로 돌려 본다.

    망이 막힌 곳에서는 진짜 호출을 못 하는데, 그렇다고 LLM 갈래를 한 번도
    안 지나가 보고 배포할 수는 없다. 답을 만드는 부분만 가짜로 바꾸고
    나머지(경로 판단 -> 조회 -> 검산 -> think_trace)는 진짜로 돌린다."""
    import answer_llm as me
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from api.server import answer_payload
    import hcx

    q = "미래에셋솔로몬단기국공채 총보수 얼마야?"
    fakes = {
        "정상 응답": (
            "미래에셋솔로몬단기국공채증권자투자신탁1호(채권)(KR5153420063)의 총보수는 "
            "가입 방법에 따라 연 0.32%에서 0.65%까지 다릅니다. 퇴직연금(DC/IRP) 계좌로 "
            "온라인 가입하시면 0.32%로 가장 낮습니다. 작성기준일은 2025-02-07입니다."),
        "숫자를 지어낸 응답": (
            "총보수는 연 0.32%~0.65%이며, 10년간 보유하면 약 71만원의 비용이 "
            "발생합니다. 업계 평균인 0.48%보다 낮은 편입니다."),
    }
    real_chat, real_cfg = me.chat, me.is_configured
    ok = True
    try:
        for name, text in fakes.items():
            hcx.reset_breaker()
            me.chat = lambda *a, **k: text
            me.is_configured = lambda: True
            p = answer_payload("MOCK", q)
            how = p["think_trace"].strip().splitlines()[-1]
            used_llm = "숫자 검산 통과" in how
            want_llm = name == "정상 응답"
            ok = ok and used_llm == want_llm
            print(f"{'OK ' if used_llm == want_llm else '!! '}{name}")
            print(f"     {how}")
            print(f"     답변 첫 줄: {p['answer'].splitlines()[0][:80]}")
    finally:
        me.chat, me.is_configured = real_chat, real_cfg
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="프롬프트 모양 보기")
    ap.add_argument("--check", action="store_true", help="숫자 검산기 시험")
    ap.add_argument("--mock", action="store_true",
                    help="가짜 HCX 응답으로 답변 경로 전체를 돌려 보기(망 없이)")
    args = ap.parse_args()
    rc = 0
    if args.demo:
        _demo()
    if args.check:
        rc |= _check_demo()
    if args.mock:
        rc |= _mock_demo()
    if not (args.demo or args.check or args.mock):
        ap.print_help()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
