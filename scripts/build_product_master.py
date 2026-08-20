"""
연금 Agent 과제 - 상품 마스터 테이블 생성

products 문서(extracted/products/*_text.json, *_tables.json)에서 신뢰도
높은 필드부터 단계적으로 뽑아 "상품 마스터"를 만든다. 각 필드는 단순 값이
아니라 {value, page, evidence, method, confidence} 구조로 저장해서, 나중에
Agent가 신뢰도 낮은 값은 답변에 쓰지 않도록 걸러낼 수 있게 한다.

1차 대상 필드: product_code, product_name, asset_type, risk_level.
(class/total_fee/return/AUM은 문서마다 표 레이아웃 편차가 커서 별도 단계로
분리 - 이 스크립트에는 아직 포함하지 않음.)

사용법:
    python scripts/build_product_master.py
    python scripts/build_product_master.py --output product_master.json
"""

import argparse
import glob
import json
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRODUCTS_DIR = os.path.join(REPO_ROOT, "extracted", "products")
DEFAULT_OUTPUT = os.path.join(REPO_ROOT, "product_master.json")

CANONICAL_LABELS = ["매우높은위험", "높은위험", "다소높은위험", "보통위험", "낮은위험", "매우낮은위험"]

NAME_RE = re.compile(
    r"집\s*합\s*투\s*자\s*기\s*구\s*(?:의)?\s*명\s*칭\s*[:：]?\s*(.+?)(?=\n\s*2\s*[\.\s])",
    re.S,
)
GRADE_RE = re.compile(r"(\d)\s*등급")
BRACKET_RE = re.compile(r"[\(\[]([^()\[\]]{1,20})[\)\]]")

ASSET_TYPE_VOCAB = [
    "주식혼합-재간접형", "채권혼합-재간접형", "혼합-재간접형",
    "주식혼합", "채권혼합", "재간접형", "파생형",
    "주식형", "채권형", "혼합형", "부동산형", "특별자산형",
    "국공채", "단기채", "MMF", "주식", "채권", "혼합",
]

# 표 헤더 텍스트가 명칭에 섞여 들어온 걸 감지하는 신호 (이런 게 보이면 confidence를 낮춘다)
TABLE_LEAK_MARKERS = ["펀드코드", "금융투자협회"]


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def page1_text(text_pages):
    if not text_pages:
        return ""
    return next((p["text"] for p in text_pages if p.get("page") == 1), "")


def extract_product_name(page1):
    m = NAME_RE.search(page1)
    if not m:
        return {"value": None, "page": None, "evidence": None, "method": "name_regex", "confidence": 0.0}

    raw = re.sub(r"\s+", " ", m.group(1)).strip()
    confidence = 1.0
    notes = []

    if any(marker in raw for marker in TABLE_LEAK_MARKERS):
        confidence = 0.4
        notes.append("표 헤더 텍스트 혼입 의심")
    if len(raw) > 60:
        confidence = min(confidence, 0.5)
        notes.append("비정상적으로 긴 캡처 (개명 이력 등 복수 명칭 가능성)")
    if len(raw) < 4:
        confidence = min(confidence, 0.3)
        notes.append("비정상적으로 짧은 캡처")

    return {
        "value": raw,
        "page": 1,
        "evidence": raw[:150],
        "method": "name_regex" + (" +review_flag" if notes else ""),
        "confidence": confidence,
        **({"note": "; ".join(notes)} if notes else {}),
    }


def extract_asset_type(product_name_value):
    """명칭 끝의 괄호들 중 자산유형 어휘와 일치하는 걸 찾는다. 펀드명 뒤에는
    "...(채권)(41371)"처럼 자산유형 바로 뒤에 펀드코드 괄호가 하나 더 붙는
    경우가 흔해서, 무조건 마지막 괄호가 아니라 어휘가 매칭되는 괄호를 찾는다."""
    if not product_name_value:
        return {"value": None, "page": None, "evidence": None, "method": "bracket_vocab_match", "confidence": 0.0}

    brackets = list(BRACKET_RE.finditer(product_name_value))
    if not brackets:
        return {"value": None, "page": 1, "evidence": product_name_value, "method": "bracket_vocab_match", "confidence": 0.0}

    for m in reversed(brackets):  # 뒤에서부터 어휘 매칭되는 괄호를 우선
        raw = m.group(1).strip()
        if any(v in raw for v in ASSET_TYPE_VOCAB):
            return {
                "value": raw,
                "page": 1,
                "evidence": product_name_value,
                "method": "bracket_vocab_match",
                "confidence": 1.0,
            }

    # 어휘 매칭되는 괄호가 하나도 없으면 마지막 괄호를 낮은 confidence로 보고
    raw = brackets[-1].group(1).strip()
    return {
        "value": raw,
        "page": 1,
        "evidence": product_name_value,
        "method": "bracket_vocab_match",
        "confidence": 0.4,
    }


def extract_risk_level(tables):
    for t in tables or []:
        data = t.get("data", [])
        flat = " ".join(c for row in data for c in row if c)
        m = GRADE_RE.search(flat)
        if not m:
            continue
        grade = int(m.group(1))
        if 1 <= grade <= 6:
            return {
                "value": grade,
                "page": t.get("page"),
                "evidence": CANONICAL_LABELS[grade - 1],
                "method": "risk_table_regex",
                "confidence": 1.0,
            }
    return {"value": None, "page": None, "evidence": None, "method": "risk_table_regex", "confidence": 0.0}


def build_record(doc_id):
    text_pages = load_json(os.path.join(PRODUCTS_DIR, f"{doc_id}_text.json"))
    tables = load_json(os.path.join(PRODUCTS_DIR, f"{doc_id}_tables.json"))
    p1 = page1_text(text_pages)

    name_field = extract_product_name(p1)
    asset_type_field = extract_asset_type(name_field["value"])
    risk_field = extract_risk_level(tables)

    return {
        "product_code": doc_id,
        "product_name": name_field,
        "asset_type": asset_type_field,
        "risk_level": risk_field,
        "classes": None,  # 다음 단계 (총보수/수익률/AUM과 함께 클래스별로 처리 예정)
    }


def main():
    parser = argparse.ArgumentParser(description="상품 마스터 테이블 생성 (1차: name/asset_type/risk_level)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    doc_ids = sorted(
        os.path.basename(p).replace("_text.json", "")
        for p in glob.glob(os.path.join(PRODUCTS_DIR, "*_text.json"))
    )

    records = [build_record(doc_id) for doc_id in doc_ids]

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    def low_conf(field):
        return sum(1 for r in records if r[field]["confidence"] < 0.7)

    print(f"{len(records)}개 상품 처리 → {args.output}")
    for field in ("product_name", "asset_type", "risk_level"):
        hits = sum(1 for r in records if r[field]["value"] is not None)
        print(f"  {field}: {hits}/{len(records)} 추출됨, confidence<0.7 인 것 {low_conf(field)}건")


if __name__ == "__main__":
    main()
