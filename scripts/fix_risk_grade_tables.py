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
GRADE_RE = re.compile(r"(\d)\s*등급")


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


def extract_fund_grade(original):
    """원본(깨진) 표의 텍스트에서 이 펀드의 실제 등급(예: '2등급')을 추출.
    표 안에 헤딩 텍스트로 항상 들어있어(운용사 공통 문구 패턴) 신뢰할 수 있다."""
    flat = " ".join(c for row in original.get("data", []) for c in row if c)
    m = GRADE_RE.search(flat)
    return m.group(1) if m else None


def make_synthetic_table(original, next_index):
    grade = extract_fund_grade(original)
    # FTS5는 공백 없는 한글 연속 문자열을 하나의 토큰으로 취급한다
    # ("투자위험등급" != "위험등급" 토큰) - "위험등급"이 독립 토큰으로 검색되도록
    # 반드시 띄어쓴다.
    header_row = (
        [f"투자 위험등급: {grade}등급"] if grade else ["투자 위험등급 6단계 기준표"]
    )
    return {
        "page": original.get("page"),
        "table_index": next_index,
        "rows": 3,
        "cols": 6,
        "data": [
            header_row,
            ["1", "2", "3", "4", "5", "6"],
            list(CANONICAL_LABELS),
        ],
        "doc_id": original.get("doc_id"),
        "doc_type": original.get("doc_type"),
        "source_doc": original.get("source_doc"),
        "product_code": original.get("product_code"),
        "extraction_method": "canonical_fix",
        "fixed_table_index": original.get("table_index"),
        "note": (
            "금융투자협회 표준 투자위험등급 6단계 라벨(원본 표에서 배경색 박스 "
            "셀 인식 실패로 라벨이 소실/분산됨을 보정한 합성 표, 원본 표는 유지됨). "
            "'투자위험등급' 키워드로 검색 가능하도록 헤더 행 포함."
        ),
    }


def process_file(path, dry_run):
    with open(path, "r", encoding="utf-8") as f:
        tables = json.load(f)

    # 재실행 시 이전에 추가했던 합성 표는 지우고 새로 만든다 (중복 누적 방지,
    # 스크립트를 개선해서 다시 돌릴 때 최신 버전으로 교체되도록 idempotent하게 유지).
    original_count = len(tables)
    tables = [t for t in tables if t.get("extraction_method") != "canonical_fix"]
    had_old_fix = len(tables) != original_count

    targets = find_targets(tables)
    if not targets:
        if not dry_run and had_old_fix:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(tables, f, ensure_ascii=False, indent=2)
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
