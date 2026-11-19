# Guide Sync REQUEST Authority 계약 v1

| 항목 | 값 |
| --- | --- |
| 상태 | **Approved Target · CR-A 승인; CR-A 연결·합성 검증 완료; 실제 정책 연결 및 Current 승격 미수행. Current 아님** |
| 결정 | [PD-DOSEY-20261104-GUIDE-SYNC-CR-A](../../../governance/decisions/2026-11-04-guide-sync-request-authority.md) |
| 적용 경로 | 기존 동기 Guide 생성 `201`, 공개 flag OFF |
| 비적용 | 비동기 `202`/Outbox, Chat, CR-B 공식 Identification carrier, CR-C 승인 verifier |

## 요청 권한과 수명

Bootstrap은 process 수준 client/session factory와 `for_session(session)`만 구성한다. 그 factory가 인증된 요청의 세션에 결속한 provider를 만든다. 요청별 issuer는 SELF 소유 Guide·처방·버전과 명시된 Guide operation/scope를 입력받아 **실제 evaluator**의 `REQUEST` 결정을 발급·저장한다. 그 결과의 정확한 Guard ref와 런타임 환경·Bundle·실행/retrieval manifest pin을 기존 `GuideRuntimeContextSnapshot`에 담는다. 요청별 provider는 snapshot을 기존 `GuideSyncRuntimeAuthority`로 투영하며 권한을 재선택하거나 새 PASS를 만들지 않는다.

같은 user/operation의 다른 요청은 별도 identity를 가진다. 현재 POST에는 retry identity가 없고 CR-A는 자동 재시도를 제공하지 않는다. 재POST는 새 요청이며, 향후 멱등 재시도는 별도 계약에서 정한다. 전역 Guard, 최신 Guard, process cache, 환경의 최신 Bundle을 권한 대용으로 사용하지 않는다. issuer/evaluator가 없거나 FAIL·불명확한 판단이면 성공 snapshot을 반환하지 않고 Provider를 호출하지 않는다. 자동 PASS는 금지한다.

## 동기 transaction 경계

| 단계 | DB 경계와 필수 결과 |
| --- | --- |
| A — 접수 | Prescription SELF 소유권·현재 버전 잠금 → issuer의 `REQUEST/PASS`와 exact Guard 검증 → Guide·`PROCESSING` AI Job·Full Execution Context·현재 `MATCHED` Identification pin 저장 → **commit** |
| B — 실행 | A의 불변 `GuideRuntimeRequestCarrier`를 사용해 Provider 실행. 요청 세션의 접수 transaction과 잠금을 유지하지 않음. Worker reader는 필요할 때 별도 세션으로 짧게 조회. 오프라인 검증에서는 Provider 대역만 사용 |
| C — 최종화 | 새 transaction에서 A와 공유한 세션의 identity map을 먼저 만료하고 User `FOR SHARE`를 획득한 다음 Prescription → AI Job → Guide → Environment `FOR UPDATE`. exact job/guide/user/version, persisted Context 전체 pin, canonical Bundle·execution/retrieval manifest, 현재 환경 ACTIVE pointer/revision, 같은 버전의 `MATCHED` Identification ID 집합을 대조. 요청별 evaluator가 현재 Patient Context·Source 정책을 같은 transaction 안에서 `revalidate()`하고 그 권한 사실을 필요한 잠금 아래 확인 |

C에서 현재성이 불일치하면 생성 결과를 공개하지 않고 `Guide.release_decision=STALE`, `release_is_current=false`, `AI_JOB=STALE`을 함께 커밋한 뒤 기존 `409`를 반환한다. 성공은 검증된 projection과 Job을 함께 커밋한 뒤 기존 `201`을 반환한다. A의 일반 준비 실패는 A를 rollback하여 부분 PASS/Job/Context를 남기지 않고, 별도 transaction으로 실패 Guide만 기록하며 B를 실행하지 않는다. **명시적 evaluator FAIL**에 한해 감사 기록을 별도 commit으로 보존한 뒤 기존 `503` 오류로 응답한다. 감사 실패와 성공 접수를 하나의 rollback에 묶어 잃지 않는다.

## 검증 범위와 제한

Contract/integration 테스트는 요청마다 다른 issuer identity, snapshot exact readback, A 커밋의 B 가시성, B 구간의 DB transaction 부재, C 잠금 순서와 동시 처방·환경 변경, FAIL 감사 보존, STALE 결과 비공개, commit 이후 `201`을 입증해야 한다. 실제 OpenAI 호출 없이 대역으로 동기 latency 기준값을 측정하며 실제 Provider 성능이나 비동기 비교 결과로 일반화하지 않는다.

현재 Patient Context와 Source policy의 실제 evaluator 연결은 필수다. 단순 DB row 존재나 hash 일치만으로 이 정책을 통과시켜서는 안 된다. 공개 flag는 OFF이고, 모든 구현·통합 증거 및 별도 공개 조건이 충족되기 전에는 Current 또는 사용자 공개로 승격하지 않는다. Route/DTO/schema와 Outbox에는 이 계약만으로 변경을 추가하지 않는다.

동기 Job의 5분 lease는 실행 중 표시를 위한 임시 경계다. 만료 후 자동 복구·재실행을 제공하지 않는다. C의 User 공유 잠금은 탈퇴 경로의 User 독점 잠금과 직렬화하며, evaluator는 현재 권한 사실의 잠금·재검증 책임을 가진다.
