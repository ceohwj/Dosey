# Candidate Search 합성 내부 연결 v1

- Decision ID: `PD-DOSEY-20261023-CANDIDATE-SYNTHETIC-1`
- 날짜: 2026-10-23
- 상태: 합성 데이터 내부 구현 범위 확정. Current 승격·실제 Source 이용·공개 승인 아님.
- 결정 근거: Human Owner의 “계획대로 진행”, “합성 데이터 내부 검증부터 진행” 지시.
- 선행: [Bundle binding 결정](2026-10-23-candidate-runtime-bundle-binding.md), [식별 Target](../../contracts/targets/post-mvp-1/medication-identification-v1.md).

## 범위 결정

1. 기존 POST DTO·202 응답을 유지하고 한 transaction에서 DB lexical 검색과 순수 Resolver를 완료한다. POST에도 기존 공개 flag 검사를 도메인 작업 전에 추가한다. 운영 DI에는 합성 context를 설치하지 않는다. flag 기본 false와 외부 공개 차단은 유지한다.
2. 실제 READY LEXICAL_ONLY Index의 UUID에서 `candidate-index-version/<canonical UUID>` artifact를 발급한다. version은 저장된 index_version이다. manifest는 `candidate-index-runtime-binding-v1` domain, Index scalar metadata 및 정렬된 Source refs의 canonical JSON(SHA-256, UTF-8, ensure_ascii=False, allow_nan=False, sort_keys=True, compact separators)이다. scalar 필드는 issuer의 명시적 projection을 정본으로 삼고 테스트로 고정한다. build content_hash·member_set_hash·configuration_hash는 별개의 값으로 포함한다. 소비 시 기존 READY/member/storage/Catalog 검증 후 전체 artifact를 재계산한다. 기존 opaque ref 호환·latest 추정은 하지 않는다.
3. 서버에서 직접 구성한 합성 context만 사용한다. TEST 환경·명시적 Snapshot allowlist·scope/freshness policy hash·governance ref/epoch·Resolver policy와 시각 기간이 모두 필요하다. 실제 Source에 적용할 기본 기간이나 threshold는 없다. 합성 policy에도 release_eligible=False, dense=False를 강제한다.
4. 합성 Source 검증은 실제 DB의 Bundle required/selected Source pins, Index Source refs, Snapshot→Operation→Endpoint→Source 관계, lifecycle/usage/verification, exact PRODUCT_IDENTIFICATION approval와 유효 시각/철회를 읽는다. explicit fixture freshness는 Snapshot verified_at 기준이며 현재 wall clock과 비교한다. 승인/Source/Index를 transaction 종료까지 잠근다. 기한은 approval expiry·freshness deadline·fixture search TTL의 최솟값이며 완료 직전 재평가한다.
5. 이번 receipt는 요청 내부 불변 증거다. user·PVM·operation·Search·Bundle/hash·환경 revision/governance/epoch·Index·query digest·Source pins·policy·관측시각/기한에 결속한다. 기존 REQUEST Parent ref·Guide/Evaluation PASS와 동일한 권한으로 저장하거나 표시하지 않는다.
6. **범위 축소**: production Candidate REQUEST Guard 저장 schema와 정책 FAIL audit-only commit은 만들지 않는다. 합성 내부 검사 실패는 기존 ApiError와 outer rollback을 따른다. 이 결과로 계획 D6/T9의 production 감사 계약이 완료됐다고 주장하지 않는다. 실제 Source freshness/판정 정책, 전 writer lock graph, 영속 Guard/retention/minimum privileges는 후속 결정이다. 새 DB migration·운영 설정 변경은 없다.
7. async hydration 후 불변 evidence를 sync port로 전달한다. 제품 UUID는 정확한 Snapshot+official identity로 찾는다. 합성 matcher는 명시적 함량의 정규화된 문자열 일치만 MATCH, 누락 제품 함량은 UNKNOWN, 불일치는 CONFLICT다. 단위 변환·의료 동등성은 추정하지 않는다. 단일 숫자/단위 사이 공백만 무시하며 숫자 내부·복합식의 토큰 경계는 보존한다. 제형/제조사는 입력 DTO가 없으므로 NOT_APPLICABLE이다. relevance는 실제 lexical stage score로 계산하며 policy threshold는 fixture가 제공한다. 복합제·운영 품질 승인은 별도다.
8. 실제 성분 행의 exact normalized name만 진단에 사용하며 승인 Source subset에 제한한다. INGREDIENT_ONLY는 제품 0개이고 실제 ingredient hit가 있을 때만 허용한다. 내부 점수·Top-K는 기존 DTO에 노출하지 않는다.
9. GET READY와 최초 confirm/reject에 현재 Source/Index/환경을 재검사한다. 기존 성공 idempotency replay는 현재 Guard를 호출하기 전에 그대로 재현한다. legacy/v2 fingerprint·TTL·HMAC·암호화는 변경하지 않는다. 정책 identity 변경은 query_digest를 바꾸므로 새 Search를 요구한다. 기존 Search를 재결속하지 않는다.

## 잠금 순서와 한계

내부 생산자는 owned current Prescription/PVM → active Search → Runtime Environment → READY Index FOR SHARE → 정렬된 Source pin을 순회하며 관련 Source/Endpoint/Operation/Snapshot·Approval을 FOR SHARE로 잠그는 순서를 사용한다. 같은 행의 UPDATE/DELETE는 PostgreSQL 잠금으로 대기한다. 마지막 wall clock 검사는 시간 경과를 별도로 처리한다. 실제 운영의 모든 Source 관리 writer/계정 삭제와의 전체 graph를 인증한 것은 아니며 production activation 조건에 남긴다. 기존 성공 replay는 이 잠금 경로를 거치지 않는다.

## 검증 및 후속 승인

실제 격리 PostgreSQL의 canonical Bundle·READY Index/storage·합성 approval로 POST→GET→confirm/reject→replay와 실패/철회/변경/경쟁을 검증한다. 운영 DB·실제 환자 데이터·외부 Provider는 사용하지 않는다. 자동 테스트는 실제 데이터의 freshness/의료 threshold, 공개·merge/release 승인 근거가 아니다. 최종 실행 증거는 별도 검증 기록에서 연결한다.

## 구현 시 재사용한 상태 계약

기존 Target의 count 불변식을 유지한다. READY/AMBIGUOUS에만 internal evaluated Result를 저장하고, NO_CANDIDATE/INGREDIENT_ONLY/INVALID_INPUT은 Result 0건이다. 실제 비적격 후보가 1개 있어도 NO_CANDIDATE를 빈 Result로 투영한다. 합성 경로는 required/selected CATALOG·CANDIDATE_INDEX_INPUT 목적만 PRODUCT_IDENTIFICATION으로 검증하며 다른 required 목적은 정책 미정으로 차단한다.

## 실제 저장소 검증에 따른 보완

Snapshot seal은 기존 writer대로 `snapshot-state-seal / NO_CHANGE`이며 publication 승인이 아니다. 별도의 `snapshot-current-selection / PASSED`와 effective_at 일치를 확인하고, 제외 행이 있으면 별도 publication 승인도 확인한다. 미래 effective_at은 차단한다. READY 표시/최초 선택은 정확한 제품 UUID·Source·official identity 및 현재 ACTIVE/저장 필드 일치를 잠금 재조회로 검증한다.

물리 trigram top-K는 유사도 0인 저장 행도 반환하므로 합성 adapter는 provenance 검증 뒤 **trigram score가 정확히 0인 행만** 판정 evidence에서 제외한다. 양수이지만 비적격인 제품은 유지하며 INGREDIENT_ONLY로 바꾸지 않는다. 기존 물리 검색과 pure Resolver 계약은 변경하지 않는다. 실제 성분 exact 진단은 이 적용 뒤 제품 evidence가 없을 때만 실행한다.

## 공개 계약 메타데이터와 artifact projection

- POST operation ID는 `medication-candidate.search.create`로 고정하고 404·409 응답을 기존 오류 envelope로 명시한다. 새 요청/응답 필드는 없다. 202는 이번 bounded 경로의 terminal 결과를 포함하며 비동기 Job이 접수됐다는 뜻이 아니다.
- Index artifact의 `index` projection 필드는 `id`, `index_code`, `index_version`, `status`, `build_mode`, `catalog_set_id`, `catalog_version`, `catalog_manifest_hash`, `schema_version`, `normalization_version`, `lexical_config_version`, `search_order_version`, `candidate_limit`, `display_limit`, `embedding_provider`, `embedding_model`, `embedding_model_version`, `embedding_dimension`, `distance_metric`, `member_count`, `product_identity_count`, `product_name_count`, `approved_alias_count`, `vector_count`, `member_set_hash`, `configuration_hash`, `content_hash`다. 최상위 `source_refs`는 `(snapshot_id, source_version)` 순으로 정렬한다. 소비 시 Bundle Catalog version/hash도 실제 Index와 일치해야 한다.
- 제품·성분 행은 관련 `RagEntityIdentity`의 entity type·공식 code system·canonical code와 일치해야 한다. FK의 entity type 일치만으로 이 관계가 입증되지는 않는다.
- [합성 연결 검증 기록](../../testing/candidate-search-synthetic-20261023.md)에 실행 증거와 후속 범위를 기록한다.
