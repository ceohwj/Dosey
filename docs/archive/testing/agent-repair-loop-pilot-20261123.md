# 실제 코드 대상 에이전트 수리 루프 파일럿 결과 (2026-11-23)

2026년 11월 23일 실제 프론트엔드 코드·테스트 변경(`chatpage-feedback-flake-pilot`)과 `scripts/agent_workflow.py`의 `prepare-repair` 확장을 대상으로, 메인 Integrator 중심의 연속성·독립 검토·수리 핸드오프 1사이클을 실행했다.

## 실행 요약

- **대상 작업 ID**: `chatpage-feedback-flake-pilot` (`base_revision: 936d902`)
- **허용 파일**: `frontend/src/components/ResponseFeedback.tsx`, `frontend/tests/ResponseFeedback.test.tsx`, `frontend/tests/ChatPage.test.tsx`
- **배경**: PR `#5` CI에서 `ChatPage.test.tsx`의 `"이전 답변에 저장한 feedback은 새 답변 후에도 삭제할 수 있다"` 케이스가 피드백 버튼 클릭 직후 `피드백 보내기` 버튼을 찾지 못하는 간헐 실패가 1회 발생했다. `ResponseFeedback.tsx`의 `useEffect([targetKey])`가 초기 마운트에서도 무조건 `setRating(null)`을 실행하는 구조적 경합이 원인으로 확인됐다.
- **중간 사용자 재설명 / 결과 복사 전달 횟수**: 0회 (이번 파일럿 실행 범위 기준)

## 수명주기 및 독립 검토·수리 루프 기록

1. **선행 환경 검증 (Phase 1)**: `~/.cache/uv` 정리로 여유 공간 38 GiB 확보 후 `uv sync --group app --group worker --frozen`(110 packages) 및 로컬 `.venv/bin/mypy backend/app ai_worker`(973개 파일, 오류 0건) 통과.
2. **작업 입장 및 초기 체크포인트**: `scripts/agent_workflow.py claim` 획득 후 `revision 1` (`phase=running`, `operation_in_flight=true`, `cycles_used=1`) 저장.
3. **구현 및 1차 검증**: `ResponseFeedback.tsx`에 `previousTargetKeyRef` 가드를 추가해 `targetKey`가 실제로 변경될 때만 draft를 초기화하도록 수정하고, `ChatPage.test.tsx`의 제출 버튼 조회를 `findByRole`로 보강, `ResponseFeedback.test.tsx`에 StrictMode 회귀 테스트를 추가했다. (`vitest` 63 passed, `oxlint` 0 errors, `tsc -b` exit 0)
4. **독립 리뷰어 검토 (`revision 2`, `MATCHED_SNAPSHOT`)**:
   - `scripts/claude_review.py --dry-run`으로 3개 파일 패킷(`packet_bytes: 27360`, `sha256: 9931b24d...`) 검증.
   - 독립 read-only 리뷰어가 Phase 2(`prepare-repair`)와 Phase 3(파일럿)을 함께 검토해 **HIGH 2건, MEDIUM 2건, LOW 2건**을 구조화 JSON으로 반환했다:
     - `HIGH`: `prepare_repair`가 `outputs/claude-reviews/<run>/report.json` 경로를 `BLOCKED_PARTS`로 거부하는 결함
     - `HIGH`: 허용 파일 내 지적과 범위 밖 지적(`scope_violations`)이 혼재할 때 `scope_escalation_required` 대신 `repair_required`를 반환하는 결함
     - `MEDIUM`: `_merge_review_ranges`가 파일 실제 줄 수를 초과하는 `end`를 생성해 `claude_review.py --review-range`에서 거부될 수 있는 결함
     - `MEDIUM`: `ResponseFeedback.test.tsx` 신규 테스트가 `React.StrictMode` 이중 마운트·동일 target 재렌더를 검증하지 않던 보강점
     - `LOW`: [workflow.md](../ai/workflow.md)의 `prepare-repair` 반환 키 표 보완 및 `test_read_only_missing_task_never_creates_store`에 `prepare-repair` 추가
5. **1회 수리 패스 (RED → 수정 → GREEN) 및 종료**:
   - 위 6건을 모두 테스트 우선(RED 확인)으로 수정한 뒤 `pytest tests/services/test_agent_workflow.py tests/services/test_agent_hygiene.py tests/services/test_claude_review.py -q` (**117 passed**), `vitest` (**63 passed**), `oxlint` (0 errors), `tsc -b` 통과.
   - `outputs/claude-reviews/pilot-20261123/report.json`을 대상으로 `scripts/agent_workflow.py prepare-repair` 실행 → `status: "clean"`, `next_action: "ready_for_integrator_verification"` 확인.
   - `revision 3` (`phase=complete`, `cycles_used=2/3`, `active_seconds=95/1200`, `MATCHED_SNAPSHOT`) 저장 및 `release` 완료.

## 한계

- 이번 파일럿은 프론트엔드 3개 파일 + 에이전트 보조 CLI 1개 파일 범위의 단일 워크트리 검증이며, 백엔드·DB 마이그레이션·RAG가 동시에 얽히는 다중 병렬 쓰기 세션이나 자동 배포 권한을 검증하지 않는다.
- 최종 PR 머지와 배포 결정은 [AGENTS.md](../../AGENTS.md)에 따라 Human Owner가 수행한다.
