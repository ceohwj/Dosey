# 개발 에이전트 연속성 검증 — 2026-11-11

## 상태와 근거

이 기록은 최초 로컬 구현 검증과 게시용 스냅샷 검증을 구분한다. 최초 구현은 분리 worktree의 `bcac7835420118e13ee235410c6b9fdd21224b14`에서 시작했다. 게시 범위는 연속성 helper·회귀 테스트·보고 전용 hygiene·CI 연결과 관련 문서다. 다른 세션의 정책·역할 설정·검토 helper baseline은 게시 범위에 포함하지 않는다.

이식 가능한 [근거 JSON](evidence/agent-continuity-20261111.json)에 최초 실행의 출처·결과와 게시용 검증 상태를 기록한다. 로컬 검토 원장 전체나 원래 환경의 복구 artifact를 게시 저장소에 복제한 것은 아니다. 아래 과거 실행 수치는 이번 게시 스냅샷에서 재실행한 결과가 아니다.

**게시 스냅샷 로컬 검증: 테스트138통과(6.75초); 최종 문서·HTML 참조 검사도 통과했다.** CLI56+hygiene27+기존 CI·정책55를 실행했으며 게시하지 않는 review helper24는 제외했다. 기본5개 문서 hygiene, inventory, Ruff check/format과 syntax 검사가 통과했다. 최종 문서 변경의 검사 결과와 대상 revision/hash는 근거 JSON에 기록했다. 원격 GitHub CI는 NOT_RUN이며 merge·배포·실제 업무 채택을 뜻하지 않는다.

## 구현 범위

- [작업 helper](../../scripts/agent_workflow.py): canonical task discovery/restore, 협력적 worktree 단일 writer claim, revision CAS checkpoint/release. canonical을 먼저 원자 저장하고 파생 cache를 나중에 저장한다.
- [workflow](../ai/workflow.md)와 [task 템플릿](../ai/task-template.md): dispatch 전 claim과 budget/in-flight 저장, 독립 검토 → 지정 writer 수정 → 재검증, 중단 복구 규약.
- [지식 인덱스](../ai/knowledge-index.md)와 [실패 기록 안내](../failures/README.md): 기존 Decision/ADR·계약 연결, 기각·관측·가설·검증 분리와 재검토 조건.
- [hygiene scanner](../../scripts/ci/check_agent_hygiene.py): 지정 문서 링크/anchor와 한정된 source 경로의 임시 이름 후보를 JSON으로 보고한다. 삭제·수정 기능과 전역 dead-code·의미적 drift 증명은 없다.
- CI (`.github/workflows/checks.yml`): docs-only 변경도 hygiene job 대상이다. 실제 원격 실행 여부는 별도 근거가 필요하다.

새 opt-in task만 사용한다. claim은 협력하는 controller 간 입장 조정이며 OS 접근 권한이나 악의적 동시 변경 차단 장치가 아니다. 실제 sandbox·파일 배정·사용자 권한이 우선한다. App 시작 hook, 상시 daemon, 영구 대화 기억, 자동 provider 호출은 구현하지 않았다. 제품·계약·안전·merge·출시 결정과 외부 승인 조건을 변경하지 않는다.

## 최초 로컬 검증: 역사적 실행

최초 검증은 Framework Python 3.14에서 stdlib 개발 도구의 동기 테스트에 한정해 plugin을 비활성화하고 실행했다. 제품 async 테스트 통과를 뜻하지 않는다.

| 범위 | 당시 기록 |
| --- | --- |
| 기존 review helper baseline | 24통과. 해당 helper는 이번 게시 범위 밖 |
| 기존 CI 분류/inventory/DB 정책/protected writer baseline | 55통과 |
| task CLI | 56통과 |
| hygiene | 초기22개에서 file URI 회귀 추가·수리 후27통과 |
| 최종 관련 전체 실행 | 161통과/1 inventory 실패 |
| 소유 검토 복사본 확장자 정리 후 inventory 재실행 | 17통과/exit0 |

**162개 사례의 통과 근거는 161통과/1실패 실행과 후속17통과 실행에 나뉜다. 단일 전체 PASS가 아니다.** 신규83개는 CLI56+hygiene27이며 기존79개는 baseline24+55다. 최초 구현의13개 변경 파일이라는 수치는 당시 baseline 대비 범위이며 게시용 파일 수나 검증 범위를 뜻하지 않는다.

당시 기본 문서5개 scanner findings는0건, mutation은false였다. 관련 검증/실패 문서 링크, Ruff check/format4파일, syntax와 diff 검사도 통과 기록이 있다. 독립 검토는 file URI 결함과 복구 설명을 지적했고 수정 범위 재검토에서 ADDRESSED / APPROVE를 기록했다. 이는 과거 내부 검토 근거이며 현재 snapshot의 PASS나 외부 승인으로 승계하지 않는다.

[file URI 검사 누락](../failures/20261111-hygiene-file-uri.md)과 [검토 복사본 inventory 충돌](../failures/20261111-test-snapshot-inventory.md)에 실제 실패·수리·재검토 조건을 남겼다.

## 최초 합성 시연

읽기 전용 native agent에게 task ID나 대화 없이 문서와 합성 canonical task만 제공했다. cache 없이 `continuity-restore-pilot`을 발견해 목표·허용 파일·완료·gap·다음 행동을 복원하고 budget1회/15초와 snapshot UNKNOWN을 유지했다. 사용자 맥락 재설명·결과 복사 전달은 시연 내 각각0회였다. 실제 App 재시작이나 다른 호스트의 자동 복원 검증은 아니다.

제품 밖 합성 formatter의 단수·복수 결함은 초기1개 테스트가 놓쳤다. 독립 reviewer의 MEDIUM finding 후 main이 정확한 claim을 획득하고 dispatch 전에 budget2회/in-flight를 저장했다. 지정 writer의 회귀 테스트 RED는2실패/1통과, 수정 후 GREEN은4통과였다. reviewer는 ADDRESSED로 확인했고 main도4개를 재실행했다. canonical revision4에 완료·clear in-flight를 저장한 restore는 MATCHED_SNAPSHOT이었고 정확한 claim만 release했다. dispatch부터 보고까지 대기를 포함한507.054초를 보수적으로 차감했으며 provider token/CPU 측정은 아니다.

## 복구 경계와 미실행

snapshot MATCHED_SNAPSHOT은 파일 일치이며 테스트 PASS가 아니다. cache 누락·손상에 따른 UNKNOWN 자체는 budget 차단을 뜻하지 않는다. 정확한 기존 claim과 알려진 누적 counter가 있을 때만 counter를 줄이지 않는 checkpoint로 증거 기반 in-flight 해제를 지원한다. `null`→확정값 갱신과 unknown/in-flight/상한 도달 task의 새 claim 획득은 지원하지 않는다. 기록·증거를 보존하고 별도 승인된 복구 범위 전까지 BLOCKED를 보고하며 dispatch하지 않는다. 유효한 claim이 없어 canonical에 blocked를 저장할 수 없으면 밖에 보고한다. reset·force·TTL·PID 부재로 takeover하지 않는다.

최초 검증에서 mypy는 설치·offline cache 부재로 NOT_RUN이었다. 원격 GitHub CI·Python3.13·외부 Claude review는 실행하지 않았다. 제품/DB/provider/의료 eval은 관련 실행 코드 변경이 없어 제외했다. 게시용 검증은 위 역사적 실행과 별도이며 실제 실행 결과만 근거 JSON에 추가한다. 실제 업무 채택·다중 호스트·임상 출시 준비는 이 합성 검증으로 증명되지 않는다.
