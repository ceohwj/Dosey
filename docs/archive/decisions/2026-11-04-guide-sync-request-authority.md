# Guide 동기 REQUEST 권한·최종화 결정

- Decision ID: `PD-DOSEY-20261104-GUIDE-SYNC-CR-A`
- Human Owner 결정: 2026-11-04 “추천안대로 CR-A 승인할게. 동기 201을 유지하면서 요청별 issuer와 snapshot 전달, 접수·최종화 transaction 분리부터 구현하고 테스트해줘. OpenAI는 호출하지 말고 대역으로 동기 기준 수치부터 측정해줘.”
- 정본 목표 계약: [Guide Sync REQUEST Authority v1](../../contracts/targets/post-mvp-1/guide-sync-request-authority-v1.md)
- 상태: **CR-A 승인 · Target; CR-A 연결·합성 검증 완료; 실제 정책 연결 및 Current 승격 미수행. Current 아님.**

## 승인 범위

인증된 Guide 요청마다 실제 evaluator 결과를 사용하는 issuer가 `REQUEST` 결정을 발급하고, 정확한 ref가 담긴 불변 `GuideRuntimeContextSnapshot`을 요청별 authority provider에 전달한다. Bootstrap은 process 수준 자원과 `for_session(session)` 구성만 보유한다. 현재 동기 `201` API는 유지하면서 접수 A와 최종화 C를 별도 DB transaction으로 커밋하고, 그 사이 B에서 DB transaction 없이 runtime을 실행한다. 실제 OpenAI 호출 없이 대역으로 동기 기준 수치를 측정한다.

Guide의 기존 route/DTO/schema를 늘리거나 비동기 `202`/Outbox 흐름으로 바꾸지 않는다. CR-B의 공식 Identification identity 공급과 CR-C의 승인 verifier·별도 안전 권한은 이번 승인이 아니다. 관련 공개 flag는 OFF를 유지하고, 실제 evaluator를 연결하지 못하면 자동 PASS로 대체하지 않는다.

## 실패와 공개 경계

- A는 처방 소유권·현재 버전을 잠근 뒤 issuer/Guard를 검증하고 Guide·`PROCESSING` Job·실행 Context·고정 Identification을 함께 저장·커밋한다. 일반 준비 실패는 A를 rollback해 부분 PASS·Guide·Job을 남기지 않고 Provider를 실행하지 않는다. 명시적 evaluator FAIL의 감사 기록만 별도 transaction에 보존하고 기존 `503` 오류 의미를 따른다.
- B는 A 커밋 이후 불변 요청 snapshot만 소비한다. DB transaction을 열어 둔 채 Provider를 호출하지 않는다.
- C는 A에서 재사용된 세션의 identity map을 만료하고 User `FOR SHARE`를 먼저 잡은 뒤 Prescription → AI Job → Guide → Environment 순서로 잠근다. 고정 Context·현재 처방/Identification/Bundle/manifest/환경을 대조하며, 요청별 evaluator는 현재 Patient Context·Source 정책의 권한 사실을 필요한 잠금 아래 C 안에서 다시 평가한다.
- 불일치는 생성 결과를 공개하지 않고 Guide의 STALE projection(`is_current=false`)과 `AI_JOB=STALE`을 함께 커밋한다. 기존 `409` 응답 의미를 유지한다. 성공 결과도 C 커밋 이후에만 기존 `201`로 반환한다.

## 미완료 게이트

이 결정은 구현 또는 사용자 공개의 증거가 아니다. 실제 evaluator, C 재검증, FAIL 감사, 동기 `201` 전체 흐름의 통합 테스트와 합성 성능 측정 결과를 확인해야 한다. OpenAI credential/운영 호출, 외부 승인, merge·release는 별도 결정이다.

현재 POST에는 retry identity가 없다. CR-A는 자동 재시도·복구를 제공하지 않으며 재POST는 새 요청이다. 향후 멱등 재시도는 별도 계약에서 결정한다. 동기 Job의 5분 lease는 임시 실행 표시이며 자동 recovery 약속이 아니다.
