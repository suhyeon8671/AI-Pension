"""
연금 Agent 과제 - 위험등급 바 표 보정 스크립트 (일회성)

pdfplumber는 선(border)이 아니라 배경색 박스로 셀을 구분하는 "투자위험등급"
6단계 바 그래픽에서 셀 인식에 실패하는 경우가 있다. 실패 유형은 두 가지다:
  (A) 라벨 셀이 통째로 빈 문자열로 인식 (매우높은위험 등 라벨 소실)
  (B) 라벨 텍스트가 줄바꿈 때문에 여러 행으로 쪼개져 들어감 (데이터는 있으나
      단일 셀로는 안 잡힘)

이 6단계 라벨("매우높은위험"~"매우낮은위험")은 금융투자협회 표준 투자설명서
서식이라 운용사와 무관하게 문구/순서가 완전히 동일하다 (직접 3개 문서
- 키움/브이아이/KB - 원본 페이지를 렌더링해 육안으로 확인함). 따라서 페이지마다
VLM으로 다시 읽는 대신, "1~6 등급 숫자 행"이 감지된 표에는 검증된 표준 라벨을
담은 합성 표(synthetic table)를 추가해 구조화 검색(structured_store.db)이
안정적으로 근거를 찾을 수 있게 한다.

원본 표는 그대로 둔다(삭제/덮어쓰기 안 함) — 투명성 유지, 감사 가능.
합성 표에는 extraction_method="canonical_fix"를 태깅해 출처를 구분한다.

사용법:
    python scripts/fix_risk_grade_tables.py            # 실제 반영
    python scripts/fix_risk_grade_tables.py --dry-run   # 대상만 확인
"""

import argparse
import glob
import json
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRODUCTS_DIR = os.path.join(REPO_ROOT, "extracted", "products")

CANONICAL_LABELS = ["매우높은위험", "높은위험", "다소높은위험", "보통위험", "낮은위험", "매우낮은위험"]


def norm(s):
    return re.sub(r"\s+", "", s or "")


def has_grade_number_row(data):
    for row in data:
        cells = [norm(c) for c in row]
        pos = -1
        ok = True
        for digit in "123456":
            found = None
            for i in range(pos + 1, len(cells)):
                if cells[i] == digit:
                    found = i
                    break
            if found is None:
                ok = False
                break
            pos = found
        if ok:
            return True
    return False


def find_targets(tables):
    targets = []
    for t in tables:
        data = t.get("data", [])
        if not data or not has_grade_number_row(data):
            continue
        flat = [norm(c) for row in data for c in row]
        matched = sum(1 for lbl in CANONICAL_LABELS if lbl in flat)
        if matched < 6:
            targets.append(t)
    return targets


def make_synthetic_table(original, next_index):
    return {
        "page": original.get("page"),
        "table_index": next_index,
        "rows": 2,
        "cols": 6,
        "data": [
            ["1", "2", "3", "4", "5", "6"],
            list(CANONICAL_LABELS),
        ],
        "doc_id": original.get("doc_id"),
        "doc_type": original.get("doc_type"),
        "source_doc": original.get("source_doc"),
        "product_code": original.get("product_code"),
        "extraction_method": "canonical_fix",
        "note": (
            "금융투자협회 표준 투자위험등급 6단계 라벨(원본 표에서 배경색 박스 "
            "셀 인식 실패로 라벨이 소실/분산됨을 보정한 합성 표, 원본 표는 유지됨)"
        ),
    }


def process_file(path, dry_run):
    with open(path, "r", encoding="utf-8") as f:
        tables = json.load(f)

    targets = find_targets(tables)
    if not targets:
        return 0

    max_index = max((t.get("table_index") or 0) for t in tables)
    next_index = max_index + 1

    for t in targets:
        synthetic = make_synthetic_table(t, next_index)
        next_index += 1
        if not dry_run:
            tables.append(synthetic)
        print(f"  {os.path.basename(path)} p.{t.get('page')} table_index={t.get('table_index')} -> 합성 표 추가 (table_index={synthetic['table_index']})")

    if not dry_run:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(tables, f, ensure_ascii=False, indent=2)

    return len(targets)


def main():
    parser = argparse.ArgumentParser(description="위험등급 바 표 보정 (합성 표 추가)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    files = sorted(glob.glob(os.path.join(PRODUCTS_DIR, "*_tables.json")))
    total = 0
    affected_docs = 0
    for fp in files:
        n = process_file(fp, args.dry_run)
        if n:
            affected_docs += 1
            total += n

    print(f"\n총 {total}개 합성 표 {'추가 예정' if args.dry_run else '추가 완료'} ({affected_docs}개 문서)")


if __name__ == "__main__":
    main()
