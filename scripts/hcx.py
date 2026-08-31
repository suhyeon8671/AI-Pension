"""HyperCLOVA X Chat Completions 호출.

왜 얇게 만드나
--------------
답을 만드는 힘든 부분은 이미 끝나 있다. 어떤 상품인지 찾고, 어느 클래스의
숫자인지 고르고, 못 사는 클래스를 빼고, 근거 페이지를 다는 일은 전부
구조화 DB 쪽에서 한다. LLM이 할 일은 그렇게 만들어진 근거를 사람 말로
옮기는 것뿐이다.

그래서 이 파일은 "호출"만 한다. 무엇을 물을지(프롬프트)와 답이 근거를
벗어났는지 보는 일(검산)은 api/server.py에 있다.

환경변수
--------
    NCP_CLOVASTUDIO_API_KEY    필수. 클로바스튜디오 API 키(nv-로 시작)
    NCP_CLOVASTUDIO_CHAT_URL   선택. 기본값은 아래 DEFAULT_URL
    NCP_CLOVASTUDIO_MODEL      선택. 기본 HCX-005
    NCP_CLOVASTUDIO_TIMEOUT    선택. 초 단위, 기본 20

.env 파일이 저장소 루트에 있으면 읽는다(키를 코드에 박지 않기 위해).

확인:
    python3 scripts/hcx.py --selftest
    python3 scripts/hcx.py --ask "연금저축과 IRP의 차이를 한 문장으로"
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(REPO_ROOT, ".env")

DEFAULT_URL = "https://clovastudio.stream.ntruss.com/v3/chat-completions"
DEFAULT_MODEL = "HCX-005"
DEFAULT_TIMEOUT = 20.0
# 실패해도 다시 걸어 볼 만한 것들. 인증 실패(401/403)나 잘못된 요청(400)은
# 다시 걸어도 같은 답이 오므로 넣지 않는다.
RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_WAITS = (1.0, 3.0)
# 엔드포인트에 아예 못 닿는 상황(방화벽/오프라인)에서는 질문마다 재시도를
# 반복할 이유가 없다. eval 36문항을 돌리면 문항마다 4초씩 기다려서 검증이
# 몇 분씩 걸렸다. 연달아 이만큼 실패하면 이 프로세스에서는 더 안 부른다.
BREAKER_LIMIT = 2
_consecutive_failures = 0
_breaker_open = False


def breaker_state():
    """(끊겼나, 연속 실패 횟수). 왜 LLM을 안 썼는지 적을 때 쓴다."""
    return _breaker_open, _consecutive_failures


def reset_breaker():
    global _consecutive_failures, _breaker_open
    _consecutive_failures, _breaker_open = 0, False


class HcxError(RuntimeError):
    """호출이 안 됐다. 답을 지어내지 말고 이걸 위로 올려야 한다."""


def load_dotenv(path=ENV_PATH):
    """.env를 환경변수로 올린다. 이미 있는 값은 덮지 않는다."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def is_configured():
    load_dotenv()
    return bool(os.environ.get("NCP_CLOVASTUDIO_API_KEY"))


def chat(messages, max_tokens=900, temperature=0.1, top_p=0.8,
         repeat_penalty=1.1, timeout=None, model=None):
    """messages(role/content 목록) -> 답변 글자.

    실패하면 HcxError를 올린다. 조용히 빈 문자열을 돌려주면 부르는 쪽이
    "답이 없다"와 "호출이 안 됐다"를 구분할 수 없고, 그 둘은 고객에게
    해야 할 말이 다르다."""
    global _consecutive_failures, _breaker_open
    load_dotenv()
    key = os.environ.get("NCP_CLOVASTUDIO_API_KEY")
    if not key:
        raise HcxError("NCP_CLOVASTUDIO_API_KEY가 없다(.env 확인)")
    if _breaker_open:
        raise HcxError(f"연속 {_consecutive_failures}회 실패해 호출을 멈춤"
                       " (엔드포인트에 닿지 않음)")

    model = model or os.environ.get("NCP_CLOVASTUDIO_MODEL", DEFAULT_MODEL)
    base = os.environ.get("NCP_CLOVASTUDIO_CHAT_URL", DEFAULT_URL).rstrip("/")
    url = base if base.endswith(model) else f"{base}/{model}"
    if timeout is None:
        timeout = float(os.environ.get("NCP_CLOVASTUDIO_TIMEOUT", DEFAULT_TIMEOUT))

    payload = {
        "messages": messages,
        "maxTokens": max_tokens,
        "temperature": temperature,
        "topP": top_p,
        "repeatPenalty": repeat_penalty,
        # 스트리밍은 안 쓴다. 평가 API가 GET 한 번에 완성된 답을 돌려주는
        # 스펙이라 중간 조각을 받을 이유가 없다.
        "includeAiFilters": True,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    last = None
    for attempt in range(len(RETRY_WAITS) + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
            out = _content_of(raw)
            _consecutive_failures = 0
            return out
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last = HcxError(f"HTTP {e.code}: {detail}")
            if e.code not in RETRY_STATUS:
                _note_failure()
                raise last
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = HcxError(f"연결 실패: {e}")
        if attempt < len(RETRY_WAITS):
            time.sleep(RETRY_WAITS[attempt])
    _note_failure()
    raise last


def _note_failure():
    global _consecutive_failures, _breaker_open
    _consecutive_failures += 1
    if _consecutive_failures >= BREAKER_LIMIT:
        _breaker_open = True


def _content_of(raw):
    try:
        data = json.loads(raw)
    except ValueError:
        raise HcxError(f"JSON이 아닌 응답: {raw[:200]}")
    # 클로바스튜디오는 성공해도 본문 status.code로 실패를 알린다.
    status = (data.get("status") or {})
    if status.get("code") not in (None, "20000"):
        raise HcxError(f"status {status.get('code')}: {status.get('message')}")
    result = data.get("result") or {}
    msg = result.get("message") or {}
    text = (msg.get("content") or "").strip()
    if not text:
        raise HcxError(f"빈 응답: {raw[:200]}")
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true",
                    help="키·엔드포인트가 실제로 통하는지 한 번 호출해 본다")
    ap.add_argument("--ask", help="아무 말이나 한 번 물어본다")
    ap.add_argument("--model")
    args = ap.parse_args()

    load_dotenv()
    key = os.environ.get("NCP_CLOVASTUDIO_API_KEY")
    print(f"키       : {'있음(' + key[:6] + '…)' if key else '없음'}")
    print(f"모델     : {args.model or os.environ.get('NCP_CLOVASTUDIO_MODEL', DEFAULT_MODEL)}")
    print(f"엔드포인트: {os.environ.get('NCP_CLOVASTUDIO_CHAT_URL', DEFAULT_URL)}")
    if not (args.selftest or args.ask):
        return 0
    if not key:
        print("\n키가 없어서 호출하지 않는다. .env에 NCP_CLOVASTUDIO_API_KEY를 넣을 것.")
        return 1

    q = args.ask or "한 단어로만 답해라. 대한민국의 수도는?"
    try:
        out = chat([{"role": "user", "content": q}], max_tokens=100,
                   model=args.model)
    except HcxError as e:
        print(f"\n실패: {e}")
        return 1
    print(f"\n질문: {q}\n답변: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
