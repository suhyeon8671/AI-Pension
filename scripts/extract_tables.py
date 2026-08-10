"""
연금 Agent 과제 - PDF 표 추출 스크립트

용도:
- data/institution/ 안의 제도·세제 문서 (표 위주, 예: 세액공제표)
- data/products/ 안의 투자설명서 (클래스별 비용표, 위험등급 등)

사용법:
    python scripts/extract_tables.py --input data/institution/doc1.pdf --output extracted/institution/doc1_tables.json
    python scripts/extract_tables.py --input data/products/R2.pdf --output extracted/products/R2_tables.json

주의:
- 선(격자)이 명확한 표는 잘 뽑히지만, 이미지로 삽입된 표나 색상 박스로
  디자인된 표(예: 위험등급 바)는 컬럼이 깨지거나 빈 값으로 나올 수 있음.
- 결과는 반드시 원본 페이지와 육안 대조해서 검증할 것.
  실패한 페이지는 render_page_as_image.py로 이미지 렌더링 후 VLM으로 재처리 권장.
"""

import argparse
import json
import pdfplumber


def extract_tables_from_pdf(pdf_path, page_range=None):
    """PDF에서 페이지별 표를 추출한다.

    Args:
        pdf_path: PDF 파일 경로
        page_range: (시작페이지, 끝페이지) 1-indexed, None이면 전체

    Returns:
        표 정보 dict의 리스트. 각 표는 source page, index, 행/열 수, raw data 포함.
    """
    results = []
    with pdfplumber.open(pdf_path) as pdf:
        pages = pdf.pages
        start = 1
        if page_range:
            start = page_range[0]
            pages = pdf.pages[page_range[0] - 1: page_range[1]]

        for i, page in enumerate(pages):
            page_num = start + i
            tables = page.extract_tables()
            for t_idx, table in enumerate(tables):
                cleaned = [
                    [cell.strip() if cell else "" for cell in row]
                    for row in table
                ]
                results.append({
                    "page": page_num,
                    "table_index": t_idx,
                    "rows": len(cleaned),
                    "cols": len(cleaned[0]) if cleaned else 0,
                    "data": cleaned,
                })
    return results


def extract_text_by_page(pdf_path, page_range=None):
    """서술형 텍스트를 페이지 단위로 추출한다. (RAG 청크용 원본)"""
    documents = []
    with pdfplumber.open(pdf_path) as pdf:
        pages = pdf.pages
        start = 1
        if page_range:
            start = page_range[0]
            pages = pdf.pages[page_range[0] - 1: page_range[1]]

        for i, page in enumerate(pages):
            page_num = start + i
            text = page.extract_text()
            if text:
                documents.append({"page": page_num, "text": text})
    return documents


def main():
    parser = argparse.ArgumentParser(description="연금 PDF 표/텍스트 추출기")
    parser.add_argument("--input", required=True, help="입력 PDF 경로")
    parser.add_argument("--output", required=True, help="출력 JSON 경로 (표)")
    parser.add_argument("--output-text", help="출력 JSON 경로 (서술형 텍스트, 선택)")
    parser.add_argument("--start", type=int, default=None, help="시작 페이지 (1-indexed)")
    parser.add_argument("--end", type=int, default=None, help="끝 페이지 (포함)")
    args = parser.parse_args()

    page_range = (args.start, args.end) if args.start and args.end else None

    tables = extract_tables_from_pdf(args.input, page_range)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(tables, f, ensure_ascii=False, indent=2)
    print(f"[표] {len(tables)}개 추출 → {args.output}")

    # 표가 없거나 깨진 페이지 경고
    empty_or_broken = [
        t for t in tables
        if all(all(c == "" for c in row) for row in t["data"])
    ]
    if empty_or_broken:
        pages = sorted(set(t["page"] for t in empty_or_broken))
        print(f"  ⚠ 빈 표 감지된 페이지: {pages} → 이미지 렌더링 후 VLM 재처리 검토 필요")

    if args.output_text:
        texts = extract_text_by_page(args.input, page_range)
        with open(args.output_text, "w", encoding="utf-8") as f:
            json.dump(texts, f, ensure_ascii=False, indent=2)
        print(f"[텍스트] {len(texts)}페이지 추출 → {args.output_text}")


if __name__ == "__main__":
    main()
