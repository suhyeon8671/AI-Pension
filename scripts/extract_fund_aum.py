"""
연금 Agent 과제 - 펀드 자체 재무상태표에서 순자산총계(AUM) 추출

이전에 "AUM은 원본에 없다"고 결론 냈었는데, 이건 "순자산총액"/"설정액"/
"운용규모" 같은 키워드로만 찾아서 놓친 것이었다. 실제로는 제3부.1.재무정보
"나. 재무상태표"(또는 "가. 요약재무정보")에 이 펀드 자체의 자산총계/부채총계가
있고, 순자산총계 = 자산총계 - 부채총계로 계산하면 그게 사실상 이 펀드의
AUM이다(사용자가 실제 표를 캡처해서 보여줘서 확인함, KR510902511M: 자산총계
5,711 - 부채총계 14 = 56.97억원).

주의: "자산총계"라는 단어가 이 펀드 재무상태표 말고도 완전히 다른 곳에
나온다 - 제4부(집합투자기구 관련회사) 섹션의 **운용사(회사) 자체 법인
재무제표**에도 "자산총계"가 있는데, 거기는 "유동자산/고정자산" 같은 일반
기업회계 용어를 쓴다(펀드 재무상태표는 "운용자산" 용어를 씀). 이 둘을
혼동하면 안 되므로 "유동자산"/"고정자산"이 근처에 있으면 회사 재무제표로
보고 제외한다.

숫자 단위(원/백만원)는 문서마다 다르므로 "단위: 백만원" 같은 문구를 같이
찾아서 unit 필드로 남긴다(못 찾으면 관측된 자릿수 규모로 봤을 때 대부분
"원" 단위라 기본값 "원"으로 둔다 - 확신 없는 경우 evidence로 원문을 남겨
검증 가능하게 함).

여러 회계기간(기수)이 나란히 열거되는데, 모든 문서에서 일관되게 "가장
최근 기수가 첫 번째"로 나왔다(KR510902511M/KR5111420047/KR5113420012/
KR5119450058 등 4개 문서로 확인). 그래서 asset_total/liability_total의
첫 번째 값을 "가장 최근 기준 순자산"으로 취급한다.

사용법:
    python scripts/extract_fund_aum.py
"""

import argparse
import glob
import json
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRACTED_DIR = os.path.join(REPO_ROOT, "extracted", "products")
DEFAULT_OUTPUT = os.path.join(REPO_ROOT, "fund_aum.json")

NUM_OR_DASH = r"(?:-|[\d,]+)"
ASSET_TOTAL_RE = re.compile(rf"자산총계\s*({NUM_OR_DASH}(?:\s+{NUM_OR_DASH}){{0,2}})")
LIAB_TOTAL_RE = re.compile(rf"부채총계\s*({NUM_OR_DASH}(?:\s+{NUM_OR_DASH}){{0,2}})")
UNIT_RE = re.compile(r"단위\s*[:：]\s*(백만원|천원|원)")


def to_num(token):
    token = token.strip()
    if token == "-":
        return 0
    return int(token.replace(",", ""))


def find_fund_balance_sheet(doc_id):
    fp = os.path.join(EXTRACTED_DIR, f"{doc_id}_text.json")
    if not os.path.exists(fp):
        return None
    with open(fp, "r", encoding="utf-8") as f:
        pages = json.load(f)

    for p in pages:
        t = p.get("text", "")
        if "운용자산" not in t or "자산총계" not in t:
            continue
        idx = t.find("자산총계")
        window_before = t[max(0, idx - 400):idx]
        # 운용사 자체 법인 재무제표(제4부)는 "유동자산/고정자산" 같은 일반
        # 기업회계 용어를 쓴다 - 펀드 재무상태표가 아니므로 제외.
        if "유동자산" in window_before or "고정자산" in window_before:
            continue
        return p.get("page"), t
    return None


def extract_fund_aum(doc_id):
    found = find_fund_balance_sheet(doc_id)
    if not found:
        return None
    page, t = found

    asset_m = ASSET_TOTAL_RE.search(t)
    liab_m = LIAB_TOTAL_RE.search(t)
    if not asset_m or not liab_m:
        return None

    asset_vals = [to_num(v) for v in asset_m.group(1).split()]
    liab_vals = [to_num(v) for v in liab_m.group(1).split()]
    n = min(len(asset_vals), len(liab_vals))
    if n == 0:
        return None
    net_asset_vals = [asset_vals[i] - liab_vals[i] for i in range(n)]

    unit_idx = t.find("자산총계")
    unit_m = UNIT_RE.search(t[max(0, unit_idx - 600):unit_idx])
    unit = unit_m.group(1) if unit_m else "원"

    idx = t.find("자산총계")
    evidence = t[max(0, idx - 120):idx + 60].replace("\n", " / ")

    return {
        "product_code": doc_id,
        "unit": unit,
        "asset_total": asset_vals,
        "liability_total": liab_vals,
        "net_asset_total": net_asset_vals,
        "net_asset_latest": net_asset_vals[0],
        "page": page,
        "evidence": evidence,
        "method": "text_regex",
        "confidence": 0.8,
    }


def main():
    parser = argparse.ArgumentParser(description="펀드 자체 재무상태표에서 순자산총계(AUM) 추출")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    doc_ids = sorted(
        os.path.basename(p).replace("_text.json", "")
        for p in glob.glob(os.path.join(EXTRACTED_DIR, "*_text.json"))
    )

    results = []
    for doc_id in doc_ids:
        r = extract_fund_aum(doc_id)
        if r:
            results.append(r)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"{len(results)}/{len(doc_ids)}개 문서 → {args.output}")


if __name__ == "__main__":
    main()
