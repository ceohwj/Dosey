# Guide Runtime Authority·Bootstrap 변경 요청 v1

| 항목 | 값 |
| --- | --- |
| 상태 | **CR-A 승인 → 별도 Approved Target; CR-B/C는 Proposed · 미승인** |
| 작성일 | 2026-11-04 |
| 작업 | RAG-AUTH-20261104 |
| 기준 | `bcac7835420118e13ee235410c6b9fdd21224b14` + 기존 미커밋 작업 트리 |
| 목적 | 요청별 REQUEST 권한을 실제 Guide 요청과 결속하고 기존 bootstrap seam으로 전달 |
| 승인 범위 | 2026-11-04 Human Owner가 CR-A 요청별 issuer·snapshot 전달과 동기 201의 접수/최종화 transaction 분리를 승인. [Decision](../../../governance/decisions/2026-11-04-guide-sync-request-authority.md)과 [Target](../../targets/post-mvp-1/guide-sync-request-authority-v1.md)이 CR-A 정본. CR-B/C 미승인 |

## 1. 재사용 조사

- [Runtime target](../../targets/post-mvp-1/rag-runtime-v1.md)의 Guide 접수는 graph 전에 Full REQUEST/PASS, Job, Full Execution Context, Identification과 Outbox를 같은 transaction에서 고정한다. 현재 동기 API는 201이며 target의 비동기 전환 완료가 아니다.
- [REQUEST Guard binding](request-guard-runtime-binding-v1.md)은 evaluator가 발급한 요청별 UUID/outcome을 저장한다. 같은 사용자·operation의 반복 요청도 서로 다른 request identity를 가진다. reader나 bootstrap은 PASS·UUID를 만들거나 기존 Guard를 자동 재사용하지 않는다. 해당 계약 자체도 Proposed이며 Guide orchestration 연결은 범위 밖이다.
- `GuideRuntimeContextSnapshot`과 `GuideJobIntakeTransactionAdapter`는 이미 있다. snapshot을 새 이름의 중복 DTO로 만들지 않는다. adapter는 미래 202 경로용이고 이번 작업에서 현재 201 API에 자동 연결하지 않는다.
- `GuideSyncRuntimeAuthorityProvider.resolve(user, guide)`는 현재 Protocol뿐이다. `GuideSyncRuntimeAuthority`는 정확한 환경·Bundle·실행 manifest·retrieval binding·Guard ref를 반환하지만, 입력에는 이 요청의 Guard ref나 신뢰할 발급자가 없다.
- `RagRequestGuardRuntimeBinding`에는 `guide_id`가 없다. user+operation 조회나 가장 최근 Guard 조회로 새 Guide 요청의 권한을 증명할 수 없다. 기존 read는 full artifact ref exact-read다.
- 기존 Worker dependency bundle은 이미 확정된 권한을 caller에게 요구한다. process-scoped factory에 request-scoped session, Guard, 관측 context를 공통 고정값으로 저장하는 것은 실제 요청별 권한 연결을 해결하지 못한다.

근거: `backend/app/services/guide_intake.py`, `guide_sync_runtime_execution.py`, `guide_sync_runtime_lifecycle.py`, `backend/app/repositories/rag_request_guard_runtime_binding_repository.py`, `rag_runtime/request_guard_runtime_binding.py`, `ai_worker/tasks/rag/guide_runtime_provider.py`.

## 2. CR-A: 요청 발급자와 snapshot 전달 — 승인된 후속 Target 참조

이 절의 초안과 결정 질문은 승인 전 검토 이력이다. Human Owner의 CR-A 결정 범위와 transaction 의미는 [Guide Sync REQUEST Authority v1](../../targets/post-mvp-1/guide-sync-request-authority-v1.md)이 소유한다. 아래 interface 초안은 정본이 아니며, 구현·통합 검증은 진행 중이다.

### 제안

bootstrap은 process-level client/session factory 및 **요청별 provider를 만드는 구성**만 보유한다. 인증된 요청 안에서 신뢰할 issuer가 이 Guide의 REQUEST 결정을 발급·기록하고, 그 결과의 정확한 ref를 포함한 기존 `GuideRuntimeContextSnapshot`을 반환한다. provider는 그 snapshot을 기존 `GuideSyncRuntimeAuthority`에 projection하며 임의 선택·승인 판단을 하지 않는다.

issuer의 입력에 필요한 의미:

| 값 | 출처·규칙 |
| --- | --- |
| authenticated user / SELF profile | 기존 인증·소유권 확인 결과. 외부 요청 body가 지정하지 않음 |
| Guide·처방·처방 version | 이번 Guide 생성의 정확한 참조. 소유권·currentness 확인 뒤 고정 |
| 요청 instance identity | 지정된 issuer가 발급. reader가 새 UUID로 권한을 가장하지 않음 |
| 환경·Bundle·실행/retrieval manifest | 구성에서 승인된 환경을 지정하고 접수 시 exact pin. 실행 중 latest 재선택 금지 |
| 요청 operation/scope | 기존 `GUIDE_SYNC_ANSWER` 및 승인된 Guide scope의 명시적 의미. hash에서 scope 복원 금지 |
| Source/approval 판단 | 해당 정책의 실제 evaluator 결과. 활성 Bundle이나 행 존재만으로 PASS를 추론하지 않음 |

검토할 interface 초안은 다음과 같다. **구현·승인된 shared port가 아니다.** caller 소유 transaction의 session을 사용하며 자체 commit은 하지 않는 안이다.

```python
async def issue_for_guide(
    *, session, user_id, self_profile_id, guide_id,
    prescription_id, prescription_version_id,
    runtime_environment_id, evaluated_at,
) -> GuideRuntimeContextSnapshot: ...
```

issuer는 입력의 user/profile/Guide/version을 검증하고, 구성에서 지정된 환경의 Bundle·manifest를 접수 시 고정한다. 반환 snapshot의 Guard는 이 호출에 대해 실제 평가·발급·저장된 요청 identity여야 한다. 이미 생성된 다른 Guide의 Guard ref를 넣어주는 resolver는 이 interface의 구현으로 인정하지 않는다. API body에 issuer 입력을 추가하지 않는다. 미발급/FAIL/불명확한 판단은 성공 snapshot을 반환하지 않으며, 그 실패의 201 경로 저장·오류 매핑은 아래 transaction 결정에 포함한다.

다음은 전달 관계의 설명이다.

```text
authenticated request + exact Guide/version
→ designated REQUEST evaluator/issuer
→ persisted per-request decisions + binding
→ existing GuideRuntimeContextSnapshot
→ request-scoped authority provider
→ existing lifecycle exact verification/pinning
```

### Owner가 결정할 항목

1. 신뢰할 issuer/evaluator의 실제 구현 주체와 승인 자료 원장. 외부 입력이면 exact artifact 검증 책임, 저장소 내부 구현이면 평가 정책·범위를 지정한다.
2. 현재 동기 201 흐름을 먼저 연결할지, target 202/Job/Outbox 접수와 함께 연결할지. 권고는 public API 전환과 provider slice를 분리하되, 201 연결도 별도 versioned 결정으로 request 발급·실패 transaction 의미를 명시하는 것이다.
3. PASS/FAIL 결정과 Guide/Job 저장의 transaction 순서. FAIL 감사가 outer rollback과 함께 사라져도 되는지, 보존이 필요하면 기존 감사 구조 재사용 여부를 결정한다. 자동 독립 commit은 추가하지 않는다.
4. 같은 요청의 retry identity와 별도 신규 요청을 구분하는 경계. 전역 Guard ref·사용자별 최신 Guard 선택·process cache를 허용하지 않는다.
5. process-level dependency와 request-level session/authority/ProviderCallContext의 분리. 현재 정적 bundle을 자동 활성화하지 않는다.

### 영향·테스트·migration

- 소비자: Guide route/Service/Sync lifecycle, DI/bootstrap, REQUEST writer/readers, Worker request carrier, Job/Outbox 전환 시 관련 API/Frontend.
- 기존 snapshot과 exact ref 저장을 우선 재사용한다. 이 단계의 제안만으로 새 table/column이나 `guide_id` Guard FK가 필요하다고 결론내리지 않는다. issuer·transaction 결정 후 migration 영향 재평가.
- 필수 테스트: 같은 user/op의 새 두 요청에 서로 다른 issuer decision, retry의 exact replay, 다른 Guide/version/user/Bundle/Guard 조합 거부, 권한 발급 실패 시 Provider 무호출, concurrent revoke/version 변경, rollback/FAIL 감사 의미, 실제 issuer→snapshot→lifecycle→저장 통합.

## 3. CR-B: 고정 Identification의 공식 identity 공급

현재 `MedicationIdentification`에는 immutable `code_system`·`canonical_code`가 저장되지만, Guide carrier에는 identification ID·약 ID·이름/함량 snapshot만 있다. Worker port `resolve_ordered()`의 concrete 구현은 없다.

권고 delta는 Backend의 기존 pinned MATCHED Identification 조회에서 공식 identity를 projection하고 Worker에 전달하는 것이다. 다음 두 **required** 필드 추가안을 제안하며 이번에 필드를 추가하지 않는다.

| 제안 필드 | 타입/누락 의미 | source |
| --- | --- | --- |
| `code_system` | canonical nonblank `str`; 누락·알 수 없는 체계는 해당 identification 사용 거부 | composite ID로 조회한 MATCHED `MedicationIdentification.code_system` |
| `canonical_code` | canonical nonblank `str`; 누락 시 사용 거부, 정규화·대체 금지 | 같은 immutable row의 `MedicationIdentification.canonical_code` |

기존 identification/medication ID와 이름·nullable 함량 snapshot은 유지한다. carrier version은 해당 경계를 versioned Decision에서 새 버전으로 고정하며 기존 v1을 조용히 재해석하지 않는다. 기존 context schema version과 어떤 관계로 전환할지 및 모든 생산자/소비자 동시 호환은 Owner 결정 대상이다. 다른 대안은 기존 Worker read adapter 패턴의 exact historical lookup이며, 이 경우도 DB 접근·권한 경계와 adapter 계약을 먼저 결정한다.

- 현재 Catalog 재조회, `product_id` 최신 행 추적, 이름 재검색으로 identity를 복원하지 않는다.
- 소비자: `RagRuntimeRepository.list_execution_identification_query_snapshots`, `GuideRuntimeRequestIdentification`, shared identification port, `MedicationIdentityRefResolverPort`, Guide composition.
- 테스트: 같은 identification/medication composite identity, immutable 공식 code tuple, 순서 보존, 누락·중복·UNRESOLVED·교차 version 거부, Catalog 재적재가 과거 identity에 영향 없음.
- 기존 DB의 두 값 재사용 시 schema migration은 불필요할 수 있다. shared carrier/adapter·version 및 계약/통합 테스트 변경은 필요하다.

## 4. CR-C: 승인 verifier와 잔여 안전 권한

`Rag15ApprovalDecisionVerifierPort`를 실제로 구현하려면 승인 decision의 신뢰 원장·issuer·artifact exact 검증·철회/만료 정책을 지정해야 한다. `object()`나 항상 성공하는 verifier를 production bootstrap에 넣지 않는다. 기존 승인 자료가 실제 제공되면 그 exact reader를 구현한다.

Guide Source freshness/scope/철회 evaluator와 typed minimal Patient Context·post-composition safety filter는 별도 미완료 항목이다. 이번 bootstrap 변경이 그 승인을 대신하지 않는다. [callable readiness](../../targets/post-mvp-1/guide-langgraph-callable-readiness-v1.md)를 유지한다.

## 5. 이번에 기존 계약 안에서 구현할 범위

- bootstrap의 기존 opaque dependency/authority-provider 경계에서 누락·호출 불가능한 구성, async가 아닌 resolver와 잘못된 sync factory를 상태 등록 전에 거부한다. 기존 Protocol의 async `resolve`·sync `create`를 검사하며, 초기화 검사가 실제 권한의 적격성을 증명하는 것은 아니다.
- executor factory 구성 실패 시 OpenAI client·DB 정리 경계가 실행되도록 startup resource lifetime을 보완한다. 각 close가 실패해도 나머지 자원의 종료를 시도한다. 외부 credential health check를 추가하지 않는다.
- 모든 public flag와 201 API/DTO/enum/schema·권한 발급·transaction 순서는 유지한다. 자동 factory 선택이나 fake issuer·resolver·승인 우회를 production 코드에 넣지 않는다.
- 실제 OpenAI API는 유효하지 않다고 가정한다. 실제 요청은 0회이며 SDK/Provider 대역으로 구성 실패와 HTTP 401의 기존 fail-closed 의미를 검증한다. 인증 실패를 합성 성공이나 model-only fallback으로 바꾸지 않는다.

## 6. 결정 및 상태 기록

| 요청 | 상태 |
| --- | --- |
| CR-A issuer·snapshot 전달·transaction·수명 | **APPROVED TARGET** — [Decision](../../../governance/decisions/2026-11-04-guide-sync-request-authority.md), [Target](../../targets/post-mvp-1/guide-sync-request-authority-v1.md). CR-A 연결·합성 검증 완료; 실제 정책 연결 및 Current 승격 미수행, Current 아님 |
| CR-B 고정 공식 identity projection/reader | NEEDS_HUMAN_OWNER_DECISION |
| CR-C 승인 원장/verifier·별도 안전 정책 | NEEDS_MORE_EVIDENCE / NEEDS_HUMAN_OWNER_DECISION |

상태는 문서상 계약 관리용이며 앱/DB enum이 아니다. CR-A의 승인과 CR-B/C의 미승인을 구분한다. 구현·통합 증거 없이 CR-A를 Current로 승격하지 않으며, 이번 테스트와 Agent 검토는 merge·배포·공개 승인이 아니다.
