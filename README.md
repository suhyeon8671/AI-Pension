# AI-Pension

미래에셋증권 AI Festival — 연금 Agent 과제 (주제1: 대고객 연금 질의 / 주제2: 상품 설명)

## 폴더 구조

```
AI-Pension/
├── data/
│   ├── institution/   # 제도·세제 문서 (원본 PDF/DOCX/XLSX/PPTX)
│   └── products/      # 투자설명서 등 상품 문서 (상품코드별 폴더, 원본 PDF)
├── extracted/
│   ├── institution/    # institution 문서 추출 결과 (표 JSON, 텍스트 JSON, 청크 JSON)
│   └── products/       # products 문서 추출 결과 (상품코드가 doc_id)
├── structured_store.db  # 표 데이터 SQLite(FTS5) — build_structured_store.py 산출물 (git 미포함, 로컬 생성)
├── vector_store/chroma/  # 청크 임베딩 Chroma persistent store — build_vector_store.py 산출물 (git 미포함, 로컬 생성)
├── scripts/
│   ├── extractors.py            # 포맷별 추출기 (PDF/DOCX/XLSX/PPTX) 공용 모듈
│   ├── extract_tables.py        # PDF 표/텍스트 추출 CLI (단일 파일, extractors.py 래퍼)
│   ├── extract_all.py           # data/ 전체 일괄 추출 + 청크 생성 (병렬)
│   ├── chunk_text.py            # 페이지별 텍스트 → RAG용 청크 JSON
│   ├── build_structured_store.py # 표 JSON → SQLite(FTS5) 구조화 저장소
│   ├── embeddings.py            # 임베딩 프로바이더 (tfidf 기본 / sentence_transformers / hyperclova)
│   ├── build_vector_store.py    # 청크 JSON → 임베딩 → Chroma 적재
│   └── search.py                # 검색 인터페이스 (semantic_search / table_search) + CLI
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

### 단일 파일 처리 (기존 방식)

```bash
pip install -r requirements.txt --break-system-packages

# 표 + 텍스트 추출 (PDF 전용, DOCX/XLSX/PPTX는 extractors.py의 extract_any() 사용)
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

### 전체 파이프라인 (일괄 추출 → 구조화 저장소 → 벡터 저장소)

```bash
pip install -r requirements.txt --break-system-packages

# 1. data/institution, data/products 전체를 병렬로 추출 + 청크 생성
#    (PDF/DOCX/XLSX/PPTX 모두 처리, extracted/ 아래에 저장, 이미 처리된 문서는 스킵)
python scripts/extract_all.py --workers 4

# 2. 표 데이터를 SQLite(FTS5)로 구조화 (세액공제 한도, 위험등급, 총보수 등 키워드 검색용)
python scripts/build_structured_store.py

# 3. 청크를 임베딩해서 Chroma 벡터 저장소에 적재
python scripts/build_vector_store.py --reset

# 4. 검색 인프라 수동 점검
python scripts/search.py --query "DC와 DB, 운용 주체가 어떻게 다른가요?" --mode semantic
python scripts/search.py --query "세액공제" --mode table
```

`scripts/search.py`의 `semantic_search()` / `table_search()`는 다음 단계(질의 라우팅,
HyperCLOVA X 파이프라인, 평가용 API 서버)에서 그대로 import해서 쓰도록 설계했다.
두 함수 모두 결과에 `doc_id` / `source_doc` / `page` 등 근거 메타데이터를 포함한다.

### 임베딩 프로바이더

`scripts/embeddings.py`가 임베딩 구현을 추상화한다 (`EMBEDDING_PROVIDER` 환경변수 또는
`--provider` 옵션으로 선택).

| 이름 | 설명 | 상태 |
|---|---|---|
| `tfidf` (기본값) | 문자 n-gram TF-IDF + TruncatedSVD(LSA), 코퍼스에 자체 fit, API 키/모델 다운로드 불필요 | 동작 확인 |
| `sentence_transformers` | 다국어 sentence-transformers 모델 로컬 추론 (`intfloat/multilingual-e5-small`) | 이 개발 환경은 `huggingface.co` 아웃바운드가 정책상 막혀 있어 모델을 받지 못해 **미검증**. 접근 가능한 환경에서 전환 권장 |
| `hyperclova` | 네이버 클로바스튜디오 임베딩 API | API 키 미발급으로 **미검증**. 키 발급 후 엔드포인트/응답 스키마 확인 필요 |

`tfidf`는 코퍼스 자체에 fit해야 하므로 상태(벡터라이저 + SVD)를
`vector_store/chroma/embedding_provider.pkl`에 저장하고, 검색 시 그대로
불러와 같은 벡터 공간에 질의를 투영한다. `build_vector_store.py`를 다시
돌리면(특히 `--reset`) 이 상태도 다시 fit되므로, 문서가 추가되면 재적재가 필요하다.

## TODO

- [x] ~~청크 임베딩 → Vector DB 적재 스크립트~~ (`build_vector_store.py`, 기본 프로바이더는 tfidf/LSA — 실제 HyperCLOVA X 임베딩으로 교체 필요)
- [x] ~~표 데이터 구조화~~ (`build_structured_store.py`, SQLite FTS5)
- [ ] 표 추출 실패 페이지용 VLM 재처리 스크립트 (`render_page_as_image.py`)
- [ ] `hyperclova` 임베딩 프로바이더 실제 키로 검증 (엔드포인트/응답 스키마 확인)
- [ ] 가능하면 `sentence_transformers` 프로바이더를 huggingface.co 접근 가능한 환경에서 검증하고 tfidf보다 우선 사용 검토
- [ ] 질의 유형 분류(제도/세제/상품/복합) 라우팅 로직
- [ ] HyperCLOVA X API 연동 (질문 → 검색 → 답변 생성 파이프라인)
- [ ] 평가용 API 서버 (question_id, question → answer, retrieved_context, think_trace)
