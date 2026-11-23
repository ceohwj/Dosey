# Codex 중심 개발과 외부 검토

## 정본과 권한

Human Owner가 제품·계약·migration·안전·merge·배포·공개를 최종 결정한다. 메인 Codex는 Integrator로 작업 분해·파일 배정·통합·검증을 맡는다. 기술 안전·외부 승인 조건은 [AGENTS](../../AGENTS.md), [기여 기준](../../CONTRIBUTING.md), [보안](../../SECURITY.md), [Privacy](../privacy-safety.md), [계약 인덱스](../contracts/README.md)를 따른다. 이 문서는 작업 실행 절차이며 해당 정책의 복사본이 아니다.

## 내부 Agent 배정

| Agent | 주된 작업·읽을 근거 | 대표 검증 |
|---|---|---|
| `dosey_frontend` | 화면·API 표시·접근성, 해당 UX와 현재 DTO. DESIGN.md는 현재 Report/clinic 범위 | 관련 frontend/tests, type/build/lint, 필요한 합성 E2E |
| `dosey_backend` | API·Service/Repository·소유권·transaction·Job 연결, api/data-schema와 current 계약 | 변경 영역 unit·contract·integration |
| `dosey_llm` | Guide/Chat 생성·prompt·payload·출력 검증, ai-pipeline와 생성 계약 | 구조화 schema, baseline 대비 생성·의료 안전 회귀 |
| `dosey_ocr` | 인식·정규화·구조화, OCR 계약과 사용자 확인 경계 | 합성 OCR fixture·Worker/Backend 경계 회귀 |
| `dosey_rag` | Source·retrieval·evidence/citation·평가, 상태별 RAG 계약 | retrieval와 generation을 분리한 eval·provenance·안전 회귀 |

경로는 영구 소유권이 아니다. LLM 코드는 Backend 안에 있고 OCR/RAG는 Backend·Worker·공유 runtime에 걸쳐 있다. Integrator는 [작업 템플릿](task-template.md)으로 실제 파일을 배정한다. 기존 `dosey_executor`는 혼합·일반 구현, `dosey_explore`는 조사, `dosey_writer`는 확정 사실 문서화, `dosey_reviewer`는 내부 독립 검토, `dosey_integration_verifier`는 요청→응답 계층 추적에 사용한다. 새 전용 orchestrator나 서비스 LLM agent framework는 추가하지 않는다.

2026-10-23 역할 검토에서는 기존 10개로 현재 작업 범위를 다룰 수 있어 상시 역할을 추가하지 않았다. LLM 생성 평가는 `dosey_llm`, 검색·인용 평가는 `dosey_rag`, 독립 검증은 reviewer/verifier가 맡는다. CI·인프라·DB의 작은 작업은 정확한 허용 파일을 배정한 executor로 처리한다. 독립 평가 작업이나 특정 전문 업무가 반복되어 기존 역할로 검증하기 어려운 증거가 생기면 전용 역할을 다시 검토한다.

## 병렬 실행 순서

1. 작업 ID·기준 commit·현재 변경 상태·의존성·공유 계약을 기록한다.
2. API/DTO·DB schema·상태/오류·공유 evidence 변경은 CONTRACT_REQUEST와 Human Owner 결정을 먼저 거친다.
3. 각 작업에 정확한 ALLOWED_FILES를 배정한다. 두 작업의 파일 집합이 겹치면 파일 담당을 하나로 정하거나 순차 수행한다. 다른 작업 결과를 전제로 하면 DEPENDENCY_WAIT로 대기한다.
4. 독립 준비 작업은 host의 실제 동시 실행 한도 안에서 병렬 수행한다. 다섯 역할이 있다고 다섯 작업이 항상 동시에 실행되는 것은 아니다. 같은 checkout에서 branch 전환·reset·clean·merge·commit/index 작업은 병렬 수행하지 않는다.
5. 구현 Agent가 변경·테스트·미해결 사항을 반환하면 Integrator가 통합한다. 최종 검토 동안 writer를 멈추고 commit 또는 working snapshot을 고정한다.
6. 중요한 변경은 외부 Claude 검토를 요청한다. 단순 문서·사소한 수정은 필요한 내부 검사만 수행할 수 있다. 리뷰의 지적은 원래 구현 작업으로 돌리고, 수정 후 테스트와 필요한 재검토를 수행한다.
7. 통합 검증과 근거를 Human Owner에게 보고한다. Agent/Claude의 결과는 merge·외부 승인·공개 권한이 아니다.

자식 Agent가 다시 팀을 만들거나 외부 Claude/Antigravity를 호출하지 않는다. 메인 Integrator가 전체 작업과 외부 검토의 유일한 조정자다. 개별 역할 파일의 지침은 실제 sandbox를 대신하지 않으며 부모 세션 권한·승인·조직 정책을 따른다. 역할이 등록돼도 이 대화의 이미 로드된 도구 목록에 즉시 추가된다고 가정하지 않는다. 새 Codex 작업/세션에서 이름이 노출되는지 확인하고, 미노출이면 일반 executor에 같은 작업 계약을 전달하면서 등록 문제를 보고한다.

## 작업 연속성: 시작과 종료

새 opt-in 개발 작업에만 적용한다. 기존 Issue·task 명세의 개념은 [작업 템플릿](task-template.md)을 유지하며 과거 작업을 이관하거나 다른 채팅의 미완료 작업을 자동 재개하지 않는다. 개발 기록은 제품·계약·공개 승인이나 전역 정책의 새 정본이 아니다.

### 시작과 writer 배정

1. AGENTS와 관련 정책을 읽고 Git revision·기존 변경을 확인한다. [지식 인덱스](knowledge-index.md)에서 해당 계약 상태·Decision/ADR·검증 근거를 찾는다. 현재 코드·DTO·테스트와 목표 문서의 차이를 기록한다.
2. task ID를 모르면 전용 저장소의 canonical `task.json`을 발견·조회한다. checkpoint 디렉터리만 검색하지 않는다. 여러 후보는 목표·허용 파일·의존성으로 현재 배정과 대조하며 발견만으로 실행 권한을 승계하지 않는다.
3. **writer dispatch 또는 canonical task/source 수정 전에** worktree의 단일 main writer claim을 획득한다. `.omx/plans/dosey-agent-tasks/.writer-claim.json`의 create-exclusive claim은 서로 다른 task ID 사이에도 한 holder만 허용한다. 기존 claim이면 새 writer를 배정하지 않고 충돌을 보고한다. claim은 협력 세션의 admission이며 OS 격리나 권한 부여가 아니다. 실제 sandbox와 파일 배정은 별도로 따른다.
4. claim holder인 main은 자식 작업의 정확한 파일 소유권을 배정한다. 자식은 결과를 반환하며 다른 main의 claim을 인수하지 않는다. checkpoint CAS는 metadata만 보호하므로 writer admission을 대신하지 않는다.
5. canonical의 목표·범위·의존성·`next_action`·budget·`operation_in_flight`와 현재 파일을 대조한다. 사용량이 `null`이거나 in-flight이거나 상한에 도달했으면 아래 복구 경계를 확인하고 새 admission/dispatch를 멈춘다. counter 초기화나 새 task ID로 차단을 우회하지 않는다. 신규 작업 ID는 canonical 생성 전에 claim할 수 있다.
6. 구현·검증 dispatch 전에 cycle 소비를 반영하고 `operation_in_flight=true`를 canonical에 저장한다. 결과 후 실제 활동 시간·결과·gap·다음 행동을 반영하고 in-flight를 해제한다. 증거가 없으면 UNKNOWN으로 남기고 후속 dispatch를 멈춘다.

### 정본·checkpoint·복구

- `.omx/plans/dosey-agent-tasks/<task-id>/task.json`은 해당 작업의 명세와 현재 진행 정본이다. `phase`, `completed`, `known_gaps`, `next_action`, `budget`은 항상 여기에 둔다. 필드는 [JSON 형식](task-template.md#canonical-task-json)을 따른다.
- 같은 디렉터리의 `checkpoint.json`은 재생성 가능한 snapshot cache다. canonical digest/revision, Git HEAD, 선택한 allowed/contract/knowledge 경로 hash와 missing-file sentinel을 담으며 별도 진행 ledger가 아니다. cache가 없어져도 다음 행동·budget은 canonical에서 복구한다.
- checkpoint는 정확한 claim/task와 canonical revision을 확인한 metadata lock 안에서 CAS로 저장한다. 최초 `expected_revision=0`은 revision 1을 만들고 후속 저장은 1씩 증가한다. 사용한 counter를 조용히 줄일 수 없다. canonical atomic 저장 후 cache를 atomic 저장하므로 중간 실패는 canonical 진행을 보존하고 cache 누락/오래됨을 표시한다.
- restore는 canonical 진행과 knowledge refs를 반환하고 snapshot `MATCHED_SNAPSHOT`, `STALE`, `UNKNOWN`을 실제 테스트 결과와 분리한다. cache 누락·손상은 UNKNOWN, canonical/file/hash/HEAD 불일치는 STALE로 취급한다. snapshot UNKNOWN만으로 budget 차단을 뜻하지는 않는다. 같은 HEAD의 코드·정책 변경도 재검증 대상이다. 어떤 snapshot 판정도 테스트 PASS가 아니다.
- crash 후 holder·작업·실제 변경·증거를 확인한다. 정확한 **기존 claim과 알려진 누적 counter**가 있으면 증거에 따라 counter를 줄이지 않는 checkpoint로 in-flight를 해제할 수 있다. helper는 `null`→확정값 checkpoint와 unknown/in-flight/상한 도달 작업의 새 claim 획득을 지원하지 않는다. 이 경우 기록·증거를 보존하고 별도로 승인된 복구 범위가 마련될 때까지 BLOCKED를 보고하며 dispatch하지 않는다. 유효한 기존 claim이 없어 `phase=blocked`도 저장할 수 없으면 canonical 밖의 작업 보고에 남긴다. canonical 직접 덮어쓰기는 복구 절차가 아니다. TTL이나 PID 부재만으로 claim을 인수·강제 해제하지 않고 정확한 holder token으로만 release한다. `.omx/state`와 `.codex`는 수정하지 않는다.

### 종료와 수리 loop

1. 구현자는 변경 파일·검증 명령/exit/증거·미실행 사유·gap을 반환하고 main은 canonical 진행과 다음 행동을 갱신한다. 예시 budget 3 cycles/1200 active seconds는 작업별 명시 변경할 수 있다. provider token 비용 추정으로 대체하지 않는다.
2. 모든 writer를 멈추고 검토 revision 또는 working snapshot/hash·파일 범위를 고정한다. 독립 reviewer(`dosey_reviewer`) 또는 외부 `scripts/claude_review.py`의 구조화 보고서 JSON을 `scripts/agent_workflow.py prepare-repair --task ID --review-report PATH`에 전달해 `BLOCKING`/`HIGH` 항목과 `re_review_ranges`를 추출한다. `allowed_files` 밖 지적(`scope_violations`)이나 budget/in-flight 차단(`status=blocked`)이 있으면 자동 수리를 멈추고 Integrator/Human Owner에게 에스컬레이션한다. 허용 범위 내 지적은 원래 지정 구현자에게 돌려 재현 테스트(RED) → 최소 수정 → 통과(GREEN)와 수정 줄 범위 반환을 확인한 뒤 `--review-range`로 해당 구간만 재검토한다. 문장은 내용·참조 검사와 소비자 복구 pilot으로 검증하며 문구 grep 테스트를 만들지 않는다.
3. 재dispatch 전에 [실패 기록](../failures/README.md)과 재검토 조건을 확인한다. cycle/active time 상한, unknown 사용량, 미해결 in-flight이면 멈추고 evidence·gap·`next_action`을 보존한다. 재시도를 위해 counter를 초기화하지 않는다.
4. 위 복구 경계 안에서 checkpoint가 가능하면 종료 상태·남은 작업·증거를 canonical에 저장하고 정확한 holder가 claim을 release한다. 검증된 완료는 `phase=complete`, 차단은 `blocked`와 gap으로 기록하며, 저장할 수 없으면 canonical 밖에 보고한다. 결과 STATUS와 JSON phase는 템플릿처럼 구분한다. 완료/검토 기록은 Human Owner의 merge·공개 결정이 아니다.

### 작업 중 커밋과 GitHub 기록

Issue와 PR 및 커밋 설명은 [GitHub 글쓰기 지침](github-writing.md)을 따른다. 메인 에이전트는 승인된 작업 범위 안에서 검증과 필요한 검토를 마친 변경 단위가 완성되면 로컬 커밋을 만든다. 사용자가 작업 중 통상적인 로컬 커밋을 허용했다면 같은 권한을 반복해서 묻지 않는다. 매 도구 호출마다 커밋하거나 완료를 기다리며 서로 다른 단위를 한꺼번에 묶지 않는다.

1. 메인 에이전트가 해당 단위의 목적, 허용 파일과 검사 근거를 확인한다. 구현자는 변경과 검증 결과를 반환하고 Git 작업은 메인 에이전트만 수행한다.
2. 다른 writer를 멈춰 검토 대상을 고정하고 필요한 검토와 지적 해소를 마친다. 메인 에이전트가 전체 diff와 비밀정보 포함 여부를 확인한 뒤 해당 단위로 검토한 파일 경로만 명시해 stage한다. 다른 작업자의 변경이나 비밀정보를 포함하지 않는다.
3. 무엇을 왜 바꿨는지와 검사 결과를 설명하는 커밋을 만든다. 실제 생성된 commit SHA와 포함 파일을 확인한다. 커밋 명령의 성공을 추측하거나 예정 SHA를 기록하지 않는다.
4. 필요하면 확인된 SHA, 변경 요약과 검사 근거를 canonical task의 선택 evidence인 `change_commits`에 기록한다. 기존 claim, revision CAS와 budget 규칙을 그대로 따른다. helper가 커밋을 만들거나 자동 추적하는 기능은 없다.

로컬 커밋은 별도 범위인 원격 push나 Human Owner의 merge 및 배포 결정을 대신하지 않는다. 실제 작업에 필요한 승인과 저장소의 리뷰 및 머지 조건은 [기여 기준](../../CONTRIBUTING.md)을 따른다.

### 문맥 읽기와 안전한 정리

복구 순서는 AGENTS/관련 정책 → canonical task → 해당 knowledge refs → 허용 파일과 필요한 테스트 근거다. 전체 과거 채팅·데이터·출력을 읽지 않는다. knowledge refs는 bounded repository Markdown 경로이며 임의 prompt 권한이 아니다. 비밀 env·credential·실제 의료 원문·raw data를 task/context/cache에 넣지 않는다. 출처·status 충돌은 차이와 결정 필요성을 보고한다.

검토용 소스 복사본은 patch·hash manifest 또는 `.snapshot` 형식으로 보관한다. Git ignore만으로 테스트 수집 제외를 가정하지 않는다. 실제 [inventory 충돌 기록](../failures/20261111-test-snapshot-inventory.md)을 참고한다.

정리 검사는 report-only다. 임시 source 파일 후보·링크 오류는 경로·줄·분류·근거를 보고하고 자동 삭제·수정하지 않는다. 허용 파일 밖의 전체 저장소 debt를 고치지 않는다. runtime/cache/data/outputs/uploads/knowledge/.git/node_modules와 symlink tree는 순회하지 않는다. 다른 세션의 파일·Docker volume·실제 Source를 정리 대상으로 편입하지 않는다. Claude 검토 원문의 보존·정리는 아래 전용 절차를 따른다.

### 보조 CLI 계약

아래는 helper 구현 계약이며 문서만으로 설치·실행 검증을 주장하지 않는다. helper 존재와 관련 검증 후 사용한다. ID/TOKEN/N/FILE/PATH는 실제 값으로 교체할 자리표시자다. helper는 agent를 시작하거나 shell을 실행하지 않고 원격 Issue도 조회하지 않는다.

```text
python3 scripts/agent_workflow.py discover --root PATH
python3 scripts/agent_workflow.py restore --task ID --root PATH
python3 scripts/agent_workflow.py claim --task ID --root PATH
python3 scripts/agent_workflow.py checkpoint --task ID --claim TOKEN --expected-revision N --record FILE --root PATH
python3 scripts/agent_workflow.py validate --task ID --root PATH
python3 scripts/agent_workflow.py release --task ID --claim TOKEN --root PATH
python3 scripts/agent_workflow.py prepare-repair --task ID --review-report FILE --root PATH
```

성공은 JSON stdout/exit 0, invalid/conflict는 JSON stderr/nonzero다. claim은 opaque unique token과 task ID를 반환한다. 읽기는 저장소를 생성하지 않고 쓰기는 전용 task store에 한정한다. 정규 bounded JSON 파일만 허용하고 symlink component·path escape를 거부한다. 제한된 argv 기반 Git HEAD 조회 외 명령 실행은 하지 않는다. 아래는 Integrator가 고정한 JSON 결과 형식이다. `verification`은 snapshot 판정이며 테스트 통과가 아니다. cache가 없다는 이유로 task를 발견 결과에서 숨기지 않는다. restore는 ID가 필요하므로 ID가 없으면 discover를 먼저 사용한다.

| 호출 | 성공 JSON |
| --- | --- |
| discover | `{"tasks":[{"task_id":"ID","goal":"...","phase":"planned","next_action":"...","revision":1}]}` |
| restore | `{"task":canonical_record,"verification":"MATCHED_SNAPSHOT/STALE/UNKNOWN","reconciliation_required":false,"reason":"..."}` |
| claim | `{"task_id":"ID","claim":"opaque_token"}` |
| checkpoint | `{"task_id":"ID","revision":2,"verification":"MATCHED_SNAPSHOT"}` |
| release | `{"task_id":"ID","released":true}` |
| validate | `{"task_id":"ID","valid":true,"verification":"MATCHED_SNAPSHOT/STALE/UNKNOWN","reconciliation_required":false}` |
| prepare-repair | `{"task_id":"ID","status":"repair_required/clean/scope_escalation_required/blocked","repair_findings":[...],"scope_violations":[...],"deferred_findings":[...],"re_review_ranges":["FILE:START:END"],"next_action":"..."}` |

표의 `canonical_record`는 [task JSON](task-template.md#canonical-task-json) 객체 자리표시자이며 slash로 나눈 verification은 셋 중 한 문자열이다. restore의 진행·budget·knowledge refs는 `task` 아래 canonical 값만 사용한다. `reconciliation_required=true`이면 새 admission/dispatch를 멈추고 위 복구 경계를 따른다. 이 값은 CLI가 해당 복구를 지원한다는 뜻이 아니며 자동 reset하지 않는다. 오류 stderr는 `{"error":"message","code":"stable_code"}`이고 traceback을 내보내지 않는다.

Python 공개 API는 `WorkflowError`, `discover_tasks(root)`, `restore_task(root, task_id)`, `claim_writer(root, task_id)`, `checkpoint_task(root, task_id, claim, expected_revision, record)`, `release_writer(root, task_id, claim)`, `prepare_repair(root, task_id, review_report_path)`, `validate_record(record)`다. discover API는 CLI의 tasks 배열에 해당하는 summary list를 반환하고, 나머지 동작 API는 위 CLI와 같은 JSON-serializable 객체를 반환한다. `validate_record`는 성공 시 None, 실패 시 WorkflowError다.

report-only hygiene helper는 `python3 scripts/ci/check_agent_hygiene.py --root PATH`이며 반복 `--document`와 `--check`를 지원한다. 기본 문서는 AGENTS·workflow·task-template·knowledge-index·failures README다. fenced 예제·외부 URL을 제외한 local Markdown link/anchor, 안전한 상대 경로, 제한된 source root의 임시 파일명 후보를 검사한다. 명시적인 역사 참조를 구분하고 역사 문서를 기본 순회하지 않는다. JSON findings는 `path`, `line`, `code`, `message`; exit 0 clean, 1 findings, 2 invocation error다. delete/apply 옵션은 없다.

## Claude Code CLI 검토

`python3 scripts/claude_review.py`는 설치된 `claude`를 호출하는 로컬 연결이다. Codex 내부 reviewer와 구분한다. 사용자의 이번 설정 요청은 프로젝트 코드 검토 연결과 검증을 허용하지만 비밀·환자 데이터 전송, 배포, 외부 게시를 허용하지 않는다.

```bash
# 먼저 전송할 파일 목록·크기·packet hash 확인. Claude 호출 없음.
python3 scripts/claude_review.py --base HEAD --working-tree \
  --file scripts/claude_review.py --dry-run

# 현재 고정 working snapshot 검토. 미추적 파일도 명시 선택하면 포함.
python3 scripts/claude_review.py --base HEAD --working-tree \
  --file scripts/claude_review.py --file tests/services/test_claude_review.py

# 두 commit 사이의 명시 파일 검토 (REF를 실제 revision으로 교체).
python3 scripts/claude_review.py --base BASE_REF --head HEAD_REF \
  --file frontend/src/pages/ReportPage.tsx
```

- 기본 packet은 `--file`로 선택한 파일의 변경 hunk와 앞뒤 6줄만 담으며 전체 최종 본문을 중복 전송하지 않는다. 새 파일은 전체가 변경이므로 해당 diff에 포함된다. AGENTS.md·SECURITY.md·docs/privacy-safety.md는 검토 안전 경계로 유지한다. 필요한 호출부·테스트는 `--file`과 `--context-range FILE:START:END`로 정확한 최종 줄 구간만 추가한다(구간당 최대120줄). 변경 없는 파일은 명시 구간이 필요하며 전체 선택에는 실제 변경이 있어야 한다. 생략한 테스트·구현은 미확인이지 미구현이 아니다. 문맥이 부족한 지적은 필요한 구간을 확인한 뒤 판단한다.
- 같은 파일의 일부를 수리한 재검토는 `--review-range FILE:START:END`를 반복 지정한다. 범위와 교차하는 변경 hunk만 전송한다(문맥 6줄은 유지하므로 범위는 hunk 선택 기준이지 텍스트 절단 경계가 아니다). 이 옵션이 하나라도 있으면 범위를 지정하지 않은 다른 파일의 diff도 보내지 않는다. 필요한 주변 파일은 `--context-range`를 명시한다. 범위 밖 변경은 검토 완료로 표시하지 않으며 coverage와 캐시 key에 선택 범위를 기록한다. 변경 hunk가 없는 구간·잘못된 줄·빈 선택은 오류이고 전체 파일로 자동 확대하지 않는다. 삭제된 파일의 기준 줄은1이다. 일반 삭제 hunk는 최종 위치의 anchor/주변 문맥과 교차하는 범위를 사용한다.

```bash
# 실제 수리한 구간만 재검토. 줄 번호는 현재 파일에 맞춰 지정.
python3 scripts/claude_review.py --base HEAD --working-tree \
  --file frontend/src/pages/ChatPage.tsx \
  --review-range frontend/src/pages/ChatPage.tsx:830:875 --dry-run
```

- 완료 검토는 무시되는 `outputs/claude-reviews/review-index.json`에 packet hash와 run ID로 기록한다. 같은 기준 revision·파일 범위·변경 hunk·추가 구간·정책 내용이면 구조화된 기존 보고서를 검증한 후 `status: reused`, `claude_called: false`로 반환한다. 새 CLI 호출은 하지 않는다. dry-run·실패·stale·미완료는 재사용하지 않는다. 코드나 정책·범위가 달라지면 새 검토 대상이다. 재검토 시 이미 끝난 파일을 다시 포함하지 않고 수정된 파일과 미해결 지적의 필요한 문맥만 선택한다.
- dry-run의 packet_bytes와 metadata의 review_mode/coverage를 확인한다. 실제 검토 범위는 기록된 hunk/구간뿐이며 전체 파일·기능 검증으로 확대 해석하지 않는다. 재사용할 보고서/metadata/index는 보존하고, 기존 지침대로 packet/raw response 원문은 검토 후 정리한다. 누락된 보고서는 재사용하지 못한다.

```bash
# 변경된 Chat 화면 hunk만 전송한다.
python3 scripts/claude_review.py --base HEAD --working-tree \
  --file frontend/src/pages/ChatPage.tsx --file frontend/src/pages/ChatPage.css --dry-run

# 필요성이 확인된 테스트 문맥만 추가(실제 줄 번호로 교체).
python3 scripts/claude_review.py --base HEAD --working-tree \
  --file frontend/src/pages/ChatPage.tsx --file frontend/tests/ChatPage.test.tsx \
  --context-range frontend/tests/ChatPage.test.tsx:1:40 --dry-run
```

- 기본 크기 상한은 파일 128 KiB·전체 packet 256 KiB다. 디렉터리·저장소 전체 전송은 하지 않는다. source/문서 형식 allowlist를 적용하고 Git에서 무시되는 입력, 민감 경로·dataset/export 형식·바이너리·symlink·경로 이탈을 거부한다. 이 검사는 비밀 탐지기가 아니므로 선택한 코드·문서의 민감정보 포함 여부를 확인한다.
- CLI는 격리 임시 디렉터리에서 safe mode·도구 없음·MCP 없음·hooks/skills 없음·세션 저장 없음으로 실행한다. repo에 shell/편집 접근을 주거나 `--fix`, 권한 우회, 자동 게시를 사용하지 않는다. 조직에서 강제하는 설정은 유지된다.
- 로그인은 설치된 Claude Code의 인증을 사용한다. `claude auth status --text`로 상태 확인; 필요하면 Human Owner가 `claude auth login`으로 대화형 로그인한다. 자격증명을 코드에 저장하지 않는다. 자동 검토 요청은 기존 CLI 사용량을 소비하며 모델 기본값은 이 연결에서 바꾸지 않는다. 호스트가 외부 전송을 차단하고 별도 승인을 요구하면 전송 파일 목록과 대상을 Human Owner에게 제시하고 승인 전에는 보내지 않는다. 다른 CLI나 경로로 우회하지 않는다.
- 결과는 Git 무시 여부를 확인한 `outputs/claude-reviews/<run-id>/`의 소유자 전용 디렉터리(0700)와 packet·metadata·response·report 파일(0600)로 기록한다. commit SHA 또는 packet hash와 선택 파일을 기록하므로 검토 대상을 추적할 수 있다. 공개 증거로 올리기 전 별도 검토한다. packet과 raw response는 개발용 임시 원문이므로 검토·수정 완료 후 삭제하고 늦어도 생성 7일 안에 정리한다. 필요한 task 근거에는 비민감 지적·처리 결과·revision/hash·CLI 버전만 남긴다. 이 기한은 개발 검토 자료에만 적용하며 환자 데이터·Source artifact의 기존 보존 정책을 바꾸지 않는다. 자동 삭제 scheduler는 추가하지 않았으므로 Integrator가 해당 run의 정확한 경로만 확인해 정리한다.
- CLI 부재·로그인/권한/네트워크 실패·timeout·잘못된 schema·검토 도중 snapshot 변경은 실패다. 출력 `reviewed`와 exit 0은 보고서 수집 성공만 의미하며 findings나 테스트 통과를 뜻하지 않는다. Claude는 테스트를 실행하지 않는다. 리뷰 범위 밖의 기능·의료 안전·전체 품질을 검증했다고 쓰지 않는다.

로컬 `CLAUDE.md`는 사람이 Claude를 직접 실행할 때 공통 정책을 연결한다. 격리된 검토 연결은 이를 자동 로딩하지 않고 위 세 정책 파일을 packet으로 전달한다. MEMORY나 채팅을 다른 제품과 자동 공유하지 않는다.

2026-10-23에 설치된 Claude Code 2.1.301로 합성 회귀 사례와 Human Owner가 전송을 승인한 설정·연결 코드 packet의 실제 검토를 실행해 구조화된 보고서를 수집했다. 이 실행에서 위 CLI 옵션이 수용됐음을 확인했다. 이후 수정한 코드의 회귀테스트 결과와 외부 검토 snapshot은 별도 증거로 기록하며, 이 기록은 모든 CLI 버전의 호환성을 보장하지 않는다.

## Antigravity 인계

Human Owner가 앱/CLI에서 직접 사용한다. 동일 작업 명세와 base revision을 전달하고, 해당 파일의 다른 writer를 멈춘다. 독립 worktree를 사용하거나 단일 checkout을 순차 이용한다. 완료 후 diff·테스트·남은 문제를 Codex에 전달해 통합 검증한다. Codex가 Antigravity CLI를 자동 실행하거나 소비자 로그인으로 다른 도구를 감싸는 연결을 만들지 않는다.

2026-10-23에 공식 문서와 로컬 도움말을 확인했다. 독립 터미널 Agent인 `agy`는 파일 편집과 `-p` 비대화형 실행을 지원하므로 간단한 함수 정리·중복 제거·테스트 보완에도 사용할 수 있다. 이 적합성 판단은 편집 기능과 검증 절차에 근거하며 실제 Dosey 리팩토링 성공을 측정한 결과는 아니다. 로컬 `/Users/junghyunwoo/.local/bin/agy`는 **1.2.8**이며 `--sandbox`, `--mode plan/accept-edits`, `--output-format`, `--json-schema`, `--print-timeout`을 제공한다. 로그인·모델 실행·코드 전송은 이번 확인에서 수행하지 않았다.

```bash
# 사람이 직접 시작하는 예시. 이 명령만으로 코드 변경을 승인하지 않는다.
cd /Users/junghyunwoo/Projects/Dosey
agy --sandbox --mode=plan
```

시작 후 AGENTS.md와 작업 템플릿을 읽게 하고 허용 파일·보존 동작·검증 명령을 지정한다. `plan`은 계획 모드이며 OS 수준 read-only 보장의 대용이 아니다. 현행 공식 문서의 `auto`는 1.3.5 이상 기능이므로 설치된 1.2.8에는 적용하지 않는다. 비대화형 실행을 추후 도입한다면 명시적인 `--print-timeout`을 지정한다. 설치 도움말의 기본값은 `0s`(무제한)로, 현재 문서의 5분 기본값과 다르다. headless의 exit 0만으로 작업 성공을 판단하지 않고 결과·권한 거절·테스트를 확인한다. `AGENTS.md`를 공유할 수 있어 중복 GEMINI.md 규칙 파일은 만들지 않는다.

## 문서와 기록

제품 목적은 [PRD](../../PRD.md), 구현 구조는 [기존 아키텍처](../architecture.md), 기능별 UX는 [DESIGN](../../DESIGN.md) 및 해당 설계, 작업은 Issue 또는 task별 명세, 결정은 기존 Decision/ADR에 기록한다. 완료한 task를 PRD나 메모에 복사해 진행 상황을 이중 관리하지 않는다. agent 설정은 `.codex/agents/*.toml`이 실행 정본이며 [model-routing](model-routing.md)은 설명과 검증 범위를 기록한다.

## 공식 근거 (2026-10-23 확인)

- [Codex custom agents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [Claude Code CLI](https://code.claude.com/docs/en/cli-reference)
- [Claude Code 로컬 검토](https://code.claude.com/docs/en/code-review-local)
- [Antigravity FAQ](https://www.antigravity.google/docs/faq/)
- [Antigravity headless CLI](https://www.antigravity.google/docs/cli/headless/)
- [Antigravity 실행 모드](https://www.antigravity.google/docs/cli/modes/)
- [Antigravity와 Gemini CLI 구분·공통 지침](https://www.antigravity.google/docs/cli/gcli-migration/)
