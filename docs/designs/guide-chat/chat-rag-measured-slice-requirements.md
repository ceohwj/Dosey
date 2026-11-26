# 복약 Chat RAG 실측 고도화 v1 — 제품 요구사항

- **상태**: 선정됨 (Human Owner 결정 1~5 확정, 2026-11-25) · 구현 착수 전
- **작성일**: 2026-11-25
- **기능 명세**: [chat-rag-measured-slice-design.md](chat-rag-measured-slice-design.md)
- **구현 계획**: [chat-rag-measured-slice-plan.md](chat-rag-measured-slice-plan.md)
- **결정 기록**: [ADR 0004](../../adr/0004-chat-rag-measured-slice-selection.md), [PRD.md](../../../PRD.md)에서 이 문서를 선정 기능의 정본으로 연결한다.

이 문서는 PRD.md가 기능마다 먼저 기록하라고 정한 7개 항목(사용자 문제, 포함/제외 범위, baseline, 목표, 평가 데이터와 재현 명령, 통합 범위, 안전 조건)을 채운 것이다. 2026-11-25 Human Owner 인터뷰로 10절 결정 1~5를 확정했다. 이 문서는 공유 계약 변경, Source 이용 승인, 공개 활성화를 승인하지 않는다. 요구사항별 ID와 팀 정의서 연결, 검증 방법은 11절에 있다.

## 1. 사용자 문제

- 복약 Chat 답변이 제시한 근거와 실제로 일치하는지 측정한 적이 없다.
- RAG 검색 파이프라인(Exact → Lexical → Dense → RRF → Rerank → Evidence Gate)의 각 단계가 품질과 지연에 얼마나 기여하는지 근거가 없다. 그래서 파이프라인이 과한지 판단할 수 없다.
- 기존 합성 DEV gold(`rag-natural-language-retrieval-dev@1.0.0`)는 Reranker 없이도 Recall@5가 0.983이다. 천장에 가까워 개선을 변별하지 못한다. 근거는 [rag-korean-reranker-benchmark-20261124.md](../../archive/testing/rag-korean-reranker-benchmark-20261124.md)이며, 이 결과는 DEV_DIAGNOSTIC_ONLY다.
- 팀 요구사항 정의서 v3의 AI 평가 항목(EVAL-RAG·CIT·NLI·SAF)은 모두 미평가다. ISS-AI-005(관련성 기준), ISS-AI-006(NLI 판정 기준), ISS-AI-009(평가 기준)는 보류 상태다.

## 2. 포함 / 제외 범위

포함:

- 사용자 흐름 하나: 복약 Chat Q&A (질문 → 검색 → 답변 생성 → Claim·Citation 검증 → 화면 표시)
- 식약처 공개 의약품 문서 기반 평가 코퍼스와, 변별력 있는 평가셋 구축
- 검색 단계별 ablation과 지연 측정, 그리고 측정에 근거한 파이프라인 구성 결정
- Citation 판정기 비교(로컬 NLI · LLM Judge · 사람 라벨)와 런타임 검증기 채택
- 기존 Safety 경계의 회귀 측정

제외:

- OCR, 약품 식별(Resolver), 자동 Guide 생성
- Safety Triage 분류 로직 변경
- 모델 fine-tuning
- 팀 설계안 v1.50이 `DISABLED`로 둔 범위: 처방약–처방약 상호작용, 음식·보충제 상호작용 등
- Production 공개, 실제 사용자 대상 활성화

## 3. 현재 baseline

| 항목 | 상태 | 확인 수준 |
| --- | --- | --- |
| Chat 응답 경로 | Backend `apps/backend/app/services/chat.py`가 OpenAI 호출로 답변 생성. `CHAT_CLOSED_DEMO_RAG_ENABLED` 활성 시 `apps/backend/app/dependencies/services.py`에서 `closed_demo_retrieval_service`를 `ChatGeneratorEngine`에 연결해 closed-demo 근거 프롬프트 사용 | 코드 읽기 기준. 실행 미확인 |
| Chat의 RAG 연결 | Closed-Demo RAG 검색 경로는 DI에 배선돼 있으나, 일반 Worker RAG 파이프라인·Claim Citation 검증 연결은 미연결 | 코드 읽기 기준. `chat-rag-baseline-trace`로 실행 확인 |
| 검색 baseline | `RET-L-OFFLINE` 인메모리 Lexical RRF + `korean-lexical-concept-rerank@1` | #14 벤치마크 실행 기록 있음 (합성 질의 60건 / 코퍼스 100건) |
| PostgreSQL Lexical·pgvector 검색 | [knowledge-evidence-search-rrf-v1](../../contracts/proposed/post-mvp-1/knowledge-evidence-search-rrf-v1.md) 등 proposed 구현 | 구현 브랜치에서 검증 중으로 기록됨 |
| 청크·임베딩 적재 경로 | 식약처 허가사항 청크 정책 `mfds-label-knowledge-chunk@1`, OpenAI 임베딩 → pgvector 인덱스 구축 관리자 실행기(`apps/ai_worker/admin/knowledge_evidence_index.py`) 존재 | 코드 읽기 기준. 실행 기록 없음 |
| 실제 Vector DB 데이터 | 로컬 PostgreSQL(pgvector 0.8.6)의 `knowledge_document`, `knowledge_chunk`, `rag_knowledge_index_member`, `rag_source` 모두 0건. 이 DB는 예전 팀 프로젝트 폴더에서 띄운 것이라 migration이 `180a1b2c3d4e`로, 저장소 head `925a1b2c3d4e`보다 뒤처져 있음. feature 브랜치용 volume 2개는 비어 있음 | 2026-11-25 직접 조회 |
| Claim·Citation Validator | `apps/ai_worker/tasks/rag/claim_citation_validator.py` 존재 (Guide 경로) | Chat 미연결 |

## 4. 목표와 성공 기준

아래 수치는 팀 설계안 v1.50의 폐쇄형 데모 목표를 참고해 Human Owner가 2026-11-25에 확정했다(결정 2). 결과를 본 뒤 기준을 바꾸지 않으며, 미달하면 미달 그대로 보고한다. 지표 정의와 판정 규칙은 [기능 명세](chat-rag-measured-slice-design.md)의 5절에 있다.

| 영역 | 지표 | 기준 |
| --- | --- | --- |
| 검색 | Recall@5 | 최종 구성 ≥ 0.90 (평가용 분할 기준) |
| 검색 | 단계별 P50·P95 지연 | 측정해서 보고. 상한은 baseline 측정 후 결정 |
| 검색 | 단계 제거 허용폭 | Recall@5 차이의 paired bootstrap 95% CI 하한 ≥ −0.03이고 P95 지연이 줄면 그 단계를 제거 |
| 답변 | Claim Support Precision | ≥ 0.95 |
| 답변 | Citation Coverage | ≥ 0.95 |
| 답변 | Critical Unsupported Claim | 0건. Critical Claim 표본이 30건 이상일 때만 판정하고 95% 상한을 함께 보고. 30건 미만이면 "판정 불가" |
| 판정기 | 사람 라벨 대비 Macro F1, Cohen's κ, 오인용 통과율(False-Support Rate), 1건당 지연·비용 | Macro F1 ≥ 0.80, κ ≥ 0.70, FSR ≤ 0.05, Critical 오인용 0건. 모두 탈락하면 런타임 검증기를 연결하지 않고 Chat 인용 표시를 막는다(fail-closed) |
| Safety | 고위험 회귀셋에서 기존 대비 안전 동작 변화 | 악화 0건. `SAFETY_REGRESSION` 93건은 4주차 최종 회귀에 1회만 사용 |
| E2E | 시연 시나리오 | 합성 계정과 승인 데이터로 처음부터 끝까지 재현 가능 |

## 5. 평가 데이터와 재현 명령

| 데이터 | 용도 | 상태 |
| --- | --- | --- |
| 기존 retrieval gold (질문 60, gold 20, hard negative 80) | 회귀용 쉬운 slice | DRAFT (과거 팀 내부 gold 승인 기록 존재, 외부 의료 검토 `NOT_REQUESTED`·HOLDOUT 동결 미완료) |
| 식약처 nedrug 허가사항 XML 코퍼스 (효능효과·용법용량·사용상의 주의사항, 약 200개 제품, 흔한 성분 군집 중심·고정 seed) | 현실적인 난이도의 검색과 근거 | **이용 조건 확인 전**. 원문은 로컬 private 디렉터리에만 두고 저장소에는 목록·URL·hash·청크 수만 기록 |
| 확장 retrieval 평가셋 | 단계별 ablation 변별 | 미작성 |
| Claim 라벨 표본 (목표 150~200 Claim) | 판정기 비교의 정답 | 미작성 |
| 고위험 회귀셋 (`rag-holdout-safety-v1`) | Safety 회귀 | `FROZEN`(`HOLDOUT` 60 + `SAFETY_REGRESSION` 93). 개발 중에는 쓰지 않고 4주차 최종 회귀에 1회만 사용. 개발 중 확인은 별도 합성 safety DEV case로 한다 |

실제 환자정보와 처방전은 쓰지 않는다. 질의는 합성으로 작성하고, 코퍼스는 공개되어 이용이 허락된 의약품 문서만 쓴다.

현재 재현 가능한 명령은 기존 baseline 하나다. 신규 명령은 기능 명세와 구현 task에서 정의한다.

```bash
PYTHONPATH=apps:packages:. .venv/bin/python -m ai_worker.tasks.evaluation.rerank_benchmark --format markdown
```

## 6. 통합 범위

Frontend Chat 화면 → Backend Chat API → 검색 → 답변 생성 → Claim·Citation 검증 → 응답 저장 → 화면의 근거·인용 표시.

화면은 기존 Chat UI에 근거와 인용 표시를 더하는 수준으로 제한한다. Chat 응답에 Claim별 Citation을 싣게 되면 공유 DTO 변경일 가능성이 높다. 그 경우 구현 전에 `CONTRACT_REQUEST`를 거쳐 Human Owner 결정을 받는다.

## 7. 안전 조건

- `SECURITY.md`와 `docs/privacy-safety.md`를 따른다.
- Safety Triage(응급·과량·복용 중단·임신 등 우선 처리)와 fail-closed 동작을 바꾸지 않는다.
- Critical Claim 및 근거 모순(`contradict`) 판정 Claim은 반드시 제외·폐기(fail-closed)한다. 비치명적 일반 안내의 `neutral` 판정 Claim만 "확인 필요" 표시와 함께 보여 줄 수 있으며, 이 Claim도 Citation Coverage와 Support Precision의 분모에 포함한다(결정 2). 근거가 부족한 상태를 안전하다고 표현하지 않는다.
- 복용 중단, 증량, 감량 조언을 추가하지 않는다.
- 외부 API는 OpenAI만 쓰고, 보내는 내용은 합성 질의와 공개 문서로 한정한다(결정 5).
- 결과는 폐쇄형 데모의 기술 검증이다. 임상적 유효성이나 Production 승인을 뜻하지 않는다.

## 8. 리스크

| 항목 | 영향 | 대응 |
| --- | --- | --- |
| 식약처 공개 데이터 이용 조건 | 코퍼스 구축이 막힐 수 있음. 2026-11-25 기준 저장소에 nedrug·공공데이터 Source의 이용허락 기록이 없고 `LICENSE_VERIFIED` 관문도 통과 전 | 1주차 `mfds-source-terms`로 확인. 불명확하면 코퍼스 task를 멈추고 다시 결정(결정 3) |
| Vector 인덱스가 비어 있음 | 문서 수집·청크·임베딩·적재를 먼저 해야 Dense 검색(RET-D, RET-H)을 비교할 수 있음 | 1~2주차에 인덱스 구축을 코퍼스 task에 포함 |
| 로컬 DB 환경이 오래됨 | 현재 migration이 적용되지 않아 인덱스 실행기와 검색 코드가 동작하지 않을 수 있음 | 저장소 기준으로 새 로컬 DB를 띄우고 migration을 head까지 적용 |
| OpenAI 비용 | 코퍼스 크기, 재임베딩, Judge·생성 횟수에 비례 | 실행마다 예상 비용을 먼저 기록. 4주 합계 US$30의 80%에서 멈춤(결정 5) |
| Chat의 실제 RAG 연결 상태 | baseline 정의가 달라짐 | 1주차에 경로 추적 |
| 디스크 여유 약 8.7 GiB (2026-11-25 실측) | NLI·Embedding 모델 다운로드 제약 | base급 모델부터 크기 확인 후 필요 시 디스크 공간 확보 |
| 4주 안에 E2E 화면까지 완성 | 일정 초과 | 화면 범위를 근거 표시로 제한 |
| 사람 라벨링 작업량 | 일정 초과 | Claim 표본 규모를 먼저 고정 |
| 공유 DTO 변경 | 계약 절차로 지연 | 1주차에 CONTRACT_REQUEST 여부 판단 |

## 9. 일정 (4주)

| 주차 | 산출물 |
| --- | --- |
| 1 | 로컬 DB 준비, baseline 경로 확인, Source 이용 조건 확인, 코퍼스 수집, 평가셋 설계, NLI 스모크 측정, CONTRACT_REQUEST 판단 |
| 2 | 청크·임베딩·pgvector 인덱스 구축, 확장 retrieval 평가셋 완성, 검색 ablation 실행과 결과 |
| 3 | 답변 생성과 Claim 라벨 표본, 판정기 비교, 검증기 채택 |
| 4 | E2E 연결과 시연, Safety 회귀, 결과 문서와 포트폴리오 정리 |

## 10. Human Owner 결정 (2026-11-25 확정)

| 결정 | 결과 |
| --- | --- |
| 1. 기능 선정 | 이 기능을 다음 고도화 기능으로 선정하고 4주 일정을 시작한다. [PRD.md](../../../PRD.md) 갱신, [ADR 0004](../../adr/0004-chat-rag-measured-slice-selection.md) 기록 |
| 2. 성공 기준과 허용폭 | 4절 표와 7절 안전 조건에 반영 |
| 3. Source 범위 | nedrug 허가사항 XML(효능효과·용법용량·사용상의 주의사항)만 사용. 약 200개 제품, 흔한 성분 군집 중심, 고정 seed로 목록 재현. 원문은 로컬 private 디렉터리에만 둔다. `mfds-source-terms`에서 이용 조건이 불명확하거나 수집을 허용하지 않으면 코퍼스 task를 `BLOCKED`로 두고 다시 결정하며, 독립 task는 계속 진행한다. e약은요는 v1 범위가 아니며 이후 확장 후보로 둔다 |
| 4. Chat Citation DTO | 하위 호환 선택 필드(citations) 추가만 허용하고 기존 필드는 바꾸지 않는다. 실제 `CONTRACT_REQUEST` 여부는 `chat-rag-baseline-trace` 뒤 1주차 말에 판단하며, 계약 변경 자체는 그때 별도로 결정한다 |
| 5. 외부 API와 비용 | OpenAI만 사용(임베딩·답변 생성·LLM Judge). Judge는 답변 생성 모델과 다른 모델로 하고 정확한 모델명은 `citation-judge-compare`에서 고정한다. 4주 합계 상한 US$30. 실행마다 예상 비용을 먼저 기록하고, 누적 비용이 상한의 80%에 이르면 멈추고 Human Owner에게 묻는다 |

이 결정은 Source 이용 승인(`LICENSE_VERIFIED`)이나 공유 계약 변경을 대신하지 않는다.

## 11. 요구사항 목록과 추적

이 기능의 요구사항을 `CRM-xx` ID로 나눈다(CRM: Chat RAG Measured). "팀 정의서"는 팀 프로젝트 요구사항 정의서 v3의 ID다. 이 기능이 해결하거나 근거를 만드는 항목을 연결했으며, 연결했다고 팀 항목 전체를 완료한다는 뜻은 아니다. task 이름과 담당 lane은 [기능 명세](chat-rag-measured-slice-design.md) 5.3~5.4절을 따른다.

| ID | 요구사항 | 우선순위 | 팀 정의서 | task | 검증 방법 |
| --- | --- | --- | --- | --- | --- |
| CRM-01 | 공개 의약품 문서 기반 평가셋을 DEV·평가용으로 나누고 버전과 hash를 고정한다 | 필수 | ISS-AI-009, NFR-GEN-007 | `mfds-source-terms`, `mfds-eval-corpus`, `retrieval-eval-set-v2` | 평가셋 manifest에 버전·hash·분할 기록. 기존 gold보다 baseline Recall@5가 낮아 변별력이 있는지 확인 |
| CRM-02 | 검색 구성 RET-L/D/H/HR의 품질과 단계별 지연을 같은 평가셋에서 paired 비교한다 | 필수 | EVAL-RAG-001, EVAL-RAG-002 | `retrieval-ablation` | 구성별 Recall@5·MRR@5·nDCG@5·P95 지연과 paired bootstrap 95% CI 보고서 |
| CRM-03 | 측정 결과와 허용폭 규칙으로 최종 검색 구성을 결정하고 이유를 기록한다 | 필수 | NFR-GEN-005 | `retrieval-ablation` | 제거·유지한 단계마다 수치 근거가 있는 결정 기록 |
| CRM-04 | Evidence Gate의 근거 부족 판정 기준(관련성 임계값) 후보를 측정으로 제시한다 | 선택 | ISS-AI-005, REQ-EVD-004, REQ-CHT-012 | `retrieval-ablation` | 임계값별 No-hit 비율과 오답 근거 통과 비율 표 |
| CRM-05 | Claim–근거 쌍을 support·neutral·contradict로 판정하고 판정기 모델·설정 버전을 기록한다 | 필수 | REQ-EVD-008, ISS-AI-006 | `nli-smoke`, `citation-judge-compare` | 세 판정의 대표 fixture가 기대 판정으로 재현되는 단위 테스트 |
| CRM-06 | 로컬 NLI, LLM Judge, 사람 라벨을 비교해 런타임 검증기를 채택한다 | 필수 | EVAL-NLI-001, ISS-AI-006 | `claim-label-set`, `citation-judge-compare` | 사람 라벨 대비 Macro F1, Cohen's κ, 1건당 지연·비용 보고서 |
| CRM-07 | 답변의 Claim Support Precision과 Citation Coverage를 측정한다 | 필수 | EVAL-CIT-001, EVAL-CIT-002, EVAL-GEN-001 | `chat-claim-citation-generation`, `claim-label-set`, `citation-judge-compare` | 사람 라벨 표본 기준 지표와 95% CI |
| CRM-08 | Chat 응답에서 인용이 필요한 Claim마다 근거를 연결해 화면에 출처를 표시한다 | 필수 | REQ-CHT-002, REQ-EVD-007, REQ-EVD-010 | `chat-claim-citation-generation`, `chat-citation-contract`, `chat-citation-api`, `chat-citation-ui`, `chat-citation-e2e-verify` | 계약 테스트와 Frontend→API→Backend 통합 테스트 |
| CRM-09 | Critical Claim과 `contradict` 판정 Claim은 반드시 제외(fail-closed)하고, 검증을 통과하지 못한 비치명적 `neutral` Claim은 확정 정보로 제공하지 않고 제외하거나 "확인 필요"로 표시한다 | 필수 | REQ-EVD-009, REQ-CHT-017, NFR-GEN-008 | `chat-citation-validator-wire`, `chat-citation-api`, `chat-citation-ui` | Critical·contradict 비노출 및 neutral 미확정 표시 테스트 |
| CRM-10 | 검색이나 검증이 실패하거나 시간을 넘기면 불완전한 답변을 완료로 저장하거나 노출하지 않는다 | 필수 | REQ-CHT-018 | `chat-citation-validator-wire`, `chat-citation-api` | 실패·timeout fixture에서 COMPLETED로 저장되지 않는 테스트 |
| CRM-11 | 기존 Safety 동작이 악화되지 않음을 고위험 회귀셋으로 확인한다 | 필수 | EVAL-SAF-001~003, REQ-CHT-013, NFR-GEN-005 | `safety-regression` | 변경 전후 같은 회귀셋 결과 비교, 악화 0건 |
| CRM-12 | 모든 평가 실행에 dataset·corpus·config hash, model·prompt version, commit, 명령을 기록한다 | 필수 | NFR-GEN-007 | 전체 | 결과 파일마다 식별자 필드 존재 확인 |
| CRM-13 | 승인된 Source 문서를 청크·임베딩해 pgvector 인덱스를 구축하고 corpus·embedding·config hash와 receipt를 남긴다 | 필수 | REQ-EVD-003, REQ-EVD-011 | `local-rag-db-setup`, `mfds-eval-corpus` | 인덱스 receipt와 청크 수, 같은 입력으로 다시 만들 때 hash가 같은지 확인 |

목표값 차이: 팀 정의서의 EVAL-CIT-002(Citation Completeness) 목표는 ≥ 90%다. Human Owner는 2026-11-25에 설계안 v1.50 기준인 ≥ 0.95를 이 기능의 목표로 선택했다.

이번 기능에서 다루지 않는 팀 Chat 항목: REQ-CHT-005(상호작용 확인), REQ-CHT-006·020(대화 이력), REQ-CHT-024~031(장벽·행동 단계 상담) 등.
