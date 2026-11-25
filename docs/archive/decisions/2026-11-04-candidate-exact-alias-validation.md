# Candidate 초기 Exact·승인 Alias 검증 범위

- Decision ID: `PD-DOSEY-20261104-CANDIDATE-STAGES-1`
- 상태: Human Owner가 초기 검증 stage를 선택하고 해당 범위 실행을 지시함. 아래 미확정 정책/계약·실제 Source 사용·migration·공개 승인은 포함하지 않음.
- 지시 근거: “Exact·승인 Alias부터 검증”, 이어서 “그 범위로 계획대로 진행…수치 정책이나 계약 확정이 필요한 건 근거를 모아 권고안…기존 재시도와 안전 규칙·공개 차단 유지”.
- 선행: [합성 내부 연결](2026-10-23-candidate-search-synthetic-connection.md), [식별 Target](../../contracts/targets/post-mvp-1/medication-identification-v1.md).

## 확정 범위

1. 초기 Candidate adapter에서 선택 가능한 stage는 `PRODUCT_NAME_EXACT`·`APPROVED_ALIAS_EXACT`의 비어 있지 않은 부분집합이다. 설정이 Trigram/Dense 선택을 허용하면 검색 전에 기존 `503 CANDIDATE_RESOLVER_UNAVAILABLE`로 차단한다. caller policy를 조용히 바꿔 digest와 실행 의미를 다르게 만들지 않는다.
2. 양수 Trigram hit는 경쟁 후보·margin·진단에 유지한다. Trigram-only로 READY를 만들거나, 경쟁 후보를 삭제해 Exact 하나를 인위적으로 READY로 만들지 않는다. 두 개 이상 eligible인 공식 identity는 기존 AMBIGUOUS를 유지한다.
3. 기존 일반 pure Resolver는 후속 실험을 위한 허용 stage 어휘를 그대로 유지한다. 이번 제한은 초기 Candidate adapter의 실행 경계다. 확인 전 자동 MATCHED·새 공개 DTO/enum·Provider/HYBRID·보험코드 확장은 없다.
4. 기존 ownership → 저장 성공 replay → 신규 mutation Guard 순서, legacy/v2 digest, HMAC·암호화 응답·TTL은 변경하지 않는다. 일반 DI context=None, PUBLIC_TRACK_F_ENABLED=false와 Source/의료/Privacy gate를 유지한다.

## 근거와 미확정 범위

실제 Exact/Alias SQL score는 1.0이며 합성 evaluator는 stage score의 최댓값을 사용한다. relevance 예제값을 올리는 것만으로 잘못된 공식 identity·Alias·함량 문제를 해결할 수 없다. 현재 `release_eligible=False` 제한은 유지하고 기존 합성 fixture의 숫자는 운영 threshold로 채택하지 않는다. [검색 구현](../../../backend/app/services/rag_candidate_index_search.py), [pure policy](../../../backend/app/services/rag/candidate_policy.py), [adapter](../../../backend/app/services/candidate_search_resolution.py).

실제 Source max-age/cadence·capture clock 발행·PRODUCT_IDENTIFICATION 승인 범위·운영 gold/threshold·영속 Guard/FAIL 감사·retention·권한/삭제/새 DB schema는 별도 결정이 필요하다. [구체적 영속 계약 변경안](../../contracts/targets/post-mvp-1/candidate-request-guard-audit-v1.md)에 기존 구현 재사용과 필요한 델타를 정리한다. 이 문서가 그 미확정 항목을 승인하지 않는다.

실행 검증과 Source 관측 inventory는 [검증 기록](../../testing/candidate-policy-evidence-20261104.md)에서 상태를 구분해 기록한다. 과거 360 PASS를 이번 변경의 검증 결과로 재사용하지 않는다.

후속 결정: [영속 감사 합성 내부 승인](2026-11-04-candidate-guard-audit-internal.md)으로 두 저장 구조와 내부 transaction·삭제/권한 검증만 확정했다. 실제 Source·수치·운영 배포·공개와 Current 승격은 여전히 제외한다.
