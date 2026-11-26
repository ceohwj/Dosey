# 복약 Chat RAG 실측 고도화 v1 설계

- **상태**: Draft (Human Owner 결정 1~5 반영, 2026-11-25)
- **작성일**: 2026-11-25
- **제품 요구사항**: [chat-rag-measured-slice-requirements.md](chat-rag-measured-slice-requirements.md)
- **관련 문서 / 계약**: [#180 Guide public projection·Chat 후속 경계](../../contracts/proposed/rag-guide-chat-public-projection-180.md), [복약 챗봇 AI 응답 생성 설계](medication-chat-ai-generation-design.md), `docs/privacy-safety.md`

이 문서는 기능 명세다. 문제, 범위, 성공 기준은 제품 요구사항 문서에 있고 여기서 반복하지 않는다. 3.3절(E2E 연결)은 `chat-rag-baseline-trace` 결과를 확인한 뒤 확정한다.

## 1. 문제 정의 및 배경

- 해결하려는 문제: 제품 요구사항 1절 참고.
- 현재 동작(Baseline): 제품 요구사항 3절 참고. Chat 경로의 RAG 연결 여부는 코드 읽기로만 판단했고 실행 확인 전이다.

## 2. 포함 / 제외 범위

제품 요구사항 2절을 따른다.

## 3. 목표 설계

### 3.1 검색 ablation

같은 평가셋, 코퍼스, Case 순서로 다음 구성을 paired 비교한다.

전제: 2026-11-25 확인 결과 로컬 pgvector 인덱스는 비어 있다(제품 요구사항 3절). Dense가 들어가는 `RET-D`, `RET-H`, `RET-HR`을 비교하려면 3.5절의 인덱스 구축이 먼저 끝나야 한다. 인덱스가 준비되기 전에는 `RET-L`만 실행할 수 있다.

| Variant | 구성 |
| --- | --- |
| `RET-L` | Exact + Lexical |
| `RET-D` | Exact + Dense |
| `RET-H` | Lexical + Dense + RRF |
| `RET-HR` | `RET-H` + Rerank |

- 측정 지표: Recall@5, MRR@5, nDCG@5, No-hit 비율, 단계별 P50·P95 지연.
- 단계 범위 메모: `Exact`는 `RET-L`/`RET-D`의 공통 바닥선으로 유지해 별도 제거 대상에서 뺀다. `CRM-04`(Evidence Gate 관련성 임계값 후보 탐색: 임계값별 No-hit 비율과 오답 근거 통과율 표)는 선정 후보 구성 위에서 함께 측정한다.
- 단계 제거 규칙: 그 단계를 뺀 구성과의 Recall@5 차이에 대한 paired bootstrap 95% CI 하한이 −0.03 이상이고 P95 지연이 줄면 제거한다(제품 요구사항 4절, 결정 2).
- 평가셋 분할: DEV와 평가용을 나눈다. 허용폭과 설정은 DEV에서만 조정하고, 평가용 분할은 최종 비교에 한 번만 쓴다.

### 3.2 Citation 판정기 비교

답변의 Claim과 인용 근거 쌍에 support / contradict / neutral 라벨을 붙이고 판정기끼리 비교한다.

- Claim 분해 단위 고정: 세 판정기(로컬 NLI, LLM Judge, 사람 라벨) 모두 `chat-claim-citation-generation`이 출력한 구조화 Claim 단위를 그대로 사용하며 판정기별로 문장을 다시 쪼개지 않는다.

| 판정기 | 실행 환경 | 비고 |
| --- | --- | --- |
| 로컬 다국어 NLI (mDeBERTa-v3-base XNLI 계열 후보) | 로컬 CPU (Apple M2, 16GB) | 추론만 한다. 512토큰을 넘는 근거는 문장 단위로 나눠 최댓값을 쓴다 |
| LLM Judge | 외부 API (OpenAI, 생성 모델과 다른 모델) | Prompt와 모델 버전을 고정한다 |
| 사람 라벨 | 수동 | 정답 기준 |

- 비교 지표: 사람 라벨 대비 Macro F1, Cohen's κ, 오인용 통과율(False-Support Rate: 사람 라벨이 `neutral`/`contradict`인데 판정기가 `support`로 판정한 비율), 1건당 지연, 1건당 비용.
- 채택 규칙(결정 2 확정): `Macro F1 ≥ 0.80`, `Cohen's κ ≥ 0.70`, `False-Support Rate ≤ 0.05`, Critical Claim 오인용 0건을 모두 만족하는 판정기 중 지연과 비용이 낮은 쪽을 런타임 검증기로 채택한다. 모두 탈락하면 검증기를 Chat에 연결하지 않고 Chat 인용 표시를 막는다(fail-closed).
- LLM Judge는 OpenAI 모델 중 답변 생성 모델과 다른 모델을 쓴다(결정 5).
- NLI 모델의 실제 속도와 메모리는 `nli-smoke` 전이며, 위 하드웨어 판단은 추정이다.

### 3.3 E2E 연결 (`chat-rag-baseline-trace` 반영 완료)

목표 흐름: Frontend Chat 화면 → Backend Chat API → 검색 → 답변 생성 → Claim·Citation 검증 → 응답 저장 → 화면의 근거·인용 표시.

2026-11-26 `chat-rag-baseline-trace` 결과 확정 사항:

- **현재 경로와 DTO 상태 (기존 필드 재사용 불가)**:
  - Chat Q&A는 동기 `POST /api/v1/chat-sessions/{session_id}/messages` (`201 Created`)만 구현되어 있으며 비동기 `202`·`AI_JOB` 경로는 없다.
  - `SendChatMessageData`(`POST`), `ChatMessageData`(`GET`, `apps/backend/app/dtos/chat.py:25-56`), `ChatReplyOutput`(`apps/backend/app/services/chat_ai/__init__.py:40-44`) 모두 `citations` 필드가 없다. `content`에 인용 JSON/참조 정의를 섞으면 Frontend `AssistantMessageContent.tsx`의 마크다운 렌더링이 깨지므로, `chat-citation-contract`에서 `POST`/`GET` 응답 DTO 모두에 하위 호환 선택 필드(`citations` 등) 추가를 `CONTRACT_REQUEST`로 결정해야 한다.
  - DB에는 이미 `chat_citation`(`apps/backend/app/models/chat.py:141-161`) 테이블이 존재하나 현재 미사용 상태이며, `knowledge_chunk_id`가 `NOT NULL` FK이고 `claim_key`, `claim_kind`/Critical 여부, 판정 상태(`support`/`neutral`) 컬럼이 없어 저장 범위가 정해지면 보완이 필요하다.
- **검증기 삽입 위치와 경계 (Backend 동기 경로)**:
  - `ChatGeneratorEngine.reply()`(`apps/backend/app/services/chat_generator_engine.py:40-84`) 내부 생성 직후 + `ChatService.send_message()`의 `mark_completed()` 이전 경계에 둔다.
  - 단, `tests/contract/test_backend_ai_worker_import_boundary.py`가 Backend의 `ai_worker` import를 차단하므로 런타임 검증기 인터페이스·핵심 판정 어댑터는 `packages/rag_runtime` 또는 `apps/backend/app/services/chat_ai/` 경계에 맞추고, DB row lock(`get_session_owned_for_update`) 아래에서 검색+생성+검증기 전체에 `asyncio.timeout` 예산을 적용한다.
- **실패·timeout 상태 (`CRM-10`) 및 겹치는 파일**:
  - 검색·생성·검증기 실패나 timeout 시 `ChatGeneratorEngine.reply`가 도메인 예외(`ChatTimeoutError` / `ChatServiceUnavailableError` / `ChatGenerationFailedError`)로 매핑하고 `ChatService.send_message()`가 `commit_failed_message_pair()`로 `generation_status=FAILED` (`content=None`)를 커밋해 `COMPLETED` 저장과 초안 노출을 원천 차단한다.
  - `chat-citation-validator-wire`와 `chat-citation-api`는 `apps/backend/app/services/chat_generator_engine.py`, `apps/backend/app/services/chat.py`, `apps/backend/app/services/chat_ai/__init__.py`, `apps/backend/app/dependencies/services.py`에서 겹치므로 계약 확정 후 직렬 또는 명확한 파일 소유 분리로 진행한다.
- **합성 Baseline 실행 상태 (`NOT_RUN`)**: `CHAT_CLOSED_DEMO_RAG_ENABLED=false` 기본 비활성, 로컬 `OPENAI_API_KEY`·`SOURCE591_*` 미설정, `knowledge_chunk` 0건 및 읽기 전용 추적 경계로 인해 이번 trace에서 라이브 호출은 실행하지 않았다.

### 3.4 공유 계약·DB 스키마 영향

- Chat 응답에 Claim별 Citation을 추가하면 공유 DTO 변경일 가능성이 높다. 기존 계약을 재사용할 수 없으면 구현 전에 `CONTRACT_REQUEST`로 delta, 영향받는 소비자, 테스트, migration 영향을 제출한다. 계약 제안 문서는 그때 `docs/contracts/proposed/`에 만든다.
- 평가 실험(3.1, 3.2)과 인덱스 구축(3.5)은 기존 테이블에 데이터를 적재할 뿐 DB 스키마를 바꾸지 않는다. 로컬 DB에는 저장소의 기존 migration을 head까지 적용한다.

### 3.5 Vector 인덱스 구축

이미 있는 코드 경로를 그대로 사용하고 새 적재 경로를 만들지 않는다.

1. 로컬 PostgreSQL(pgvector)을 저장소 `docker-compose.yml` 기준으로 새로 띄우고 migration을 head까지 적용한다. 예전 팀 프로젝트 폴더의 DB와 volume은 쓰지 않는다.
2. 이용 조건이 확인된 nedrug 허가사항 XML(효능효과·용법용량·사용상의 주의사항, 약 200개 제품, 흔한 성분 군집 중심·고정 seed)을 로컬 private 디렉터리에 수집하고 acquisition manifest와 Endpoint Receipt hash로 검증한 뒤 Source·Snapshot 테이블에 기록한다. 현재 코드는 XML을 직접 내려받지 않고 미리 수집한 파일을 검증한다([runbook](../../runbooks/mfds-label-ingestion-591.md)). 원문은 저장소에 커밋하지 않는다.
3. 식약처 허가사항 문서를 `mfds-label-knowledge-chunk@1` 정책으로 청크로 나눠 `knowledge_document`와 `knowledge_chunk`에 저장한다.
4. `apps/ai_worker/admin/knowledge_evidence_index.py` 실행기로 OpenAI 임베딩을 만들어 `rag_knowledge_index`와 그 member에 적재한다.
5. 청크 수, 임베딩 모델과 차원, corpus·embedding·config hash, receipt를 결과로 남긴다.

임베딩 비용은 4단계 실행 전에 청크 수로 추정해 기록하고, 4주 합계 상한 US$30(결정 5)의 80%를 넘지 않는지 확인한다. 실행기가 현재 migration head에서 동작하는지는 아직 실행해 보지 않았다.

### 3.6 대안 비교

| 대안 | 판단 |
| --- | --- |
| 기존 합성 gold로만 평가 | 천장 효과로 단계별 차이를 변별하지 못해 제외 |
| 설계안 v1.50의 Bundle·Guard·Manifest 체계 전체 적용 | 4주 개인 프로젝트 범위에 과해 4.3절의 경량 식별자로 대체 |
| NLI 모델 fine-tuning | 로컬 디스크와 메모리 제약, 라벨 규모 부족으로 제외. 추론만 비교 |
| LLM Judge만 사용 | 비용과 재현성 문제로 로컬 NLI와 비교해 결정 |

## 4. 개인정보 · 의료 안전 조건

### 4.1 데이터 경계

- 실제 환자정보와 처방전을 쓰지 않는다. 질의는 합성으로 작성한다.
- 외부 API로는 합성 질의와 공개 문서만 보낸다.

### 4.2 Fail-closed와 의료 안전 가드

- Safety Triage와 기존 fail-closed 동작을 바꾸지 않는다.
- Critical Claim 및 근거 모순(`contradict`) 판정 Claim은 반드시 제외·폐기(fail-closed)한다. 비치명적 일반 안내의 `neutral` 판정 Claim만 "확인 필요" 표시와 함께 보여 줄 수 있고, 이 Claim도 Coverage·Precision 분모에 포함한다(결정 2).
- 검증기 또는 검색이 실패하거나 시간을 넘기면 `COMPLETED` 상태로 저장하지 않고 검증된 답변처럼 노출하지 않는다(CRM-10).
- 복용 중단, 증량, 감량 조언을 추가하지 않는다.

### 4.3 재현 식별자 (경량)

새 실험은 결과마다 다음만 기록한다: dataset version·hash, corpus hash, config hash, model·prompt version, git commit, 실행 명령, 결과 파일 경로.

## 5. 검증 및 완료 기준

### 5.1 지표 정의

| 지표 | 정의 |
| --- | --- |
| Claim Support Precision | 인용이 붙은 Claim 중 사람 라벨이 support인 비율 |
| Citation Coverage | 인용이 필요한 Claim 중 인용이 붙은 비율 |
| Critical Unsupported Claim | 의료 안전에 영향을 주는 Claim 중 support 근거가 없는 건수 |

모든 비율은 분자, 분모, Dataset Version, 95% 신뢰구간을 함께 기록한다. Critical Unsupported Claim은 Critical Claim이 30건 이상일 때만 0건 여부를 판정하고, 관측 0건이면 95% 상한(rule of three, 3/n)을 함께 적는다. 30건 미만이면 "판정 불가"로 기록한다.

### 5.2 테스트

- 단위: ablation 구성 선택, 판정기 어댑터, 지표 계산.
- 계약·통합: 3.3절이 확정되고 DTO가 바뀌면 해당 계약 테스트와 Frontend→API→Backend 통합 테스트를 추가한다.
- Eval: 검색 ablation, 판정기 비교, Safety 회귀를 `evals/` 아래에서 재현 가능하게 실행한다.

### 5.3 작업 분해 후보

[구현 계획](chat-rag-measured-slice-plan.md)과 `task.json`으로 옮길 후보다. 요구사항 ID는 [제품 요구사항](chat-rag-measured-slice-requirements.md) 11절을 따른다. CRM-12(재현 식별자)는 모든 task에 적용한다.

lane은 [Agent workflow](../../ai/workflow.md)의 역할 중 배정 후보다. 경로를 영구 소유하는 것이 아니며, 실제 파일 소유와 `ALLOWED_FILES`는 메인 Integrator가 task를 만들 때 확정한다. 한 파일은 한 시점에 한 writer만 맡는다.

| 순서 | task | lane | 목표 | 요구사항 | 선행 |
| --- | --- | --- | --- | --- | --- |
| 1 | `chat-rag-baseline-trace` | `dosey_integration_verifier` (읽기 전용) | Frontend→API→Backend→생성 경로와 baseline 응답 기록. 3.3절 확정 근거 | CRM-08~10의 전제 | 없음 |
| 1 | `mfds-source-terms` | `dosey_explore` (읽기 전용) | 식약처 Source별 이용 조건·출처 표시 요건 조사. 결정은 Human Owner | CRM-01 | 없음 |
| 1 | `nli-smoke` | `dosey_rag` | 로컬 NLI 다운로드 크기·속도·메모리 측정 | CRM-05 | 없음 |
| 1 | `local-rag-db-setup` | `dosey_executor` | 저장소 compose 기준 로컬 PostgreSQL·pgvector 기동, migration head 적용, 인덱스 실행기 smoke | CRM-13 | 없음 |
| 2 | `mfds-eval-corpus` | `dosey_rag` | 승인된 Source로 코퍼스 수집, 청크·임베딩·pgvector 인덱스 구축, hash와 receipt 고정 (3.5절) | CRM-01, CRM-13 | Source 범위 결정, `local-rag-db-setup`, 임베딩 비용 상한 |
| 2 | `chat-claim-citation-generation` | `dosey_llm` | Chat 답변을 Claim과 근거 청크 ID로 구조화해 생성하는 prompt·출력 schema | CRM-07, CRM-08 | `chat-rag-baseline-trace` |
| 2 | `chat-citation-contract` | Integrator + `dosey_backend` | 재사용 가능 여부 조사 후 필요하면 `CONTRACT_REQUEST` 제출 | CRM-08 | `chat-rag-baseline-trace` |
| 3 | `retrieval-eval-set-v2` | `dosey_rag` | 변별력 있는 확장 retrieval 평가셋과 DEV·평가 분할 | CRM-01 | `mfds-eval-corpus` |
| 4 | `retrieval-ablation` | `dosey_rag` | RET-L/D/H/HR 비교, 지연 측정, Evidence Gate 임계값 후보 탐색, 구성 결정 기록 | CRM-02~04 | `retrieval-eval-set-v2` |
| 5 | `claim-label-set` | Human Owner + `dosey_rag` | 실제 검색 근거 기반 생성 답변에서 Claim 150~200건 표본 추출(agent)과 사람 라벨(Human Owner) | CRM-06, CRM-07 | `chat-claim-citation-generation`, `retrieval-ablation` |
| 6 | `citation-judge-compare` | `dosey_rag` | NLI, LLM Judge, 사람 라벨 비교와 검증기 채택 근거 | CRM-05~07 | `nli-smoke`, `claim-label-set` |
| 7 | `chat-citation-validator-wire` | `dosey_rag` | 채택한 검증기를 Chat 경로에 연결, 실패·timeout 처리 | CRM-09, CRM-10 | `citation-judge-compare`, `chat-claim-citation-generation`, `retrieval-ablation` |
| 7 | `chat-citation-api` | `dosey_backend` | 승인된 계약대로 DTO·저장·응답 상태 구현 | CRM-08~10 | `chat-citation-contract`, 계약 결정 |
| 8 | `chat-citation-ui` | `dosey_frontend` | 출처 표시, 비치명적 `neutral` "확인 필요" 표시 및 Critical·`contradict` 제외 반영 | CRM-08, CRM-09 | `chat-citation-api`, `chat-citation-validator-wire` |
| 8 | `safety-regression` | `dosey_llm` | 변경 전후 고위험 회귀셋 비교 (API 경유 E2E 회귀 기준) | CRM-11 | `chat-citation-validator-wire`, `chat-citation-api` |
| 9 | `chat-citation-e2e-verify` | `dosey_integration_verifier` (읽기 전용) | 화면부터 저장까지 요청→응답 추적과 테스트 확인 | CRM-08~10 | 7~8 순서 완료 |
| 10 | `final-review` | `dosey_reviewer` (읽기 전용) | 고정된 snapshot의 독립 검토 | 전체 | 9 완료 및 모든 writer 정지 |
| 10 | `results-writeup` | `dosey_writer` | 측정 결과·결정·제한 사항 문서화 | 전체 | 9 완료 및 결과 확정 |

같은 순서 번호의 task는 선행 의존성이 없는 한 서로 다른 파일을 맡아 병렬로 실행할 수 있다. `chat-citation-api`와 `chat-citation-validator-wire`는 Backend Chat 경로 파일이 겹칠 수 있어, Integrator가 파일 소유를 나누지 못하면 순서대로 실행한다.

### 5.4 역할별 요약

| lane | 맡는 task | 추가되는 것 |
| --- | --- | --- |
| `dosey_rag` | `nli-smoke`, `mfds-eval-corpus`(인덱스 구축 포함), `retrieval-eval-set-v2`, `retrieval-ablation`, `claim-label-set`(표본 추출), `citation-judge-compare`, `chat-citation-validator-wire` | 평가 코퍼스와 pgvector 인덱스, 평가셋, ablation 실행기와 보고서, 판정기 어댑터와 비교 보고서, Chat용 런타임 검증기 연결 |
| `dosey_llm` | `chat-claim-citation-generation`, `safety-regression` | Claim·인용 구조화 출력 prompt와 schema, Safety 회귀 비교 |
| `dosey_backend` | `chat-citation-contract`(Integrator와 함께), `chat-citation-api` | 계약 변경 제안, Chat 응답 DTO·저장·실패 상태, 계약·통합 테스트 |
| `dosey_frontend` | `chat-citation-ui` | 출처 목록, 비치명적 `neutral` "확인 필요" 및 Critical·`contradict` 제외 화면 표시, 해당 화면 테스트 |
| `dosey_integration_verifier` | `chat-rag-baseline-trace`, `chat-citation-e2e-verify` | 변경 전후 경로 추적 보고서 |
| `dosey_explore` | `mfds-source-terms` | Source 이용 조건 조사 보고서 |
| `dosey_executor` | `local-rag-db-setup` | 재현 가능한 로컬 DB 기동 절차와 migration·실행기 smoke 결과 |
| `dosey_reviewer` | `final-review` | 독립 검토 보고서 (승인이 아니라 근거) |
| `dosey_writer` | `results-writeup` | 결과 문서와 포트폴리오 근거 정리 |
| Human Owner | 결정 1~5, Claim 사람 라벨, 계약 승인 | 판단과 정답 라벨 |

`dosey_ocr`는 이 기능에서 배정할 task가 없다. 작은 CI·설정 작업이 더 생기면 허용 파일을 지정해 `dosey_executor`에 맡긴다.
