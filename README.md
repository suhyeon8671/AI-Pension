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
│   ├── search.py                # 검색 인터페이스 (semantic_search / table_search) + CLI
│   └── router.py                # 질의 분류(제도/세제/상품/복합) + 검색 라우팅 + CLI
├── api/
│   └── server.py                # 평가용 API 서버 (FastAPI, GET /answer)
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

### 이미지 기반 페이지 재처리 (VLM)

institution 문서 중 17개(doc1, doc2, doc3, doc4, doc5, doc8, doc9, doc21, doc24,
doc27, doc28, doc30, doc31, doc32, doc37, doc54, doc56), 총 126페이지는 원본
PDF의 폰트 인코딩이 깨져 있어 텍스트 레이어 자체가 없다(글자 하나하나가
이미지로 심어진 형태 — HWP→PDF 변환 시 흔한 케이스). `pdfplumber`뿐 아니라
`pdftotext`로도 텍스트 추출이 안 되는 걸 확인했고, 반대로 poppler(`pdftoppm`)로
페이지를 이미지 렌더링하면 시각적으로는 완전히 정상 표시된다 — 즉 텍스트
추출로는 원천적으로 복구 불가능하고 VLM(이미지를 읽어서 텍스트화)이 필요한
케이스다.

대회 측 Q&A에서 "전처리·구조화 작업에 사용하는 OCR/VLM은 HyperCLOVA X가 아닌
다른 모델을 써도 무방하다"고 명시적으로 확인했다(최종 답변 생성만 HyperCLOVA X
필수). 이번 세션에서는 별도 API 비용 없이 Claude Code 세션 내에서 126페이지를
직접 읽어 텍스트/표를 옮기는 방식으로 처리했다. 페이지를 poppler로 렌더링한
뒤 VLM으로 재추출한 텍스트/표 레코드에는 `extraction_method: "vlm"` 필드를
추가해서, 자동 추출과 구분하고 원본 페이지(`doc_id` + `page`)로 항상 검증할 수
있게 했다.

향후 유사 문서가 추가되면 같은 패턴(pdftoppm 렌더링 → VLM 판독 → text/tables
JSON에 병합 → chunk_text로 청크 재생성)을 반복하면 된다. 아직 별도
스크립트(`render_page_as_image.py`)로 자동화하지는 않았고, 이번엔 수작업으로
처리했다 — 문서량이 많아지면 스크립트화 또는 Claude API 기반 자동화를 검토할 것.

### products 위험등급 표 보정 (`fix_risk_grade_tables.py`)

products 문서에서 "투자위험등급" 6단계 바(1=매우높은위험 ~ 6=매우낮은위험)는
격자선이 아니라 배경색 박스로 셀을 구분하는 그래픽이라, pdfplumber가 셀 인식에
실패하는 경우가 있다. 실패 유형은 두 가지였다: (A) 라벨 셀이 통째로 빈 문자열로
잡히는 경우, (B) 라벨 텍스트가 줄바꿈 때문에 여러 행으로 쪼개져 들어가 단일
셀로는 안 잡히는 경우.

먼저 "1~6 등급 숫자가 순서대로 있는 행은 있는데, 6개 표준 라벨
(매우높은위험/높은위험/다소높은위험/보통위험/낮은위험/매우낮은위험) 중 일부가
표 안에서 안 잡히는 표"를 자동 탐지해서 실제 영향 범위를 확인했다 — products
100개 문서, 총 6,425페이지 중 **59개 표(39개 문서)**. 이 6단계 라벨은 운용사와
무관하게 금융투자협회 표준 투자설명서 서식이라 문구·순서가 항상 동일하다는 걸
서로 다른 3개 운용사(키움/브이아이/KB) 원본 페이지를 poppler로 렌더링해 직접
대조하여 확인했다. 따라서 institution 때처럼 59페이지를 전부 VLM으로 다시
읽는 대신, 탐지된 표마다 검증된 표준 라벨 2행(등급 숫자 / 라벨) 합성 표를
추가하는 방식으로 처리했다 (`extraction_method: "canonical_fix"` 태깅, 원본
표는 삭제하지 않고 그대로 유지 — 감사 가능성 유지).

**주의**: 이건 "products 전체가 검증됐다"는 뜻이 아니다. 이번 탐지는 딱
"1~6 등급 숫자 행 + 표준 라벨 매칭 실패"라는 좁은 패턴만 잡아낸 것이고, 이
패턴에 해당하지 않는 다른 종류의 표 깨짐(예: 수수료표, 수익률표의 열 밀림 등)은
별도로 검증되지 않았다. 전수 검증이 아니라 샘플 기반으로 발견한 문제이므로,
비슷한 방식(표준화된 고정 서식 여부 확인 → 해당하면 자동 탐지·보정, 아니면
VLM 재처리 검토)의 점검을 다른 표 유형에도 확장할 필요가 있다.

**추가로 발견/수정한 것**: 표 데이터만 고쳐놓고 실제 질의 경로(`router.py`)를
안 고치면 무용지물이라는 걸 뒤늦게 확인했다. 두 가지 문제가 더 있었다.
1. `classify()`의 `use_table_search`가 세제 키워드(`TAX_KEYWORDS`)에만
   반응해서, "위험등급"처럼 표에 있는 사실을 묻는 상품 관련 질의는 애초에
   `table_search()`를 호출하지 않았다 → `TABLE_FACT_KEYWORDS`(세제 + 위험등급/
   보수/수수료/수익률/설정액)로 확장해서 해결.
2. FTS5로 "위험등급"을 검색하면, 상관없는 "변경이력" 표(개정 로그에 "투자위험등급
   변경(4등급→3등급)" 같은 문구가 있어 우연히 걸림)가 먼저 잡히는 경우가 있었다.
   합성 표에 넣은 헤더가 `"투자위험등급: N등급"`(붙여쓰기)이라 FTS5 토크나이저가
   "위험등급"과 다른 토큰으로 인식해 애초에 안 걸리기도 했다 → 헤더를
   `"투자 위험등급: N등급"`(띄어쓰기)으로 바꾸고, 상품코드가 특정된 질의는
   `table_search`에 `product_code`를 넘겨 그 상품 표만 우선 검색하도록
   `route_search()`를 수정했다. (완전히 1순위로 뜨지는 않는 케이스가 남아있지만
   `k=5` 내에는 포함되는 것을 확인함.)

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

### 질의 라우팅

`scripts/router.py`가 자연어 질의를 institution(제도/세제) / products(상품) /
복합으로 분류하고, `search.py`의 `semantic_search` / `table_search`를 알맞게
호출해서 근거 후보를 모은다. HyperCLOVA X 키가 아직 없어서 지금은 키워드
규칙 기반 분류(`classify()`)이고, 키가 생기면 이 함수만 HCX 의도분류로
교체하면 된다(`route_search()`의 반환 스키마는 그대로 유지).

```bash
python scripts/router.py --query "연금저축이랑 IRP에 넣으면 세액공제 얼마까지 되나요?"
```

## 평가용 API 서버

대회 스펙(요청/응답 규격)에 맞춘 API 서버가 `api/server.py`에 있다.

```
GET {endpoint}/answer?question_id={id}&question={질의}
-> 200 application/json
{
  "question_id": "...", "question": "...",
  "retrieved_context": "...", "think_trace": "...", "answer": "..."
}
```

로컬 실행/테스트:

```bash
uvicorn api.server:app --host 0.0.0.0 --port 8000

curl -G "http://127.0.0.1:8000/answer" \
    --data-urlencode "question_id=Q-001" \
    --data-urlencode "question=DC와 DB, 운용 주체가 어떻게 다른가요?"
```

**현재 상태**: 라우팅 + 검색(`retrieved_context`, `think_trace`)까지는 실제로
동작한다. `answer` 필드는 아직 HyperCLOVA X API 키가 없어서 진짜 LLM 생성이
아니라 검색된 근거를 발췌해서 보여주는 임시 스텁이다(`api/server.py`의
`generate_answer()`에 `TODO` 표시). 키가 발급되면 이 함수만 HCX Chat
Completions 호출로 교체하면 나머지(라우팅, 검색, 응답 스키마)는 그대로 쓸 수
있다.

**제출용 API End-point**: `[TODO — 네이버클라우드 또는 개인 서버에 배포 후
실제 접속 가능한 URL로 채울 것. 대회 규정상 README에 명시 필수]`
표준 포트(HTTP 80 / HTTPS 443, self-signed 인증서 허용) 사용, 별도 인증
헤더 없음, `/answer` 경로 고정, GET만 지원.

## TODO

- [x] ~~청크 임베딩 → Vector DB 적재 스크립트~~ (`build_vector_store.py`, 기본 프로바이더는 tfidf/LSA — 실제 HyperCLOVA X 임베딩으로 교체 필요)
- [x] ~~표 데이터 구조화~~ (`build_structured_store.py`, SQLite FTS5)
- [x] ~~이미지 기반 페이지 VLM 재처리~~ (institution 17개 문서, 126페이지 — 수작업 처리, 자동화 스크립트는 아직 없음)
- [ ] VLM 재처리 자동화 스크립트 (`render_page_as_image.py` + Claude API 등) — 향후 유사 문서 대비
- [ ] `hyperclova` 임베딩 프로바이더 실제 키로 검증 (엔드포인트/응답 스키마 확인)
- [ ] 가능하면 `sentence_transformers` 프로바이더를 huggingface.co 접근 가능한 환경에서 검증하고 tfidf보다 우선 사용 검토
- [x] ~~질의 유형 분류(제도/세제/상품/복합) 라우팅 로직~~ (`router.py`, 지금은 키워드 규칙 기반 — HCX 키 발급 후 의도분류로 교체 검토)
- [ ] HyperCLOVA X API 연동 (질문 → 검색 → 답변 생성 파이프라인) — 키 발급 대기 중
- [x] ~~평가용 API 서버~~ (`api/server.py`, `/answer` 엔드포인트 스펙대로 동작 — `answer` 필드는 HCX 연동 전까지 발췌형 스텁)
- [ ] 서버를 네이버클라우드/개인 서버에 실제 배포하고 README에 End-point URL 명시 (제출 필수 항목)
- [ ] 역질문/정보한계 대응 로직 (평가 비중 가장 높은 항목) — HCX 연동과 함께 진행
- [ ] 종합소득세/연금수령한도 등 복잡한 계산이 필요한 질의에 대한 규칙 기반 계산 로직 검토
