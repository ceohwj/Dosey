# 복약 Chat RAG 실측 고도화 v1 구현 계획

- **상태**: Draft
- **작성일**: 2026-11-25
- **기준 revision**: `3297d83` (develop)
- **제품 요구사항**: [chat-rag-measured-slice-requirements.md](chat-rag-measured-slice-requirements.md)
- **기능 명세**: [chat-rag-measured-slice-design.md](chat-rag-measured-slice-design.md)
- **Human Owner 결정**: 요구사항 문서 10절 (2026-11-25 확정)

이 문서는 기능 명세 5.3절의 task를 메인 Integrator가 `task.json`으로 옮길 때 쓰는 입력이다. task 순서, lane, 요구사항 ID는 5.3절을 따르며 여기서 반복하지 않는다.

- "예상 변경 파일"은 기준 revision의 코드를 읽고 정한 **후보**다. 실제 `ALLOWED_FILES`는 Integrator가 task를 만들 때 정확한 파일 경로로 확정한다.
- "(신규)" 표시 파일의 이름은 제안이다. 기존 명명 규칙과 다르면 Integrator가 바꾼다.
- `task.json`은 Integrator가 claim을 잡을 때 `.omx/plans/dosey-agent-tasks/<task-id>/task.json`으로 만든다([task 템플릿](../../ai/task-template.md)). 이 문서에서 미리 만들지 않는다.
- 아래 검사 명령은 작성 시점에 실행하지 않았다. `docs/testing.md` 일부 명령은 저장소 폴더 정리(`3297d83`) 이전 경로(`backend/`, `ai_worker/`)를 쓰므로, 첫 task에서 `apps/` 경로로 실제 수집되는지 확인한다.

## 코드에서 확인한 출발점

| 항목 | 기준 revision에서 확인한 내용 | 확인 방법 |
| --- | --- | --- |
| Chat 검색 연결 | `apps/backend/app/services/chat_generator_engine.py`가 선택적으로 `ClosedDemoRetrievalService`(`apps/backend/app/core/closed_demo_retrieval.py`)를 호출해 evidence를 생성 입력에 넣는다 | 코드 읽기 (실행 미확인) |
| Chat 응답 DTO | `apps/backend/app/dtos/chat.py`에 Claim·Citation 필드가 없다 | 코드 읽기 |
| Claim·Citation 검증 코드 | Guide용 `apps/ai_worker/tasks/rag/claim_citation_validator.py`, `guide_claim_citation_validation.py`가 있다. 무결성 검증이며 의미 판정(support/contradict/neutral)은 하지 않는다 | 코드 읽기 |
| 검색 구성 | `evals/configs/rag-retrieval-dev-ret-{l,hr}-v1`, `rag-natural-language-retrieval-dev-ret-{l,d,h}-v1` 실행 설정이 있다 | 파일 목록 |
| 평가 CLI | `python -m ai_worker.tasks.evaluation` (`validate`, `run-dev`, `verify-result`, `gate`) | `cli.py` 읽기 |
| NLI 실행 의존성 | `pyproject.toml`에 `torch`(CPU index), `sentence-transformers>=5.2.0`가 이미 있다. 의존성 추가 없이 cross-encoder 추론이 가능할 것으로 추정한다 | 파일 읽기 (설치 상태 미확인) |
| 인덱스 실행기 | `apps/ai_worker/admin/knowledge_evidence_index.py`가 `snapshot_id`를 인자로 받는다 | 코드 읽기 (실행 미확인) |

## Step 0. 공통 검사

모든 구현 task가 끝날 때 해당 범위에 맞게 실행한다.

```bash
uv run ruff check .
uv run ruff format . --check
uv run mypy apps/backend/app apps/ai_worker
```

Worker 테스트는 DB 접근이 차단된 환경에서 실행한다.

```bash
cd "$(git rev-parse --show-toplevel)"
export REPOSITORY_ROOT="$(git rev-parse --show-toplevel)"
source scripts/ci/test_environment.sh
run_with_worker_test_environment pytest apps/ai_worker/tests/rag apps/ai_worker/tests/evaluation -q
```

Backend Chat 회귀:

```bash
uv run pytest apps/backend/app/tests/chat apps/backend/app/tests/repositories/test_chat_repository.py \
  apps/backend/app/tests/chat_ai apps/backend/app/tests/chat_integration \
  tests/contract/test_chat_ai_backend_contract.py -q
```

Frontend: `apps/frontend`에서 `pnpm lint`, `pnpm test`, `pnpm build`.

문서만 바꾼 task는 내용, 로컬 링크, `git diff --check`, 전체 diff를 확인한다.

## Step 1. 선행 조사와 환경 (순서 1)

### `chat-rag-baseline-trace` (읽기 전용)
- **읽을 파일**: `apps/frontend/src/pages/ChatPage.tsx`, `apps/frontend/src/api/chat.ts`, `apps/backend/app/apis/v1/chat_routers.py`, `apps/backend/app/dtos/chat.py`, `apps/backend/app/services/chat.py`, `apps/backend/app/services/chat_generator_engine.py`, `apps/backend/app/core/closed_demo_retrieval.py`, `apps/backend/app/services/chat_ai/`
- **변경 파일**: 없음. 결과 보고를 받아 Integrator가 기능 명세 3.3절을 채운다.
- **완료 조건**:
  - [ ] Closed demo 검색이 실제 Chat 요청에서 켜지는 조건(flag, DI)을 확인
  - [ ] 응답 DTO에 근거를 실을 수 있는 기존 필드가 있는지 판단
  - [ ] 합성 질의 1건 이상으로 baseline 응답과 지연을 기록. 실행하지 못하면 이유를 기록

### `mfds-source-terms` (읽기 전용)
- **변경 파일**: 없음. Source별 이용 조건, 출처 표시 요건, 원문 저장 가능 여부를 보고한다.
- **완료 조건**:
  - [ ] 대상은 nedrug 허가사항 XML(결정 3). 공식 이용 조건 URL, 공공누리 유형, 출처 표시 요건, 확인 날짜를 기록
  - [ ] 이용 조건이 불명확하거나 수집을 허용하지 않으면 `mfds-eval-corpus`를 `BLOCKED`로 두고 Human Owner에게 다시 결정을 요청

### `nli-smoke` (`dosey_rag`)
- **예상 변경 파일**:
  - `apps/ai_worker/tasks/evaluation/citation_nli_smoke.py` (신규)
  - `apps/ai_worker/tests/evaluation/test_citation_nli_smoke.py` (신규, 모델을 받지 않는 단위 테스트)
  - `docs/archive/testing/chat-rag-nli-smoke-<날짜>.md` (신규, 측정 기록)
- **금지**: `pyproject.toml`, `uv.lock` 변경. 의존성이 부족하면 `BLOCKED`로 보고한다. 모델 가중치를 저장소에 넣지 않는다.
- **완료 조건**:
  - [ ] 다운로드 크기, 첫 로드 시간, Claim 1건당 P50·P95 지연, 최대 메모리를 기록
  - [ ] 한국어 합성 Claim·근거 쌍 몇 건으로 라벨 출력 형식을 확인 (정확도 판단은 하지 않음)

### `local-rag-db-setup` (`dosey_executor`)
- **예상 변경 파일**: 없음이 원칙이다. 로컬 환경파일은 커밋하지 않는다. 결과는 `docs/archive/testing/chat-rag-local-db-setup-<날짜>.md` (신규)에 비밀값 없이 기록한다.
- **금지**: `docker-compose.yml`, `envs/` 아래 파일, migration 파일 변경. 필요하면 `CONTRACT_REQUEST` 또는 `BLOCKED`로 보고한다. 예전 팀 프로젝트(`ah_05_04`) 컨테이너와 volume은 건드리지 않는다.
- **완료 조건**:
  - [ ] 저장소 compose 기준 `postgres` 서비스(`pgvector/pgvector:0.8.6-pg17-bookworm`)를 새 compose 프로젝트 이름으로 기동
  - [ ] `alembic upgrade head` 후 revision이 저장소 head와 같음을 기록
  - [ ] `vector`, `pg_trgm` extension 확인
  - [ ] `python -m ai_worker.admin.knowledge_evidence_index --help` 실행으로 인자 파싱 smoke. 실제 적재는 하지 않음

## Step 2. 코퍼스, 생성 구조, 계약 (순서 2)

### `mfds-eval-corpus` (`dosey_rag`)
- **실행만 하는 기존 경로** (코드 수정 대상 아님): `apps/ai_worker/admin/mfds_product_ingestion.py`, `mfds_label_writer.py`, `knowledge_materialization.py`, `knowledge_evidence_index.py`
- **예상 변경 파일**:
  - `evals/retrieval/manifests/chat-rag-mfds-v1.corpus.json` (신규, 문서 목록과 hash, 청크 수, 임베딩 모델·차원, receipt 요약)
  - `docs/archive/testing/chat-rag-mfds-corpus-<날짜>.md` (신규, 실행 기록과 비용)
- **선행 조건**: `mfds-source-terms`에서 nedrug 허가사항 이용 조건 확인 완료.
- **범위**: nedrug 허가사항 XML(효능효과·용법용량·사용상의 주의사항), 약 200개 제품, 흔한 성분 군집 중심. 제품 목록은 고정 seed로 재현 가능하게 만든다.
- **금지**: Source 원문은 로컬 private 디렉터리에만 두고 저장소에 커밋하지 않는다(결정 3). 기존 실행기에 버그가 있으면 고치지 말고 `BLOCKED`로 보고해 별도 task로 나눈다.
- **완료 조건**:
  - [ ] 임베딩 실행 전에 청크 수 × 단가로 비용을 추정해 기록하고, 누적 비용이 US$30의 80%를 넘으면 멈춤(결정 5)
  - [ ] corpus·embedding·config hash와 receipt를 기록
  - [ ] `knowledge_chunk`, `rag_knowledge_index_member` 건수가 manifest와 일치

### `chat-claim-citation-generation` (`dosey_llm`)
- **예상 변경 파일**:
  - `apps/backend/app/services/chat_ai/prompt.py`, `schemas.py`, `generator.py`
  - `apps/backend/app/tests/chat_ai/test_schemas.py`, `test_generator.py`, `test_public_contract.py`
- **주의**: prompt 버전을 올린다(현재 `chat-prompt-v6`). 화면으로 나가는 응답 DTO는 바꾸지 않는다. 구조화 Claim은 내부 생성 결과에만 둔다.
- **완료 조건**:
  - [ ] 출력 schema가 Claim 목록과 Claim별 근거 청크 ID를 갖고, 잘못된 ID는 거부
  - [ ] Step 0 Backend Chat 회귀 통과
  - [ ] Provider 실호출은 `RUN_OPENAI_CHAT_SMOKE=1` 승인 전까지 `NOT_RUN`으로 기록

### `chat-citation-contract` (Integrator + `dosey_backend`)
- **예상 변경 파일** (계약 변경이 필요할 때만):
  - `docs/contracts/proposed/chat-claim-citation-v1.md` (신규)
  - `docs/contracts/README.md` (proposed 목록 1줄)
- **완료 조건**:
  - [ ] 재사용 가능한 기존 계약 조사 결과 기록
  - [ ] 필요하면 delta, 소비자, 테스트, migration 영향을 담은 `CONTRACT_REQUEST` 제출. Human Owner 결정 전에는 Step 5의 `chat-citation-api`를 시작하지 않는다

## Step 3. 평가셋과 검색 비교 (순서 3~4)

### `retrieval-eval-set-v2` (`dosey_rag`)
- **예상 변경 파일** (기존 `rag-retrieval-dev-v1` 구성을 따른다):
  - `evals/retrieval/cases/chat-rag-mfds-v1/` 아래 case 파일 (신규)
  - `evals/retrieval/manifests/chat-rag-mfds-v1.dataset.json`, `chat-rag-mfds-v1.critical-claim-rubric.json` (신규)
  - `evals/retrieval/evidence/chat-rag-mfds-v1.evidence-mapping.json` (신규)
  - `evals/suites/chat-rag-mfds-v1.suite.json` (신규)
- **완료 조건**:
  - [ ] 질의는 합성이고 `data_classification`이 맞게 기록됨
  - [ ] DEV와 평가용 분할을 manifest에 고정하고 hash 기록
  - [ ] `python -m ai_worker.tasks.evaluation validate`로 schema 검증

### `retrieval-ablation` (`dosey_rag`)
- **예상 변경 파일**:
  - `evals/configs/chat-rag-mfds-ret-{l,d,h,hr}-v1.execution.json` (신규 4개)
  - `docs/archive/testing/chat-rag-retrieval-ablation-<날짜>.md` (신규, 결과와 구성 결정)
  - 단계별 지연이나 paired bootstrap을 기존 runner가 지원하지 않으면 `apps/ai_worker/tasks/evaluation/` 아래 변경이 필요하다. 이 경우 파일을 특정해 Integrator에게 범위 추가를 요청한다.
- **완료 조건**:
  - [ ] 네 구성을 같은 평가셋, 코퍼스, Case 순서로 실행하고 4.3절 재현 식별자 기록
  - [ ] 단계 제거 판단과 Evidence Gate 임계값 후보표(CRM-04) 기록
  - [ ] 평가용 분할은 최종 비교에 한 번만 사용

## Step 4. Claim 라벨과 판정기 비교 (순서 5~6)

### `claim-label-set` (Human Owner + `dosey_rag`)
- **예상 변경 파일**:
  - `evals/generation/chat-claim-label-v1.json` (신규, Claim·근거 쌍과 사람 라벨)
  - 표본 추출 스크립트가 필요하면 `apps/ai_worker/tasks/evaluation/` 아래 신규 파일 1개와 테스트
- **완료 조건**:
  - [ ] 150~200건, 라벨 기준과 라벨러, 날짜 기록
  - [ ] Critical Claim 표시 포함

### `citation-judge-compare` (`dosey_rag`)
- **예상 변경 파일**:
  - `apps/ai_worker/tasks/evaluation/citation_judge_compare.py` (신규, NLI·LLM Judge 어댑터와 지표 계산)
  - `apps/ai_worker/tests/evaluation/test_citation_judge_compare.py` (신규)
  - `docs/archive/testing/chat-rag-citation-judge-<날짜>.md` (신규)
- **완료 조건**:
  - [ ] 사람 라벨 대비 Macro F1, κ, False-Support Rate, 지연, 비용을 신뢰구간과 함께 기록
  - [ ] LLM Judge는 OpenAI 중 답변 생성 모델과 다른 모델. 합성 Claim과 공개 문서만 보냄. prompt와 모델 버전 고정. 실행 전 예상 비용 기록
  - [ ] 채택 기준(Macro F1 ≥ 0.80, κ ≥ 0.70, FSR ≤ 0.05, Critical 오인용 0건)으로 판정기를 고름. 모두 탈락하면 `chat-citation-validator-wire`를 진행하지 않고 인용 표시를 막는 쪽으로 보고

## Step 5. Chat 경로 연결 (순서 7~8)

이 단계의 파일 목록은 기능 명세 3.3절이 확정된 뒤 Integrator가 다시 확정한다.

### `chat-citation-validator-wire` (`dosey_rag`)
- **예상 변경 파일**:
  - `apps/backend/app/services/chat_generator_engine.py`
  - 검증기 위치가 Worker로 정해지면 `apps/ai_worker/tasks/rag/` 아래 신규 모듈과 테스트
  - `apps/backend/app/tests/chat_ai/test_engine_adapter.py`
- **완료 조건**:
  - [ ] 검증 실패와 timeout 시 `COMPLETED`로 저장하지 않음 (CRM-10)
  - [ ] Critical Claim과 `contradict` Claim 제외를 테스트로 고정

### `chat-citation-api` (`dosey_backend`)
- **예상 변경 파일** (승인된 계약 범위 안에서만):
  - `apps/backend/app/dtos/chat.py`, `apps/backend/app/services/chat.py`, `apps/backend/app/apis/v1/chat_routers.py`
  - 저장이 필요하면 `apps/backend/app/models/chat.py`, `apps/backend/app/repositories/chat_repository.py`, `apps/backend/alembic/versions/` 신규 migration 1개
  - `apps/backend/app/tests/chat_apis/test_chat_message_api.py`, `apps/backend/app/tests/repositories/test_chat_repository.py`, `tests/contract/test_chat_ai_backend_contract.py`
- **완료 조건**:
  - [ ] 기존 필드는 바꾸지 않고 선택 필드(citations)만 추가(결정 4)
  - [ ] 계약 문서, index, DTO, 계약·통합 테스트가 같은 변경에 맞춰짐
  - [ ] migration은 forward only이며 다른 migration task와 직렬로 진행

### `chat-citation-ui` (`dosey_frontend`)
- **예상 변경 파일**:
  - `apps/frontend/src/api/chat.ts`, `apps/frontend/src/pages/ChatPage.tsx`, `apps/frontend/src/pages/ChatPage.css`
  - `apps/frontend/e2e/guide-chat-flow.spec.ts`
- **완료 조건**:
  - [ ] 출처 표시와 비치명적 `neutral` "확인 필요" 표시. Critical·`contradict` Claim은 표시하지 않음
  - [ ] `pnpm lint`, `pnpm test`, `pnpm build` 통과

### `safety-regression` (`dosey_llm`)
- **예상 변경 파일**: 결과 기록 `docs/archive/testing/chat-rag-safety-regression-<날짜>.md` (신규). 평가 코드 수정이 필요하면 범위 추가를 요청한다.
- **완료 조건**:
  - [ ] 개발 중에는 합성 safety DEV case만 쓰고, `SAFETY_REGRESSION` 93건은 최종 비교에 1회만 사용. 기준은 악화 0건
  - [ ] 응급 우선, 중복·과량 복용 case의 blocking 조건 유지

## Step 6. 검증과 정리 (순서 9~10)

| task | 변경 파일 | 완료 조건 |
| --- | --- | --- |
| `chat-citation-e2e-verify` | 없음 (읽기 전용) | 화면→API→생성→검증→저장→화면 추적 보고 |
| `final-review` | 없음 (읽기 전용) | 모든 writer 정지 후 고정 snapshot 검토. `BLOCKING`/`HIGH`/`MEDIUM`/`LOW` 분류 |
| `results-writeup` | 기능 명세와 요구사항 문서의 결과 반영, `docs/archive/testing/` 결과 문서 | 실제 수치와 미실행 항목을 구분해 기록 |

## 파일 충돌과 실행 순서

| 겹치는 파일 | 관련 task | 처리 |
| --- | --- | --- |
| `apps/backend/app/services/chat_ai/*` | `chat-claim-citation-generation`, `chat-citation-validator-wire` | 생성 task가 먼저 끝난 뒤 연결 |
| `apps/backend/app/services/chat_generator_engine.py`, `services/chat.py` | `chat-citation-validator-wire`, `chat-citation-api` | Integrator가 파일 소유를 나누지 못하면 직렬 |
| `apps/backend/alembic/versions/` | `chat-citation-api`와 동시기 다른 migration | 직렬 |
| `docs/contracts/README.md` | `chat-citation-contract`와 다른 계약 작업 | 한 번에 한 writer |
| `apps/ai_worker/tasks/evaluation/` | `nli-smoke`, `retrieval-ablation`, `claim-label-set`, `citation-judge-compare` | 신규 파일 단위로 나누고 기존 파일 수정은 한 task만 |
| 기능 명세 3.3절 | `chat-rag-baseline-trace` 결과 반영 | Integrator만 편집 |

## 완료 체크리스트
- [ ] 현재 runtime 연결과 미연결·목표 상태를 문서에 구분해 기록함
- [ ] 실제 secret·환자정보가 포함되지 않음을 확인함
- [ ] 모든 수치에 재현 식별자(4.3절)와 신뢰구간을 기록함
- [ ] 실행하지 않은 검사를 `NOT_RUN`으로 표시함
- [x] Human Owner 결정 1~5의 결과와 날짜를 요구사항 문서에 반영함 (2026-11-25)
