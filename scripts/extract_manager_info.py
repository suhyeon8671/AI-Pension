"""
연금 Agent 과제 - 운용전문인력 정보 추출 (참고용, AUM 축의 정답 아님)

products의 "운용전문인력" 표에는 "운용현황: 집합투자기구 수 / 운용규모"라는
칸이 있어 언뜻 이 상품의 AUM처럼 보이지만, 실제로는 **그 운용역(매니저)이
동시에 운용하는 모든 펀드를 합산한 규모**다 (예: KR510902511M - 송진용,
37개 펀드 합산 15,747억원). 같은 매니저가 여러 상품 문서에 등장하면 그
문서들마다 똑같은 숫자가 반복되고, 한 상품 문서 안에 매니저가 2명이면
서로 다른(그 매니저 개인 합산) 숫자가 나란히 나온다 - 이 상품 하나만의
AUM이 될 수 없다는 뜻이다.

이 상품 자체의 실제 순자산총액/설정액은 간이투자설명서 어디에도 없다는 걸
이미 확인했다(README AUM 섹션 참고). 그래서 이 스크립트는 AUM 축의 정답을
만드는 게 아니라, "운용역 합산 규모"를 참고 정보로만 남겨두는 용도다 -
class_fees/class_returns처럼 정식 6축 데이터로 취급하지 않는다.

사용법:
    python scripts/extract_manager_info.py
"""

import argparse
import glob
import json
import os
import re

import pdfplumber

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data", "products")
EXTRACTED_DIR = os.path.join(REPO_ROOT, "extracted", "products")
DEFAULT_OUTPUT = os.path.join(REPO_ROOT, "manager_info.json")

# 이름 + 생년(년 붙기도 함) + (직위 등 임의 텍스트, 0~20자) + 운용기구수(개) +
# 운용규모(콤마 포함 정수, 억/억원). 직위 라벨("본부장", "책임\n(팀장)" 등)의
# 길이가 문서마다 달라 non-greedy로 건너뛴다.
MANAGER_RE = re.compile(
    r"([가-힣]{2,5})\s+(\d{4})년?\s*.{0,20}?(\d{1,3})개\s+([\d,]+)\s*억원?"
)
CAREER_RE = re.compile(r"(\d+년\s*\d*개?월?)\s*$")


def cluster_lines(words, tol=2.5):
    words = sorted(words, key=lambda w: w["top"])
    lines = []
    for w in words:
        if lines and abs(w["top"] - lines[-1][0]["top"]) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    for line in lines:
        line.sort(key=lambda w: w["x0"])
    return lines


def find_manager_rows_on_page(page, page_num):
    words = page.extract_words(x_tolerance=5, keep_blank_chars=False)
    lines = cluster_lines(words)
    rows = []
    for line in lines:
        text = " ".join(w["text"] for w in line)
        m = MANAGER_RE.search(text)
        if not m:
            continue
        name, birth_year, fund_count, aum = m.groups()
        career_m = CAREER_RE.search(text)
        rows.append({
            "name": name,
            "birth_year": int(birth_year),
            "manager_fund_count": int(fund_count),
            "manager_aum_billion_won": aum.replace(",", ""),
            "career": career_m.group(1) if career_m else None,
            "page": page_num,
            "evidence": text,
            "method": "coordinate_reconstruction",
            "confidence": 0.8,
            "is_product_aum": False,
        })
    return rows


def candidate_pages_for_doc(doc_id, max_page):
    fp = os.path.join(EXTRACTED_DIR, f"{doc_id}_tables.json")
    if not os.path.exists(fp):
        return []
    with open(fp, "r", encoding="utf-8") as f:
        tables = json.load(f)

    pages = set()
    for t in tables:
        flat = "".join(c for row in t["data"] for c in row if c)
        if "운용전문인력" in flat and "생년" in flat:
            pages.add(t["page"])
            if t["page"] + 1 <= max_page:
                pages.add(t["page"] + 1)
    return sorted(pages)


def process_doc(doc_id):
    pdf_candidates = glob.glob(os.path.join(DATA_DIR, doc_id, "*.pdf"))
    if not pdf_candidates:
        return []

    results = []
    with pdfplumber.open(pdf_candidates[0]) as pdf:
        pages = candidate_pages_for_doc(doc_id, len(pdf.pages))
        for page_num in pages:
            if page_num < 1 or page_num > len(pdf.pages):
                continue
            rows = find_manager_rows_on_page(pdf.pages[page_num - 1], page_num)
            for r in rows:
                r["product_code"] = doc_id
                results.append(r)

    seen = set()
    deduped = []
    for r in results:
        key = (r["name"], r["birth_year"], r["manager_aum_billion_won"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped


def main():
    parser = argparse.ArgumentParser(description="운용전문인력 정보 추출 (참고용)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    doc_ids = sorted(
        os.path.basename(p).replace("_tables.json", "")
        for p in glob.glob(os.path.join(EXTRACTED_DIR, "*_tables.json"))
    )

    all_rows = []
    docs_with_hits = 0
    for doc_id in doc_ids:
        rows = process_doc(doc_id)
        if rows:
            docs_with_hits += 1
        all_rows.extend(rows)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(all_rows, f, ensure_ascii=False, indent=2)

    print(f"{len(all_rows)}개 행 ({docs_with_hits}개 문서) → {args.output}")
    print("주의: manager_aum_billion_won은 이 상품 하나의 AUM이 아니라 해당 운용역/운용사가 운용하는 전체 펀드 합산 규모(참고용)")


if __name__ == "__main__":
    main()
