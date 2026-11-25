# Dosey 메인 워킹트리 변경분 주제별(A~F) 점검 및 파일 소유권 보고서 — 2026-11-12

- **기준 브랜치 / HEAD:** `develop` (`bcac7835420118e13ee235410c6b9fdd21224b14`)
- **원격 / 관련 브랜치 상태:**
  - `origin/develop` (`cf509d7`): 로컬 `develop` 대비 2커밋 선행 (`da4de67` 채용 개요 정렬, `cf509d7` 에이전트 작업 진행 복원 및 검토 후 수정 절차 추가 `#2`)
  - `codex/3-readable-work-history` (`7e8df7b`, 워크트리 `/Users/junghyunwoo/.codex/worktrees/dosey-commit-history/Dosey`): `origin/develop` 대비 2커밋 선행 (`344c368`, `7e8df7b` 작업별 커밋 기록 및 GitHub 작성 기준 추가 `#3`)
- **보존 원칙:** 커밋·브랜치 전환·`git reset`/`clean` 없이 기존 워킹트리 변경분을 100% 보존한 상태에서 6개 주제(Topic A~F)의 파일 소유권, 공유 파일 접점, 검증 결과 및 남은 외부 조건을 정리했다.

---

## 1. 6개 주제(Topic A~F) 분류 및 파일 소유권 매핑

### Topic A — Source Ingestion / Publisher B1 / Finalizer Owner Protocol (Track F Source Boundary)

- **A-1 (기존 워킹트리): Source Writer / Trusted Publisher B1 권한 분리 및 Capture Observation / Run Artifact Close**
  - **주요 코드:**
    - `ai_worker/admin/{source_publisher,source_writer,source_artifact_cleanup}.py`
    - `ai_worker/adapters/sqlalchemy_source_snapshot_repository.py`
    - `ai_worker/tasks/rag/source_ingestion/{failure_runs,mfds_detail,mfds_label,persistence,snapshot_lifecycle}.py`
    - `ai_worker/tasks/evaluation/ret_h_bootstrap_stages.py`
    - `backend/app/admin/source_management_api.py`
    - `backend/app/models/rag_source_capture_observation.py`
    - `backend/app/repositories/rag_source_capture_observation_repository.py`
    - `backend/app/services/source_capture_admission.py`
    - `rag_runtime/source_capture_observation.py`
    - `infra/docker/{docker-compose.prod.yml,docker-compose.source-publisher-test.yml}`
    - `infra/docker/postgres/{configure-app-role.sql,configure-management-role.sql}`
    - `infra/python/{provision_database_roles,source_role_policy,source_capture_role_policy}.py`
    - `scripts/deployment.sh`, `scripts/ci/{source_publisher_container_check,source_publisher_docker_check}.py`
  - **문서 및 계약:**
    - `docs/governance/decisions/{2026-11-04-source-run-artifact-close,2026-11-05-source-writer-run-privileges,2026-11-05-source-trusted-publisher-b1,2026-11-05-source-artifact-owner-read-direction}.md`
    - `docs/testing/{source-run-artifact-close-20261104,source-writer-role-boundary-20261105,source-publisher-b1-20261105,source-publisher-docker-readiness-20261105}.md`
  - **테스트:**
    - `ai_worker/tests/rag/source_ingestion/test_{source_publisher,source_writer,failure_runs,mfds_detail,mfds_label,snapshot_lifecycle,sqlalchemy_snapshot_repository}.py`
    - `ai_worker/tests/evaluation/test_ret_h_stage1_b1_boundary.py`
    - `backend/app/tests_unit/rag/test_source_capture_admission.py`
    - `tests/contract/rag/test_source_capture_observation.py`, `tests/contract/test_{database_role_deployment,production_deployment_script,source_management_deployment}.py`
    - `tests/integration/rag/test_{source_capture_complete_attempt,source_capture_observation_store,source_complete_attempt,source_publication_concurrency,source_publisher_roles,source_writer_roles,source_management,source_snapshot_lifecycle,database_role_provisioning}.py`
    - `tests/services/test_{source_publisher_container_check,source_publisher_docker_check}.py`

- **A-2 (이번 세션 2단계 완료): Source Artifact Owner Protocol v1 (`source-artifact-owner@1`, S1~S5)**
  - **주요 코드:**
    - `ai_worker/adapters/source_artifact_owner_protocol.py` (신규)
    - `ai_worker/adapters/local_private_source_artifact_finalizer.py`
    - `ai_worker/admin/{source_artifact_finalizer,mfds_product_rematerialization,mfds_product_ingestion,mfds_label_writer,catalog_writer,catalog_approval,knowledge_materialization,knowledge_evidence_index}.py`
    - `ai_worker/tasks/rag/catalog/mfds_product_source.py`
  - **문서 및 계약:**
    - `docs/governance/decisions/2026-11-12-source-artifact-owner-protocol-v1.md` (`PD-DOSEY-20261112-SOURCE-ARTIFACT-OWNER-V1-1`)
    - `docs/contracts/proposed/post-mvp-1/source-artifact-finalizer-owner-boundary-v1.md`
    - `docs/contracts/targets/post-mvp-1/rag-source-ingestion-v1.md`
    - `docs/testing/source-artifact-owner-v1-20261112.md`
  - **테스트:**
    - `ai_worker/tests/rag/source_ingestion/test_source_artifact_owner_protocol.py`
    - `tests/contract/rag/test_{source_artifact_owner_contract_docs,source_artifact_owner_boundary}.py`
    - `ai_worker/tests/rag/source_ingestion/test_{mfds_product_rematerialization,mfds_label_writer,mfds_product,source_artifact_finalizer_command}.py`
    - `ai_worker/tests/rag/catalog/test_{mfds_product_source,catalog_writer_execution,writer}.py`

---

### Topic B — Medication Candidate Search / Runtime Bundle Binding & Guard Audit (`924a1b2c3d4e`, `925a1b2c3d4e`)

- **주요 코드 및 마이그레이션:**
  - `backend/alembic/versions/924a1b2c3d4e_candidate_search_runtime_binding.py`
  - `backend/alembic/versions/925a1b2c3d4e_candidate_guard_audit_capture.py` (revises `924a1b2c3d4e`)
  - `backend/app/models/{rag_candidate,rag_candidate_guard_audit}.py`
  - `backend/app/repositories/{medication_candidate_repository,rag_candidate_guard_audit_repository}.py`
  - `backend/app/services/{candidate_guard_audit,candidate_index_binding,candidate_runtime_binding,candidate_search,candidate_search_resolution,candidate_source_guard,medication_candidates,medication_identification}.py`
  - `backend/app/apis/v1/medication_candidate_routers.py`
  - `rag_runtime/candidate_guard_audit.py`
  - `scripts/{candidate_index_builder,audit_candidate_runtime}.py`
  - `evals/candidate/`
- **문서 및 계약:**
  - `docs/contracts/targets/post-mvp-1/{candidate-request-guard-audit-v1.md,candidate-request-guard-audit-v1.schema.json,candidate-request-guard-audit-v1.hash-vector.json,medication-identification-v1.md}`
  - `docs/governance/decisions/{2026-10-23-candidate-runtime-bundle-binding,2026-10-23-candidate-search-synthetic-connection,2026-11-04-candidate-exact-alias-validation,2026-11-04-candidate-guard-audit-internal}.md`
  - `docs/testing/{candidate-search-synthetic-20261023,candidate-policy-evidence-20261104}.md`
- **테스트:**
  - `backend/app/tests_unit/rag/test_candidate_{guard_audit_coordinator,index_binding,search_coordinator,search_resolution,source_guard}.py`
  - `backend/app/tests/commands/test_candidate_index_builder.py`
  - `backend/app/tests/medication_candidates/test_medication_candidate_api.py`
  - `backend/app/tests/services/test_medication_candidates.py`
  - `tests/contract/rag/test_candidate_{guard_audit,runtime_binding,runtime_idempotency,synthetic_eval}.py`, `tests/contract/rag/test_medication_candidate_openapi.py`
  - `tests/integration/rag/test_candidate_{guard_audit_store,guard_audit_transaction,search_runtime_binding,search_synthetic_flow}.py`
  - `tests/migration/test_candidate_{search_runtime_binding_migration,guard_audit_migration}.py`
  - `tests/services/test_candidate_runtime_inventory.py`

---

### Topic C — Guide Sync CR-A Runtime Authority & Lifecycle (`guide-sync-request-authority-v1`)

- **주요 코드:**
  - `backend/app/services/{guide_request_authority,guide_sync_runtime_execution,guide_sync_runtime_finalizer,guide_sync_runtime_lifecycle,guides,idempotency}.py`
  - `scripts/benchmark_guide_sync_cra.py`
- **문서 및 계약:**
  - `docs/contracts/targets/post-mvp-1/{guide-sync-request-authority-v1,rag-runtime-v1,idempotency-v1}.md`
  - `docs/contracts/proposed/post-mvp-1/guide-runtime-authority-bootstrap-v1.md`
  - `docs/governance/decisions/2026-11-04-guide-sync-request-authority.md`
  - `docs/testing/{guide-sync-cra-20261104,guide-runtime-bootstrap-20261104}.md`
- **테스트:**
  - `backend/app/tests_unit/rag/test_guide_{request_authority,runtime_bootstrap,sync_runtime_finalizer,sync_transactions}.py`
  - `backend/app/tests/core/test_guide_runtime_provider_wiring.py`
  - `backend/app/tests/services/test_guide_sync_runtime_api.py`
  - `tests/fixtures/guide_sync_cra.py`, `tests/integration/rag/test_guide_sync_cra.py`

---

### Topic D — Frontend 40~60대 모바일 UX·처방/가이드/일정/챗 UI 개선 및 외부 리뷰 수리

- **주요 코드:**
  - `frontend/src/pages/{HomeTodaySection.tsx,HomeTodaySection.css,HomePage.tsx,CheckinPage.tsx,CheckinPage.css,SchedulePage.tsx,SchedulePage.css,MedicationRecordPage.tsx,MedicationRecordPage.css,PrescriptionUploadPage.tsx,PrescriptionReviewPage.tsx,PrescriptionReviewPage.css,GuidePage.tsx,GuidePage.css,ChatPage.tsx,ChatPage.css,MvpPages.css}`
- **테스트 및 검증 문서:**
  - `frontend/tests/{HomeTodaySection,AppRouter,CheckinPage,GuidePage,MedicationRecordPage,MvpDesignPages,PrescriptionReviewPage,PrescriptionUploadPage,SchedulePage}.test.tsx`
  - `frontend/e2e/{guide-chat-flow,prescription-guide-flow,today-schedule-records,auth-and-account,chat-rediscovery-initial-visual,checkin-current-source,home-visual-and-mascot,medication-record,notification-closeout}.spec.ts`
  - `docs/testing/{chat-design-20261105,external-ui-review-20261105}.md`

---

### Topic E — 설계 문서 도메인별 재구성(`docs/designs/`) 및 최상위 문서 정렬

- **주요 변경:**
  - 과거 작성자별 폴더(`docs/designs/ceohwj/*`, `docs/designs/jye-rookie/*`)에서 도메인별 폴더(`docs/designs/{account,evaluation,frontend,guide-chat,medication-management,ocr,provider,rag}/` 및 `docs/designs/README.md`)로 재구성
  - `PRD.md`, `docs/technology-stack.md`, `docs/governance/link-repair-record-2026-10-23.md` 추가
  - 참조 경로 정렬: `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `docs/README.md`, `docs/architecture.md`, `docs/ai-pipeline.md`, `docs/privacy-safety.md`, `docs/testing.md`, `docs/adr/*`, `docs/governance/*`, `docs/release-gates/*`, `docs/testing/*`, `.github/ISSUE_TEMPLATE/*`, `.github/PULL_REQUEST_TEMPLATE.md`

---

### Topic F — 에이전트 연속 운영(`dosey-agent-continuity`), Claude 선택 검토 브리지 및 Superpowers 산출물

- **주요 코드 및 문서 (이번 세션 3단계 통합 완료):**
  - `AGENTS.md`, `CLAUDE.md`, `.codex/`
  - `docs/ai/{workflow,task-template,model-routing,knowledge-index}.md`
  - `docs/failures/{README,20261111-hygiene-file-uri,20261111-test-snapshot-inventory}.md`
  - `docs/testing/agent-workflow-20261111.md`
  - `scripts/{agent_workflow,claude_review}.py`, `scripts/ci/check_agent_hygiene.py`
  - `.github/workflows/checks.yml`
  - `tests/services/test_{agent_workflow,agent_hygiene,claude_review}.py`
  - 이번 세션 Superpowers 설계·계획·원장: `docs/superpowers/specs/2026-11-12-*.md`, `docs/superpowers/plans/2026-11-12-*.md`, `.superpowers/sdd/2026-11-12-*`

---

## 2. 주제 간 공유 파일(Shared Files) 및 분리 커밋 시 주의점

향후 주제별로 커밋을 분리할 때 아래 **5개 공유 파일**은 여러 주제의 변경 hunk를 함께 포함하므로 순서대로 반영하거나 hunk 단위로 분리해야 한다:

1. `backend/app/models/__init__.py`: Topic A-1(`RagSourceCaptureObservation`) + Topic B(`MedicationCandidateGuardAuditRecord`) 모델 export 공유.
2. `backend/app/dependencies/services.py` & `backend/app/main.py`: Topic B(Candidate Search / Guard Audit DI) + Topic C(Guide Sync Runtime Authority DI) 공유.
3. `backend/alembic/versions/`: Topic B의 `924a1b2c3d4e` → `925a1b2c3d4e` 체인이 순차 연결됨 (`925a`가 `924a`에 의존).
4. `docs/contracts/README.md`: Topic A-2(`source-artifact-finalizer-owner-boundary-v1`, `rag-source-ingestion-v1`), Topic B(`candidate-request-guard-audit-v1`, `medication-identification-v1`), Topic C(`guide-sync-request-authority-v1`, `guide-runtime-authority-bootstrap-v1`, `rag-runtime-v1`, `idempotency-v1`), Topic E(`docs/designs/` 링크 수정)가 모두 반영됨.
5. `origin/develop`(`cf509d7`) 및 `codex/3-readable-work-history`(`7e8df7b`)와의 관계:
   - Topic F의 `scripts/agent_workflow.py`, `scripts/ci/check_agent_hygiene.py`, `tests/services/test_agent_{workflow,hygiene}.py`, `.github/workflows/checks.yml`, `docs/failures/*`는 이미 `origin/develop`(`cf509d7`, PR `#2`)에 머지된 커밋과 동일 계보이므로, `origin/develop` rebase/merge 시 `docs/ai/knowledge-index.md`의 2줄(`PRD.md` vs `README.md`, `model-routing.md` 링크 유무)만 로컬 개인 프로젝트 방향(`PRD.md` 유지 여부)에 맞춰 선택하면 충돌 없이 합쳐진다.

---

## 3. 일괄 검증 결과 (2026-11-12 실행)

| 검증 항목 | 실행 명령 | 결과 | 비고 |
| --- | --- | --- | --- |
| **Python Test Inventory 가드** | `python3 scripts/ci/check_python_test_inventory.py` | **PASS** | `Python test inventory is fully classified.` |
| **DB 함수/Trigger/RLS 금지 가드** | `python3 scripts/ci/check_database_logic.py` | **PASS** | `DB 함수/프로시저/Trigger/RLS 재도입 검사 통과` |
| **보호 테이블 Python 쓰기 경계 가드** | `python3 scripts/ci/check_protected_table_writes.py` | **PASS** | `보호 테이블 Python 쓰기 경계 검사 통과` |
| **에이전트 규범 문서·소스 Hygiene 스캐너** | `python3 scripts/ci/check_agent_hygiene.py --check` | **PASS** | `findings: []`, `mutation: false` (기본 5문서 + 추가 검증/실패 문서 모두 0건) |
| **저장소 전체 Ruff 정적 분석** | `python3 -m ruff check ai_worker backend scripts rag_runtime tests` | **PASS** | `All checks passed!` |
| **저장소 전체 Git 공백·포맷 검사** | `git diff --check` | **PASS** | exit `0` |
| **서비스·도구 단위/통합 테스트 (`tests/services/`)** | `pytest tests/services/test_{agent_workflow,agent_hygiene,claude_review,source_publisher_container_check,source_publisher_docker_check,candidate_runtime_inventory}.py` | **PASS** | **146 passed in 27.07s** |
| **AI Worker Source/Catalog/Owner 프로토콜 및 배포 계약 테스트** | `pytest ai_worker/tests/rag/{source_ingestion,source_cleanup,catalog} tests/contract/...` | **PASS** | **1,198 passed** (macOS Seatbelt의 `/` 기준 `openat('private')` 제약 7건 및 로컬 미설치 `cryptography`/`boto3`/`redis` 수집 제외) |
| **Alembic 단일 Head / `cryptography`·`pgvector` 의존 테스트** | `scripts/ci/verify_database_head.py --heads-only`, `backend/app/tests_unit/rag/*` | **NOT_RUN (환경 제약)** | 로컬 `/opt/homebrew/Caskroom/miniforge/base/bin/python3`에 `pgvector`, `cryptography`, `greenlet` 미설치 (호스트 여유 공간 확보 후 `uv` 가상환경 또는 CI 컨테이너에서 실행 필요) |
| **1단계 컨테이너 Probe (`source_publisher_container_check.py`)** | `python3 scripts/ci/source_publisher_container_check.py` | **PASS (2026-11-18 재검증 완료)** | 호스트 여유 공간 8.81 GiB(`>= 8 GiB` 가드 충족) 및 Docker 28.4.0에서 실제 Worker 이미지 빌드, UID 10001:10001 import/credential 혼입 거부/RO·RW 마운트/helper UID 501 불변성/child `env={}`/`mfds_product_ingestion` CLI 음성 검사 및 container·image·volume 정리 전체 통과 (`exit 0`) |
