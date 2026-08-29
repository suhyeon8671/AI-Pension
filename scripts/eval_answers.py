"""답변 검증 세트를 실제 답변 경로로 돌려 본다.

지금까지 "데이터를 정확히 뽑았는가"는 A/B 비교와 정합성 검사로 계속
확인해 왔는데, "그래서 질문에 제대로 답하는가"는 몇 개를 손으로 쳐 보는
것 말고는 확인한 적이 없다. 평가가 자유 질의응답으로 이뤄지므로,
질문 -> 답변까지 통째로 돌려 보고 틀리는 걸 찾아내는 판이 필요하다.

eval/questions.jsonl의 각 줄:
    id, category, q            질문
    route                      기대 경로 (rag / single_product / comparison)
    code                       기대 상품코드 (있을 때만)
    must                       답변+근거에 반드시 있어야 할 문자열
    must_not                   있으면 안 되는 문자열
    note                       사람이 눈으로 볼 때 참고할 메모

기계로 확인할 수 있는 건 경로·상품코드·숫자 포함 여부까지다. 제도 질문의
답이 실제로 맞는 말인지는 자동으로 못 가리므로, 근거를 같이 찍어서 사람이
읽을 수 있게 한다(--show).

실행:
    python3 scripts/eval_answers.py            # 요약만
    python3 scripts/eval_answers.py --show     # 실패한 것의 답변 전문
    python3 scripts/eval_answers.py --show-all # 전부 다
    python3 scripts/eval_answers.py --only INST  # id 앞글자로 추리기
"""

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from api.server import answer_payload  # noqa: E402

QUESTIONS_PATH = os.path.join(REPO_ROOT, "eval", "questions.jsonl")


def load_questions(path=QUESTIONS_PATH):
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def check(case, payload):
    """(통과 여부, 실패 사유 목록)"""
    fails = []
    haystack = "\n".join(str(payload.get(k, "")) for k in
                         ("answer", "retrieved_context", "think_trace"))

    want_route = case.get("route")
    if want_route and payload.get("route") != want_route:
        fails.append(f"경로: {want_route} 기대 -> {payload.get('route')}")

    want_code = case.get("code")
    if want_code and want_code not in payload.get("think_trace", ""):
        fails.append(f"상품코드 {want_code}를 못 찾음")

    # 제도 질문에 상품이 잘못 걸리는 건 경로가 맞아도 결함이다.
    # 근거 글자에 "■"가 있는지로 보면 원문 청크에도 그 글자가 있어서
    # 헛발질한다 - 상품을 인식했는지 자체로 본다.
    if case.get("no_product") and "인식된 상품" in payload.get("think_trace", ""):
        fails.append("상품이 걸리면 안 되는 질문인데 상품을 인식함")

    for s in case.get("must", []):
        if s not in haystack:
            fails.append(f"'{s}' 없음")
    for s in case.get("must_not", []):
        if s in haystack:
            fails.append(f"'{s}' 있으면 안 됨")

    return (not fails), fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="실패한 항목의 답변 전문")
    ap.add_argument("--show-all", action="store_true", help="모든 항목의 답변 전문")
    ap.add_argument("--only", default=None, help="id가 이 글자로 시작하는 것만")
    args = ap.parse_args()

    cases = load_questions()
    if args.only:
        cases = [c for c in cases if c["id"].startswith(args.only)]

    passed, failed, errored = [], [], []
    for c in cases:
        try:
            payload = answer_payload(c["id"], c["q"])
        except Exception as e:  # 답변 경로가 터지는 것 자체가 가장 큰 결함
            errored.append((c, f"{type(e).__name__}: {e}"))
            print(f"[터짐] {c['id']} {c['q']}\n    {type(e).__name__}: {e}")
            continue
        ok, fails = check(c, payload)
        (passed if ok else failed).append((c, payload, fails))
        mark = "통과" if ok else "실패"
        print(f"[{mark}] {c['id']} ({c.get('category')}) {c['q']}"
              + (f"\n    - " + "\n    - ".join(fails) if fails else ""))
        if args.show_all or (args.show and not ok):
            print("    --- 답변 ---")
            for line in str(payload.get("answer", "")).splitlines():
                print("    " + line)
            if c.get("note"):
                print(f"    [메모] {c['note']}")
            print()

    print(f"\n통과 {len(passed)} / 실패 {len(failed)} / 터짐 {len(errored)}"
          f" (전체 {len(cases)})")
    if failed:
        print("실패:", ", ".join(c["id"] for c, _, _ in failed))
    if errored:
        print("터짐:", ", ".join(c["id"] for c, _ in errored))
    return 0


if __name__ == "__main__":
    sys.exit(main())
