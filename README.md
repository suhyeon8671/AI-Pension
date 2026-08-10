# AI-Pension

미래에셋증권 AI Festival — 연금 Agent 과제 (주제1: 대고객 연금 질의 / 주제2: 상품 설명)

## 폴더 구조

```
AI-Pension/
├── data/
│   ├── institution/   # 제도·세제 문서 (원본 PDF/DOCX)
│   └── products/      # 투자설명서 등 상품 문서 (원본 PDF)
├── extracted/
│   ├── institution/    # institution 문서 추출 결과 (표 JSON, 텍스트 JSON, 청크 JSON)
│   └── products/       # products 문서 추출 결과
├── scripts/
│   ├── extract_tables.py   # PDF → 표 JSON + 페이지별 텍스트 JSON
│   └── chunk_text.py       # 페이지별 텍스트 → RAG용 청크 JSON
└── requirements.txt
```

## 처리 방침

연금 데이터는 성격이 다른 두 종류로 나뉜다.

- **정형 데이터 (표)**: 세액공제 한도, 위험등급, 총보수, 수익률처럼 숫자·조건이 명확한 표.
  `extract_tables.py`로 뽑아서 그대로 구조화 DB(또는 JSON 레코드)로 사용.
- **서술형 데이터 (텍스트)**: 제도 설명, 투자전략, 투자위험 안내 등 문장 형태의 정보.
  `chunk_text.py`로 의미 단위 청크로 나눠서 임베딩 → Vector DB에 적재 (RAG).

표는 pdfplumber로 추출 시 격자선이 있는 표는 잘 뽑히지만, 이미지로 삽입된 표나
색상 박스로 디자인된 표(예: 위험등급 바)는 깨질 수 있다. `extract_tables.py`는
빈 값만 있는 표를 자동으로 경고 출력하니, 해당 페이지는 이미지로 렌더링 후
VLM으로 재추출하는 것을 검토한다.

모든 청크와 표 레코드는 `source_doc`(원본 파일명)과 `page`(페이지 번호)를
메타데이터로 포함해서, 최종 답변에 근거 문서를 표시할 수 있게 한다.

## 사용법

```bash
pip install -r requirements.txt --break-system-packages

# 표 + 텍스트 추출
python scripts/extract_tables.py \
    --input data/institution/doc1.pdf \
    --output extracted/institution/doc1_tables.json \
    --output-text extracted/institution/doc1_text.json

# 텍스트를 청크로 분할
python scripts/chunk_text.py \
    --input extracted/institution/doc1_text.json \
    --output extracted/institution/doc1_chunks.json \
    --source-doc doc1.pdf
```

특정 페이지 범위만 처리하려면 `--start`, `--end` 옵션 사용.

## TODO

- [ ] 표 추출 실패 페이지용 VLM 재처리 스크립트 (`render_page_as_image.py`)
- [ ] 청크 임베딩 → Vector DB 적재 스크립트
- [ ] 질의 유형 분류(제도/세제/상품/복합) 라우팅 로직
- [ ] HyperCLOVA X API 연동 (질문 → 검색 → 답변 생성 파이프라인)
- [ ] 평가용 API 서버 (question_id, question → answer, retrieved_context, think_trace)
