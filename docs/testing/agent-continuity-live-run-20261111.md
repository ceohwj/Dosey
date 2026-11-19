# 실제 개발 작업의 연속성 적용 결과

2026년 11월 11일 실제 Dosey 개발 작업 `continuity-live-adoption-audit`를 완료했다. 작업 ID를 전달받지 않은 읽기 전용 에이전트가 작업을 찾고 필요한 지식을 읽었다. 메인 에이전트는 사용량을 보존하며 검증과 독립 검토를 마친 뒤 완료 상태를 저장하고 정확한 writer claim을 해제했다.

이 문서는 완료 후 정리한 기록이다. 실행 결과와 출처를 요약한 [이식 가능한 JSON 증거](evidence/agent-continuity-live-run-20261111.json)를 함께 보존한다. 이 JSON은 실행 당시 기록에서 비민감 사실을 추출한 자료이며, 새 작업의 진행 정본이나 승인 기록이 아니다.

## 실제 작업에서 확인한 내용

이번 적용은 실제 프로젝트 작업 한 건이다. 심어 둔 결함이나 합성 fixture를 사용한 시연이 아니다. 당시 허용된 변경은 보고서 한 파일이었으며 제품, UI, Source, Docker, API, DB 변경은 포함하지 않았다.

새 읽기 전용 에이전트에는 task ID를 주지 않았다. 에이전트는 작업을 발견하고 목표, 허용 파일, 다음 행동, 사용량을 확인했다. [지식 인덱스](../ai/knowledge-index.md)와 실제 실패 기록 두 개도 읽었다. 감사 중 쓰기는 0건이었다. 이 감사에서 사용자 맥락 재설명과 결과 복사 전달은 각각 0회로 기록됐다. 이는 이 실행에 한정한 관측이다.

첫 복원 시점은 revision 2였다. 작업은 running이고 operation_in_flight가 true였으므로 snapshot 판정은 UNKNOWN, reconciliation_required는 true였다. 사용량은 1/4 cycles와 0/1200초로 보존됐다. snapshot UNKNOWN을 테스트 실패나 사용량 초기화 근거로 해석하지 않았다.

메인 에이전트는 정확한 기존 claim과 알려진 누적 counter를 실행 증거와 대조했다. 163.671초의 wall time을 반영하고 in-flight를 해제한 뒤 snapshot은 MATCHED_SNAPSHOT이었다. 이후 보고서 writer를 배정하기 전에 cycle 2와 in-flight를 저장했다. 이는 [workflow](../ai/workflow.md)의 제한된 복구 절차를 적용한 사례다. 사용량이 null인 경우의 자동 복구를 검증한 결과는 아니다.

## 검증과 완료

원래 보고서는 revision 4의 작성 시점 상태를 기록했다. 원문 SHA-256은 `06d9f986e7d5102913dd3cd4f28e9c62a503792f3a40539c804513bcbafcfdd4`다. 당시 후속 검증과 claim 해제는 대기 중이었다. 이 문서는 완료 증거를 반영한 새 본문이므로 원문과 같은 hash라고 주장하지 않는다.

이후 기본 문서 검사와 보고서의 hygiene 및 링크 검사에서 findings는 0건이고 exit는 0이었다. git diff --check도 exit 0이었다. 메인 에이전트는 전체 내용, 공백, 변경 범위를 확인했다. 독립 native reviewer는 원문 hash와 단계별 사실 및 안전한 상대 참조 7개를 확인하고 지적 없이 APPROVE를 반환했다. reviewer는 테스트를 재실행하지 않았다. 이 검토는 원래 작성 시점 본문을 대상으로 했으며, 현재 이식본의 새 검토로 간주하지 않는다.

최종 작업은 phase complete, revision 7이다. 사용량은 3/4 cycles와 811.241/1200초다. 시간은 wall time 기준이며 provider token이나 CPU 측정값이 아니다. 최종 restore는 MATCHED_SNAPSHOT이고 reconciliation_required는 false였다. 정확한 claim의 release가 성공했고 당시 활성 claim이 없음을 확인했다. snapshot 일치는 테스트 PASS와 별개의 판정이다.

## 재발한 inventory 문제

[기존 inventory 실패 기록](../failures/20261111-test-snapshot-inventory.md)의 교훈은 이번 실행에서도 필요했다. 게시 입력을 보관한 input-local의 Python 테스트 복사본 두 개가 다시 수집되어 inventory가 exit 1을 반환했다. Git ignore만으로 테스트 수집을 막을 수 없었다.

메인 에이전트는 자신이 소유한 구현 및 테스트 백업 4개만 .snapshot 확장자로 바꿨다. bytes의 hash를 보존했고 구현이나 테스트 분류기는 변경하지 않았다. 검사 예외도 추가하지 않았다. 이후 inventory는 exit 0과 fully classified 결과를 반환했다. 실제 작업에서도 같은 실수가 재발했으므로 이미 읽은 교훈을 artifact 생성 단계에 적용해야 한다.

[file URI 검사 실패 기록](../failures/20261111-hygiene-file-uri.md)에 따라 문서 참조에는 안전한 저장소 상대 링크를 사용했다. 검토 복사본의 저장 형식과 링크 경계가 바뀌면 해당 실패 기록의 재검토 조건을 다시 확인해야 한다.

## 게시와 병합의 근거

실제 작업의 로컬 기준은 `bcac7835420118e13ee235410c6b9fdd21224b14`였다. 선행 게시의 기준은 `da4de67144f92ac1d1bdca23ddb375daec94df1f`, source commit은 `8c320e82f3345d1ebf6870b882e2d7890c58406f`였다. 핵심 구현 및 테스트 4개의 hash는 일치했고, 원격 16개 파일은 다른 세션 baseline을 제외한 게시 범위였다. 정책 문서는 별도로 투영했으므로 로컬 전체 checkout과 동일한 범위는 아니다.

[PR 2](https://github.com/ceohwj/Dosey/pull/2)는 squash 병합됐으며 merge commit은 `cf509d76b6eaf05126eb8a6f56915be1ff4357cc`다. [기록된 CI 실행](https://github.com/ceohwj/Dosey/actions/runs/42989539396)의 11개 check는 모두 success였고 대상 HEAD는 위 source commit이다. 이 결과를 다른 commit의 CI 성공으로 확대하지 않는다.

게시 전 138개 테스트와 관련 계약 검사, inventory, Ruff check 및 format이 통과한 근거도 선행 실행에 있다. 이 문서의 작성자는 해당 테스트나 CI를 재실행하지 않았다. 이번 이식본의 내용과 참조 자체 점검 이후 검사와 검토는 메인 에이전트가 별도로 수행한다.

## 적용 범위와 한계

[작업 템플릿](../ai/task-template.md)은 진행 상태와 결과 상태를 구분하고, [실패 기록 안내](../failures/README.md)는 재현 근거와 재검토 조건을 유지하도록 한다. 이 실행은 그 절차를 실제 작업에 적용한 근거다. 선행 합성 검증은 [기존 검증 기록](agent-workflow-20261111.md)에 별도로 남아 있다.

writer claim은 협력 세션의 쓰기 입장 조정이며 OS 격리나 추가 권한이 아니다. App 시작 hook, 상시 daemon, 영구 대화 기억을 구현하거나 검증한 결과도 아니다. 다른 세션의 자동 이관, 사용량 불명 상태의 자동 복구, 제품 배포는 이번 범위에 없었다. 기록된 개발 도구 PR의 병합은 의료 안전이나 Source 공개 승인 또는 제품 출시를 뜻하지 않는다.
