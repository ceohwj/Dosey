# Agent 작업 명세 템플릿

Issue 또는 하나의 작업별 문서에 작성한다. 작업 전체를 관리하는 Integrator가 갱신하며, 구현 Agent는 결과를 반환한다. 병렬 작업자가 하나의 TASK.md나 MEMORY.md를 함께 덮어쓰지 않는다. 규칙 정본은 [AGENTS](../../AGENTS.md), 실행 순서는 [workflow](workflow.md)다.

```text
TASK_ID:
GOAL: 사용자 관점의 결과
OWNER_AGENT: 실제 환경에서 사용 가능한 지정 구현 역할
BASE_REVISION: 실제 commit SHA
EXISTING_CHANGES: 보존할 기존 변경
SCOPE:
ALLOWED_FILES: 정확한 파일 목록 (예상 신규 파일도 명시)
FORBIDDEN_FILES:
DEPENDENCIES: 선행 task/contract 및 완료 조건
AUTHORITATIVE_CONTRACT: current/target/proposed 상태와 정본 경로
ACCEPTANCE_CRITERIA: 재현 가능한 결과
REQUIRED_TESTS: 명령·환경·성공 조건, 외부 호출 허용 여부
NON_GOALS:
CONTRACT_REQUEST: 필요 없음 또는 Human Owner 결정과 근거
REVIEW_SCOPE: 선택 파일, commit 또는 snapshot, 외부 검토 필요성
```

```text
STATUS: READY_FOR_REVIEW | BLOCKED | CONTRACT_REQUEST | DEPENDENCY_WAIT | TEST_FAILED | STOPPED_SCOPE_VIOLATION
RESULT:
CHANGED_FILES:
CHECKS: 명령 / exit / 결과 / 근거 위치
NOT_RUN: 사유
KNOWN_GAPS:
REVIEW: 실제 검토 방식, 검토 대상 SHA/hash, 지적과 처리
CHANGE_COMMITS: 확인된 실제 SHA, 무엇을 왜 바꿨는지, 검사 결과와 근거. 없으면 미커밋
NEXT_DEPENDENCIES:
```

`READY_TO_MERGE`는 Integrator의 검증 후 추천 상태로만 사용한다. 최종 merge·공개는 Human Owner 결정이며 외부 승인 조건은 별도로 충족해야 한다.

## Canonical task JSON

새 opt-in task는 위 템플릿을 `.omx/plans/dosey-agent-tasks/<task-id>/task.json`으로 표현한다. 기존 문서를 이관하지 않는다. Integrator가 claim을 먼저 획득하고 canonical을 갱신하며 구현자는 결과를 반환한다. 시작·복구·종료와 CLI는 [workflow](workflow.md)를 따른다. canonical만으로 목표·범위·다음 행동·budget을 복구할 수 있어야 한다.

다음은 형식 설명용 합성 예시이며 실제 배정·수행 기록이 아니다. base revision은 예시 작성 시 기준 commit이다. allowed files는 정확한 repository 상대 파일 경로이며 glob·디렉터리 포괄 배정이 아니다. task ID는 소문자 영숫자·하이픈 1~64자만 허용하고 점·슬래시·상위 경로를 금지한다.

```json
{
  "schema_version": 1,
  "task_id": "synthetic-doc-links",
  "goal": "지식 인덱스의 지정 로컬 링크를 확인하고 결과를 반환한다",
  "base_revision": "bcac7835420118e13ee235410c6b9fdd21224b14",
  "allowed_files": ["docs/ai/knowledge-index.md"],
  "forbidden_files": ["README.md", "docs/architecture.md"],
  "dependencies": [],
  "authoritative_contract": ["AGENTS.md", "docs/ai/workflow.md"],
  "acceptance_criteria": ["지정 문서의 참조 확인 결과와 gap을 보고한다"],
  "required_tests": ["내용·로컬 참조·전체 diff와 git diff --check 확인"],
  "non_goals": ["제품·API·DB·Source·Docker·모델 변경"],
  "phase": "planned",
  "revision": 0,
  "completed": [],
  "known_gaps": [],
  "next_action": "claim 획득 뒤 지정 문서와 링크 대상을 읽어 확인한다",
  "knowledge_refs": ["docs/ai/knowledge-index.md", "docs/contracts/README.md"],
  "budget": {
    "max_cycles": 3,
    "max_active_seconds": 1200,
    "cycles_used": 0,
    "active_seconds": 0
  },
  "operation_in_flight": false
}
```

| 필드 | 기존 명세와의 대응·검증 |
| --- | --- |
| `schema_version`, `task_id`, `base_revision` | version 1, TASK_ID, BASE_REVISION |
| `goal`, `allowed_files`, `forbidden_files` | GOAL, SCOPE의 정확한 ALLOWED_FILES, FORBIDDEN_FILES |
| `dependencies`, `authoritative_contract` | DEPENDENCIES, AUTHORITATIVE_CONTRACT의 경로. 원본의 current/target/proposed 상태 확인 |
| `acceptance_criteria`, `required_tests`, `non_goals` | ACCEPTANCE_CRITERIA, REQUIRED_TESTS, NON_GOALS |
| `phase`, `revision` | phase는 `planned/running/review/complete/blocked`; revision은 bool이 아닌 0 이상 정수. 최초 expected revision 0 → 저장 revision 1 |
| `completed`, `known_gaps`, `next_action` | 수행 결과, KNOWN_GAPS, 다음 행동. checkpoint에만 저장하지 않음 |
| `knowledge_refs` | 안전한 repository Markdown 경로 배열. Issue URL은 별도 evidence로 참고할 수 있으나 CLI 원격 fetch는 없음 |
| `budget`, `operation_in_flight` | 상한·누적 cycles/active seconds와 미완료 실행 표시. 예시 기본 3/1200은 작업별 명시 변경 가능 |

예시의 모든 필드는 필수다. 목록은 문자열 배열, goal/base revision/next action은 문자열, in-flight는 bool이다. counter는 bool/음수를 허용하지 않고 사용량이 불명확하면 `cycles_used` 또는 `active_seconds`를 `null`로 남긴다. UNKNOWN을 0으로 치환하지 않는다. 상한을 명시하고 사용량을 조용히 감소시키지 않는다. 추가 evidence 필드는 bounded·비민감 값만 허용한다. OWNER_AGENT·EXISTING_CHANGES·CONTRACT_REQUEST·REVIEW_SCOPE·CHECKS·NOT_RUN·REVIEW 등 기존 상세 정보는 task evidence로 연결하며 별도 전역 진행 ledger를 만들지 않는다.

`phase`는 진행 단계이며 결과 STATUS와 별개다. READY_FOR_REVIEW는 `review`, 차단은 `blocked`와 known gaps로 표현한다. READY_TO_MERGE 추천을 merge 승인 상태로 추가하지 않는다. 검증 명령·exit·시점·revision/hash·지적과 수리 증거를 결과에 남기며 unrun을 PASS로 표시하지 않는다.

## 선택적인 커밋 근거

`CHANGE_COMMITS`는 메인 에이전트의 커밋 확인 후 인계에 사용한다. Issue와 PR 설명은 [글쓰기 지침](github-writing.md)을 따른다. 실제 commit SHA와 포함 파일을 확인하기 전에는 예정 커밋을 완료한 기록으로 남기지 않는다.

다음 JSON은 선택 evidence 필드 `change_commits`의 형식 설명용 예시다. 독립 실행 task 예시가 아니며 `CONFIRMED_COMMIT_SHA`는 실제 SHA가 아닌 자리표시자다. 기록할 때에는 커밋 확인 후 실제 값으로 바꾼다. 검사 항목도 해당 커밋의 실제 실행 결과나 미실행 이유로 바꾼다.

```json
{
  "change_commits": [
    {
      "sha": "CONFIRMED_COMMIT_SHA",
      "summary": "무엇을 바꿨는지 쓰는 자리",
      "reason": "왜 필요한지 쓰는 자리",
      "files": ["docs/ai/github-writing.md"],
      "checks": [
        {
          "name": "검사 이름을 쓰는 자리",
          "result": "NOT_RUN",
          "reason": "형식 예시이므로 실행하지 않음"
        }
      ]
    }
  ]
}
```

커밋이 없으면 이 필드를 생략하거나 빈 배열로 둔다. `schema_version: 1`의 기존 19개 필수 필드와 검증 규칙은 바꾸지 않는다. 이 선택 기록은 bounded 비민감 evidence이며 별도 진행 정본이 아니다. helper의 자동 Git 추적이나 새 필수 schema 기능을 의미하지 않는다.
