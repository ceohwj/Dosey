# 기능별 설계와 구현 계획

설계·구현 계획을 주제별로 찾는 인덱스다. 문서의 기존 상태·작성자·검토 이력은 보존하며 현재 Agent 소유권이나 새 승인으로 해석하지 않는다. 현재 동작은 코드·현재 계약·테스트로 확인하고, 작업 배정은 [Agent workflow](../ai/workflow.md)를 따른다. 과거 문서에 적힌 팀원·workflow 명령은 당시 기록이며 현재 운영 지침은 [AGENTS](../../AGENTS.md)가 정본이다.

## 주제

| 경로 | 내용 | 이번 이동 문서 수 |
| --- | --- | --- |
| [account/](account/) | 계정·회원탈퇴 | 1 |
| [ocr/](ocr/) | OCR·구조화·사용자 확인 | 3 |
| [medication-management/](medication-management/) | 복약 관리·지원 | 3 |
| [frontend/](frontend/) | 화면·디자인 시스템 | 1 |
| [provider/](provider/) | Provider·관측성·검증 | 5 |
| [evaluation/](evaluation/) | 평가·Metric·Dataset | 16 |
| [guide-chat/](guide-chat/) | Guide·Chat 생성 | 4 |
| [rag/](rag/) | Source·검색·식별·인덱스 | 19 |

## 이동과 보존 기준

2026-10-23에 기존 후보 64개 중 52개를 기능별로 이동했다. 보호 계약·코드·테스트가 기존 경로를 참조하는 12개는 경로를 유지한다. 링크 경로만 갱신하며 설계 의도·상태·승인·Issue ID·과거 실행 명령·평가 결과는 다시 쓰지 않는다. 설계와 구현 계획은 서로 다른 기록이므로 한 문서로 합치지 않았다.

아래 이전 경로는 GitHub Issue/PR 또는 승인 시점 artifact에서 과거 문서를 찾기 위한 대응표다. 외부 기록과 immutable evidence는 수정하지 않았다. 과거 명령 속 경로를 현재 checkout에서 사용할 때는 이 표의 현재 경로를 적용한다.

## 이전 경로 → 현재 경로

| 이전 경로 | 현재 경로 | 처리·근거 |
| --- | --- | --- |
| `docs/designs/ceohwj/MFDS_RAG_Phase2A_Design.md` | [docs/designs/rag/MFDS_RAG_Phase2A_Design.md](rag/MFDS_RAG_Phase2A_Design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/MFDS_RAG_Phase2A_Plan.md` | [docs/designs/rag/MFDS_RAG_Phase2A_Plan.md](rag/MFDS_RAG_Phase2A_Plan.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/ai-one-cycle-release-validation-design.md` | [docs/designs/provider/ai-one-cycle-release-validation-design.md](provider/ai-one-cycle-release-validation-design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/ai-one-cycle-release-validation-plan.md` | [docs/designs/ceohwj/ai-one-cycle-release-validation-plan.md](ceohwj/ai-one-cycle-release-validation-plan.md) | KEEP: 보호 계약 참조 |
| `docs/designs/ceohwj/chat-recent-context-design.md` | [docs/designs/guide-chat/chat-recent-context-design.md](guide-chat/chat-recent-context-design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-158-rag-retrieval-metric-baseline-design.md` | [docs/designs/evaluation/issue-158-rag-retrieval-metric-baseline-design.md](evaluation/issue-158-rag-retrieval-metric-baseline-design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-158-rag-retrieval-metric-baseline-implementation-plan.md` | [docs/designs/evaluation/issue-158-rag-retrieval-metric-baseline-implementation-plan.md](evaluation/issue-158-rag-retrieval-metric-baseline-implementation-plan.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-160-grounding-metric-kernel-design.md` | [docs/designs/evaluation/issue-160-grounding-metric-kernel-design.md](evaluation/issue-160-grounding-metric-kernel-design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-167-rag-candidate-index-design.md` | [docs/designs/rag/issue-167-rag-candidate-index-design.md](rag/issue-167-rag-candidate-index-design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-167-rag-candidate-index-implementation-plan.md` | [docs/designs/rag/issue-167-rag-candidate-index-implementation-plan.md](rag/issue-167-rag-candidate-index-implementation-plan.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-170-medication-resolver-design.md` | [docs/designs/rag/issue-170-medication-resolver-design.md](rag/issue-170-medication-resolver-design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-170-medication-resolver-implementation-plan.md` | [docs/designs/rag/issue-170-medication-resolver-implementation-plan.md](rag/issue-170-medication-resolver-implementation-plan.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-173-medication-identification-preflight-design.md` | [docs/designs/ceohwj/issue-173-medication-identification-preflight-design.md](ceohwj/issue-173-medication-identification-preflight-design.md) | KEEP: 코드 경로 참조 |
| `docs/designs/ceohwj/issue-173-medication-identification-preflight-implementation-plan.md` | [docs/designs/rag/issue-173-medication-identification-preflight-implementation-plan.md](rag/issue-173-medication-identification-preflight-implementation-plan.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-175-runtime-bundle-build-design.md` | [docs/designs/ceohwj/issue-175-runtime-bundle-build-design.md](ceohwj/issue-175-runtime-bundle-build-design.md) | KEEP: 코드·승인 근거 참조 |
| `docs/designs/ceohwj/issue-178-followup-bootstrap-authority-split.md` | [docs/designs/rag/issue-178-followup-bootstrap-authority-split.md](rag/issue-178-followup-bootstrap-authority-split.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-178-postgresql-search-rrf-design.md` | [docs/designs/rag/issue-178-postgresql-search-rrf-design.md](rag/issue-178-postgresql-search-rrf-design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-178-rag-evidence-retrieval-design.md` | [docs/designs/rag/issue-178-rag-evidence-retrieval-design.md](rag/issue-178-rag-evidence-retrieval-design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-178-rag-evidence-retrieval-implementation-plan.md` | [docs/designs/rag/issue-178-rag-evidence-retrieval-implementation-plan.md](rag/issue-178-rag-evidence-retrieval-implementation-plan.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-178-ret-h-completion-design.md` | [docs/designs/ceohwj/issue-178-ret-h-completion-design.md](ceohwj/issue-178-ret-h-completion-design.md) | KEEP: 보호 계약 참조 |
| `docs/designs/ceohwj/issue-180-rag-runtime-pure-layer-design.md` | [docs/designs/rag/issue-180-rag-runtime-pure-layer-design.md](rag/issue-180-rag-runtime-pure-layer-design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-214-rag-evaluation-dataset-freeze-design.md` | [docs/designs/ceohwj/issue-214-rag-evaluation-dataset-freeze-design.md](ceohwj/issue-214-rag-evaluation-dataset-freeze-design.md) | KEEP: 테스트가 경로로 직접 읽음 |
| `docs/designs/ceohwj/issue-214-rag-evaluation-dataset-freeze-implementation-plan.md` | [docs/designs/ceohwj/issue-214-rag-evaluation-dataset-freeze-implementation-plan.md](ceohwj/issue-214-rag-evaluation-dataset-freeze-implementation-plan.md) | KEEP: 테스트가 경로로 직접 읽음 |
| `docs/designs/ceohwj/issue-216-rag-evaluation-schema-compatibility-design.md` | [docs/designs/evaluation/issue-216-rag-evaluation-schema-compatibility-design.md](evaluation/issue-216-rag-evaluation-schema-compatibility-design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-216-rag-evaluation-schema-compatibility-implementation-plan.md` | [docs/designs/evaluation/issue-216-rag-evaluation-schema-compatibility-implementation-plan.md](evaluation/issue-216-rag-evaluation-schema-compatibility-implementation-plan.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-231-provider-observability-shared-boundary-design.md` | [docs/designs/provider/issue-231-provider-observability-shared-boundary-design.md](provider/issue-231-provider-observability-shared-boundary-design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-231-provider-observability-shared-boundary-implementation-plan.md` | [docs/designs/provider/issue-231-provider-observability-shared-boundary-implementation-plan.md](provider/issue-231-provider-observability-shared-boundary-implementation-plan.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-241-rag-evaluation-review-provenance-1-2-design.md` | [docs/designs/evaluation/issue-241-rag-evaluation-review-provenance-1-2-design.md](evaluation/issue-241-rag-evaluation-review-provenance-1-2-design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-241-rag-evaluation-review-provenance-1-2-implementation-plan.md` | [docs/designs/evaluation/issue-241-rag-evaluation-review-provenance-1-2-implementation-plan.md](evaluation/issue-241-rag-evaluation-review-provenance-1-2-implementation-plan.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-273-holdout-freeze-preparation-design.md` | [docs/designs/evaluation/issue-273-holdout-freeze-preparation-design.md](evaluation/issue-273-holdout-freeze-preparation-design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-273-holdout-freeze-preparation-implementation-plan.md` | [docs/designs/evaluation/issue-273-holdout-freeze-preparation-implementation-plan.md](evaluation/issue-273-holdout-freeze-preparation-implementation-plan.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-273-natural-language-retrieval-evaluation-design.md` | [docs/designs/evaluation/issue-273-natural-language-retrieval-evaluation-design.md](evaluation/issue-273-natural-language-retrieval-evaluation-design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-273-phase-0-provenance-contracts-implementation-plan.md` | [docs/designs/evaluation/issue-273-phase-0-provenance-contracts-implementation-plan.md](evaluation/issue-273-phase-0-provenance-contracts-implementation-plan.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-273-phase-a-dev-authoring-implementation-plan.md` | [docs/designs/evaluation/issue-273-phase-a-dev-authoring-implementation-plan.md](evaluation/issue-273-phase-a-dev-authoring-implementation-plan.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-368-protected-retrieval-runner-security-kernel-design.md` | [docs/designs/ceohwj/issue-368-protected-retrieval-runner-security-kernel-design.md](ceohwj/issue-368-protected-retrieval-runner-security-kernel-design.md) | KEEP: 보호 계약 참조 |
| `docs/designs/ceohwj/issue-513-c2b-dataset-lifecycle-and-freeze-design.md` | [docs/designs/evaluation/issue-513-c2b-dataset-lifecycle-and-freeze-design.md](evaluation/issue-513-c2b-dataset-lifecycle-and-freeze-design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/issue-513-c2b-dataset-lifecycle-and-freeze-implementation-plan.md` | [docs/designs/evaluation/issue-513-c2b-dataset-lifecycle-and-freeze-implementation-plan.md](evaluation/issue-513-c2b-dataset-lifecycle-and-freeze-implementation-plan.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/live-provider-call-evidence-design.md` | [docs/designs/provider/live-provider-call-evidence-design.md](provider/live-provider-call-evidence-design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/medication-chat-ai-backend-integration-design.md` | [docs/designs/ceohwj/medication-chat-ai-backend-integration-design.md](ceohwj/medication-chat-ai-backend-integration-design.md) | KEEP: Current 계약 참조 |
| `docs/designs/ceohwj/medication-chat-ai-generation-design.md` | [docs/designs/guide-chat/medication-chat-ai-generation-design.md](guide-chat/medication-chat-ai-generation-design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/medication-guide-ai-generation-design.md` | [docs/designs/guide-chat/medication-guide-ai-generation-design.md](guide-chat/medication-guide-ai-generation-design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/medication-guide-ai-v3-design.md` | [docs/designs/guide-chat/medication-guide-ai-v3-design.md](guide-chat/medication-guide-ai-v3-design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/provider-observability-policy-design.md` | [docs/designs/provider/provider-observability-policy-design.md](provider/provider-observability-policy-design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/rag-evaluation-runner-reporter-design.md` | [docs/designs/evaluation/rag-evaluation-runner-reporter-design.md](evaluation/rag-evaluation-runner-reporter-design.md) | MOVE: 기능별 이동 |
| `docs/designs/ceohwj/rag-evaluation-runner-reporter-implementation-plan.md` | [docs/designs/evaluation/rag-evaluation-runner-reporter-implementation-plan.md](evaluation/rag-evaluation-runner-reporter-implementation-plan.md) | MOVE: 기능별 이동 |
| `docs/designs/issue-144-optional-review-fields-implementation-plan.md` | [docs/designs/ocr/issue-144-optional-review-fields-implementation-plan.md](ocr/issue-144-optional-review-fields-implementation-plan.md) | MOVE: 기능별 이동 |
| `docs/designs/issue-206-account-withdrawal-retention-matrix.md` | [docs/designs/account/issue-206-account-withdrawal-retention-matrix.md](account/issue-206-account-withdrawal-retention-matrix.md) | MOVE: 기능별 이동 |
| `docs/designs/jye-rookie/issue-166-d04-component-order.md` | [docs/designs/jye-rookie/issue-166-d04-component-order.md](jye-rookie/issue-166-d04-component-order.md) | KEEP: 보호 계약 참조 |
| `docs/designs/jye-rookie/issue-166-d04-loader-handoff.md` | [docs/designs/rag/issue-166-d04-loader-handoff.md](rag/issue-166-d04-loader-handoff.md) | MOVE: 기능별 이동 |
| `docs/designs/jye-rookie/issue-166-d04-mfds-resolution.md` | [docs/designs/rag/issue-166-d04-mfds-resolution.md](rag/issue-166-d04-mfds-resolution.md) | MOVE: 기능별 이동 |
| `docs/designs/jye-rookie/issue-166-d05-readiness.md` | [docs/designs/rag/issue-166-d05-readiness.md](rag/issue-166-d05-readiness.md) | MOVE: 기능별 이동 |
| `docs/designs/jye-rookie/issue-166-db-handoff-review.md` | [docs/designs/jye-rookie/issue-166-db-handoff-review.md](jye-rookie/issue-166-db-handoff-review.md) | KEEP: 보호 계약 참조 |
| `docs/designs/jye-rookie/issue-166-db-integration-plan.md` | [docs/designs/jye-rookie/issue-166-db-integration-plan.md](jye-rookie/issue-166-db-integration-plan.md) | KEEP: 보호 계약 참조 |
| `docs/designs/jye-rookie/issue-166-db-migration-readiness.md` | [docs/designs/rag/issue-166-db-migration-readiness.md](rag/issue-166-db-migration-readiness.md) | MOVE: 기능별 이동 |
| `docs/designs/jye-rookie/issue-166-mfds-acquisition-readiness.md` | [docs/designs/rag/issue-166-mfds-acquisition-readiness.md](rag/issue-166-mfds-acquisition-readiness.md) | MOVE: 기능별 이동 |
| `docs/designs/jye-rookie/issue-192-track-c-storage-implementation-plan.md` | [docs/designs/medication-management/issue-192-track-c-storage-implementation-plan.md](medication-management/issue-192-track-c-storage-implementation-plan.md) | MOVE: 기능별 이동 |
| `docs/designs/jye-rookie/issue-347-source-artifact-cleanup-plan.md` | [docs/designs/rag/issue-347-source-artifact-cleanup-plan.md](rag/issue-347-source-artifact-cleanup-plan.md) | MOVE: 기능별 이동 |
| `docs/designs/jye-rookie/issue-591-source-handoff-alignment.md` | [docs/designs/rag/issue-591-source-handoff-alignment.md](rag/issue-591-source-handoff-alignment.md) | MOVE: 기능별 이동 |
| `docs/designs/medication-ux-design-system-prototype.md` | [docs/designs/frontend/medication-ux-design-system-prototype.md](frontend/medication-ux-design-system-prototype.md) | MOVE: 기능별 이동 |
| `docs/designs/ocr-consent-gate-458-implementation.md` | [docs/designs/ocr-consent-gate-458-implementation.md](ocr-consent-gate-458-implementation.md) | KEEP: 보호 계약 참조 |
| `docs/designs/ocr-llm-consent-458.md` | [docs/designs/ocr/ocr-llm-consent-458.md](ocr/ocr-llm-consent-458.md) | MOVE: 기능별 이동 |
| `docs/designs/ocr-llm-worker-453.md` | [docs/designs/ocr/ocr-llm-worker-453.md](ocr/ocr-llm-worker-453.md) | MOVE: 기능별 이동 |
| `docs/designs/track-b/issue-670-recommendation-scope.md` | [docs/designs/medication-management/issue-670-recommendation-scope.md](medication-management/issue-670-recommendation-scope.md) | MOVE: 기능별 이동 |
| `docs/designs/track-c-rule-based-support.md` | [docs/designs/medication-management/track-c-rule-based-support.md](medication-management/track-c-rule-based-support.md) | MOVE: 기능별 이동 |
