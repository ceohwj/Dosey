# 개발 실패·기각 기록 안내

반복 작업 전에 실패·기각 근거와 재검토 조건을 확인한다. 이 디렉터리는 개발 과정의 비민감 증거를 연결하며 제품 결정·안전 승인·의료 판단이나 task 진행 정본을 만들지 않는다. 실제 기록: [file URI 검사 누락](20261111-hygiene-file-uri.md), [검토 복사본 inventory 충돌](20261111-test-snapshot-inventory.md). 분류와 검증 범위는 해당 기록의 증거를 확인한다.

## 분류와 재사용

- `rejected`: 채택하지 않은 접근. 기각 주체·이유·당시 제약과 원래 Decision/검토 근거를 연결한다. 단순 실패 관측이나 영구 금지로 바꾸지 않는다.
- `observed`: 특정 환경에서 관측한 실패. 재현 입력·명령·exit·revision/hash를 기록하며 원인을 확정하지 않는다.
- `hypothesis`: 미검증 원인·수리 가설. 확인·반증 조건을 적고 사실로 인용하지 않는다.
- `verified`: 재현·검증 증거가 뒷받침하는 원인/수리. 환경·유효 revision·한계를 함께 기록하며 모든 환경의 성공이나 승인으로 확대하지 않는다.

관련 task·증상·원인·근거가 같은 항목을 먼저 찾고 새 사본 대신 재현 증거·재검토 결과를 연결한다. 새 revision·환경·정책 또는 반증 결과가 생기면 recheck한다. 근거 없이 같은 실패를 다시 dispatch하지 않고 task budget·다음 행동을 갱신한다. 관련 Decision/ADR는 기존 위치에 링크하고 복제하지 않는다.

## 기록 템플릿

아래는 미작성 형식이며 실제 관측·실패·기각 기록이 아니다. task의 evidence나 승인된 허용 파일에 기록한다. 새 파일은 먼저 Integrator가 정확한 소유권을 배정한다.

```text
ID / RELATED_TASK:
CATEGORY: rejected | observed | hypothesis | verified
SUMMARY:
SCOPE: 환경·허용 파일·revision/snapshot hash
OBSERVATION: 관측과 미확인 부분 분리
EVIDENCE: 비민감 재현 명령·exit·테스트/검토 근거 경로·시점
REJECTED_BASIS: 해당할 때 기각 주체·이유·기존 Decision 링크
HYPOTHESIS: 미검증 가설과 반증 조건
REPAIR: 지정 구현자·수리 범위·RED → GREEN·재검토 근거
LIMITS: 미실행·gap·다른 환경의 불확실성
RECHECK_WHEN: revision/환경/정책 변경 또는 반증 조건
RECHECK_RESULT: 확인 시점·대상 hash·결과 또는 NOT_RUN/UNKNOWN
DEDUP_REF: 기존 기록·Decision/ADR 경로
NEXT_ACTION: canonical task.json에서 갱신할 다음 행동
```

비밀·credential·의료 원문·provider 원시 응답을 복사하지 않는다. snapshot/검토 범위가 바뀌면 기존 검토는 STALE로 취급하고 필요한 범위를 다시 확인한다. unrun은 PASS가 아니며 agent 기록은 Human Owner나 외부 승인이 아니다. [workflow](../ai/workflow.md)의 수리 loop와 [task 템플릿](../ai/task-template.md)에 결과를 연결한다.
