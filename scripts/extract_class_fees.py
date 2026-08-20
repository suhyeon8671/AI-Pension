"""
연금 Agent 과제 - 클래스별 총보수 추출 (좌표 기반 재구성)

products 표 중 "클래스 종류 + 총보수" 수수료표가 pdfplumber extract_tables()로
뽑을 때 셀이 뭉쳐지는(줄바꿈만으로 구분된 텍스트 블록이 되는) 경우가 많다는
걸 확인했다 (194개 표, 83개 문서 - README 참고). extract_tables()의
table_settings(strategy, tolerance)만 조정해서는 열 분리가 안 됐고, 원인은
pdfplumber의 표 셀 경계 인식 실패였다.

그래서 이 스크립트는 표 재인식을 시도하는 대신, 페이지의 각 단어의 실제 좌표
(page.extract_words())를 직접 읽어서:
  1. top(y좌표)이 비슷한 단어들을 "한 줄"로 묶고
  2. 소수 3~4개(총보수/판매보수/동종유형총보수/총보수·비용) + 정수 4개 이상
     (1/2/3/5/10년 비용예시)이 있는 줄을 "데이터 행"으로 판별하고
  3. 그 줄에서 x좌표가 가장 왼쪽인 소수를 총보수로, 그 앞의 텍스트에서
     클래스 코드(괄호 안 알파벳/숫자, 예: A2, C1, Ae, C-E)를 찾는다

KR5120420039(정상 추출된 표)로 방법을 검증(A2=0.3195 등 4개 클래스 전부 일치)
했고, KR5111420047(깨진 표)에도 적용해 원본 이미지와 6개 클래스 전부 일치함을
육안으로 확인했다.

범위: 이번 1차는 "총보수 표"만 대상으로 한다 (수익률/AUM 표는 컬럼 구조가
달라서 별도 스크립트가 필요 - 다음 단계).

사용법:
    python scripts/extract_class_fees.py
    python scripts/extract_class_fees.py --output class_fees.json
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
DEFAULT_OUTPUT = os.path.join(REPO_ROOT, "class_fees.json")

NUM_RE = re.compile(r"^\d[\d,]*\.?\d*$")
DECIMAL_RE = re.compile(r"^\d+\.\d+$")
DECIMAL_FINDALL_RE = re.compile(r"\d+\.\d+")  # 앵커 없이 텍스트 뭉치 안에서 찾을 때
CLASS_CODE_RE = re.compile(r"\(([A-Za-z0-9\-]{1,8})\)")


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


def find_fee_rows_on_page(page, page_num):
    words = page.extract_words(x_tolerance=2, keep_blank_chars=False)
    lines = cluster_lines(words)
    rows = []
    for i, line in enumerate(lines):
        all_nums = [w for w in line if NUM_RE.match(w["text"])]
        decimals = [w for w in all_nums if DECIMAL_RE.match(w["text"])]
        int_like = [w for w in all_nums if w not in decimals]
        if len(decimals) < 3 or len(int_like) < 4:
            continue

        pre_text_words = [w for w in line if w["x0"] < decimals[0]["x0"]]
        class_part1 = " ".join(w["text"] for w in pre_text_words)
        class_code = None
        m = CLASS_CODE_RE.search(class_part1)
        if m:
            class_code = m.group(1)
        else:
            for offset in (1, -1):
                j = i + offset
                if 0 <= j < len(lines):
                    text = " ".join(w["text"] for w in lines[j])
                    m2 = CLASS_CODE_RE.search(text)
                    if m2:
                        class_code = m2.group(1)
                        break

        rows.append({
            "class_code": class_code,
            "total_fee": {
                "value": decimals[0]["text"],
                "page": page_num,
                "evidence": " ".join(w["text"] for w in line),
                "method": "coordinate_reconstruction",
                "confidence": 1.0 if class_code else 0.5,
            },
        })
    return rows


def candidate_pages_for_doc(doc_id):
    """이미 확인한 대로 "블롭"인 페이지만 좌표 기반 재구성 대상으로 삼는다.
    (정상 추출된 페이지를 다시 좌표로 뽑으면 오히려 불필요한 작업/오류 위험)"""
    fp = os.path.join(EXTRACTED_DIR, f"{doc_id}_tables.json")
    if not os.path.exists(fp):
        return []
    with open(fp, "r", encoding="utf-8") as f:
        tables = json.load(f)

    pages = set()
    for t in tables:
        flat = " ".join(c for row in t["data"] for c in row if c)
        if "클래스" not in flat or "총보수" not in flat:
            continue
        for row in t["data"]:
            for c in row:
                if c and len(c) >= 50 and len(DECIMAL_FINDALL_RE.findall(c)) >= 3:
                    pages.add(t["page"])
                    break
    return sorted(pages)


def process_doc(doc_id):
    pdf_candidates = glob.glob(os.path.join(DATA_DIR, doc_id, "*.pdf"))
    if not pdf_candidates:
        return []
    pages = candidate_pages_for_doc(doc_id)
    if not pages:
        return []

    results = []
    with pdfplumber.open(pdf_candidates[0]) as pdf:
        for page_num in pages:
            if page_num < 1 or page_num > len(pdf.pages):
                continue
            page = pdf.pages[page_num - 1]
            rows = find_fee_rows_on_page(page, page_num)
            for r in rows:
                r["product_code"] = doc_id
                results.append(r)
    return results


def main():
    parser = argparse.ArgumentParser(description="클래스별 총보수 좌표 기반 추출 (1차: 총보수 표만)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    doc_ids = sorted(
        os.path.basename(p).replace("_tables.json", "")
        for p in glob.glob(os.path.join(EXTRACTED_DIR, "*_tables.json"))
    )

    all_rows = []
    docs_with_hits = 0
    docs_with_missing_class_code = 0
    for doc_id in doc_ids:
        rows = process_doc(doc_id)
        if rows:
            docs_with_hits += 1
            if any(r["total_fee"]["confidence"] < 0.7 for r in rows):
                docs_with_missing_class_code += 1
        all_rows.extend(rows)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(all_rows, f, ensure_ascii=False, indent=2)

    print(f"{len(all_rows)}개 클래스 레코드 ({docs_with_hits}개 문서) → {args.output}")
    print(f"클래스 코드 인식 실패(confidence<0.7): {docs_with_missing_class_code}개 문서")


if __name__ == "__main__":
    main()
