# 개발 에이전트 작업 연속성

이 문서는 새 opt-in 개발 작업의 로컬 기록과 협력적 쓰기 조정 절차다. 권한·정책은 [AGENTS](../../AGENTS.md), [기여 기준](../../CONTRIBUTING.md), [보안](../../SECURITY.md), [개인정보·의료 안전](../privacy-safety.md)을 따른다. 기록·검토는 승인이나 제품 계약을 대신하지 않는다.

메인 에이전트가 범위·파일 소유권·검증 기준을 정하고 실제 환경에서 사용 가능한 native 역할에 독립 작업을 배정한다. 역할 이름·모델·등록 상태를 이 문서에서 가정하지 않는다. 한 파일은 한 writer가 맡고, 검토 전에 모든 writer를 멈춰 대상 snapshot을 고정한다. 환경에 역할이나 도구가 없으면 미실행 사실과 gap을 보고한다.

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
2. 모든 writer를 멈추고 검토 revision 또는 working snapshot/hash·파일 범위를 고정한다. 독립 reviewer의 실제 위반·누락은 파일/줄과 근거를 붙여 원래 지정 구현자에게 돌린다. 재현 가능한 코드 결함은 RED → 수정 → GREEN과 해당 범위 재검토로 확인한다. 문장은 내용·참조 검사와 소비자 복구 pilot으로 검증하며 문구 grep 테스트를 만들지 않는다.
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

정리 검사는 report-only다. 임시 source 파일 후보·링크 오류는 경로·줄·분류·근거를 보고하고 자동 삭제·수정하지 않는다. 허용 파일 밖의 전체 저장소 debt를 고치지 않는다. runtime/cache/data/outputs/uploads/knowledge/.git/node_modules와 symlink tree는 순회하지 않는다. 다른 세션의 파일·Docker volume·실제 Source를 정리 대상으로 편입하지 않는다.

### 보조 CLI 계약

아래는 helper 구현 계약이며 문서만으로 설치·실행 검증을 주장하지 않는다. helper 존재와 관련 검증 후 사용한다. ID/TOKEN/N/FILE/PATH는 실제 값으로 교체할 자리표시자다. helper는 agent를 시작하거나 shell을 실행하지 않고 원격 Issue도 조회하지 않는다.

```text
python3 scripts/agent_workflow.py discover --root PATH
python3 scripts/agent_workflow.py restore --task ID --root PATH
python3 scripts/agent_workflow.py claim --task ID --root PATH
python3 scripts/agent_workflow.py checkpoint --task ID --claim TOKEN --expected-revision N --record FILE --root PATH
python3 scripts/agent_workflow.py validate --task ID --root PATH
python3 scripts/agent_workflow.py release --task ID --claim TOKEN --root PATH
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

표의 `canonical_record`는 [task JSON](task-template.md#canonical-task-json) 객체 자리표시자이며 slash로 나눈 verification은 셋 중 한 문자열이다. restore의 진행·budget·knowledge refs는 `task` 아래 canonical 값만 사용한다. `reconciliation_required=true`이면 새 admission/dispatch를 멈추고 위 복구 경계를 따른다. 이 값은 CLI가 해당 복구를 지원한다는 뜻이 아니며 자동 reset하지 않는다. 오류 stderr는 `{"error":"message","code":"stable_code"}`이고 traceback을 내보내지 않는다.

Python 공개 API는 `WorkflowError`, `discover_tasks(root)`, `restore_task(root, task_id)`, `claim_writer(root, task_id)`, `checkpoint_task(root, task_id, claim, expected_revision, record)`, `release_writer(root, task_id, claim)`, `validate_record(record)`다. discover API는 CLI의 tasks 배열에 해당하는 summary list를 반환하고, 나머지 동작 API는 위 CLI와 같은 JSON-serializable 객체를 반환한다. `validate_record`는 성공 시 None, 실패 시 WorkflowError다.

report-only hygiene helper는 `python3 scripts/ci/check_agent_hygiene.py --root PATH`이며 반복 `--document`와 `--check`를 지원한다. 기본 문서는 AGENTS·workflow·task-template·knowledge-index·failures README다. fenced 예제·외부 URL을 제외한 local Markdown link/anchor, 안전한 상대 경로, 제한된 source root의 임시 파일명 후보를 검사한다. 명시적인 역사 참조를 구분하고 역사 문서를 기본 순회하지 않는다. JSON findings는 `path`, `line`, `code`, `message`; exit 0 clean, 1 findings, 2 invocation error다. delete/apply 옵션은 없다.
