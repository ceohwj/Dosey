# Candidate 정책·원천 재확인·영속 감사 내부 계약 v1

- 상태: **APPROVED INTERNAL SYNTHETIC CONTRACT FREEZE v1**. 2026-11-04 Human Owner가 “권고 계약으로 합성 내부 구현 승인”을 선택했다. 새 두 schema·분리된 발행 역할·FAIL 감사 transaction은 격리 합성 구현/검증에 한해 확정. 실제 Source/수치·운영 DB·공개는 미승인.
- 작성: 2026-11-04
- 선행: [초기 stage 결정](../../../governance/decisions/2026-11-04-candidate-exact-alias-validation.md), [합성 연결 결정](../../../governance/decisions/2026-10-23-candidate-search-synthetic-connection.md), [#807](../../proposed/post-mvp-1/source-use-approval-807.md), [#806](../../proposed/post-mvp-1/request-guard-runtime-binding-v1.md).

## 1. 확정한 내부 범위와 제외 범위

권고는 기존 #713 REQUEST Parent/Source/Member와 #806 per-request instance를 재사용하고, **Candidate 결속/감사 확장 table 한 개**를 추가하는 것이다. 별도 inventory에서 현재 영속 자료는 실제 외부 수집과 raw replay origin을 구분하지 못함을 확인했으므로 **원천 capture 관측 table 한 개**도 추가하도록 권고한다. 원본·page/run·checksum은 기존 표를 재사용하고 아래 3A절의 최소 관측만 추가한다. 두 table과 발행 권한/삭제 계약은 위 Owner 답변으로 합성 내부 범위에서 확정했다. 수치·실제 Source 사용·활성화/공개는 포함하지 않는다.

승인 후에도 초기 구현/DB 테스트는 합성·접근 통제 내부 경로에 한정한다. 실제 runtime policy의 max-age·Search TTL·threshold·retention은 미구성으로 두어 차단한다. 운영 Source 수집·실제 DB migration·Bundle 수동 READY·PUBLIC_TRACK_F 해제는 하지 않는다. 미확정 값에 숫자 기본값을 넣거나 `release_eligible=True`를 허용하지 않는다.

## 2. 재사용과 소비자

| 기존 계약/구현 | 재사용 | 확장 없이 해결되지 않는 점 |
|---|---|---|
| #713 REQUEST Parent/Source/Member | append-only canonical refs와 Source/Member 판정 | Parent ref는 user/op/stage 의미이며 개별 Search/요청 기한을 결속하지 않음 |
| #806 runtime binding | actual request UUID·PASS/FAIL·user/op/env·Bundle·scope의 exact read | PVM/Result·환경 revision/epoch·Index·policy/clock/Source proof 부재 |
| #807 SourceUseApproval | exact Snapshot/Source/version/env/purpose/version·유효시간/철회 | PRODUCT_IDENTIFICATION은 예약 어휘. 실제 issuer·증거·허용 환경의 목적 확장은 별도 Freeze 필요 |
| Candidate Search/Result·Identification | 현재 DTO·count/상태·선택 소유권·최종화 | 새 audit와 원자 저장/삭제 FK 순서 필요 |
| SyncMutationIdempotencyService | 기존 성공 2xx replay·savepoint·승자 재조회 | mutation 안에서 던진 정책 FAIL이 audit까지 rollback하는 문제는 Candidate 바깥 coordinator로 처리 |

Backend Candidate coordinator·Repository·역할 provisioning·account cleanup·관련 계약/PG suite가 소비자다. Guide/Chat/Evaluation reader와 기존 DTO·enum·멱등 지문에는 새 의미를 주지 않는다.

## 3. Candidate extension의 구체적 최소 필드

제안 table명 `rag_candidate_request_guard_audit`. 새 모델/repo·forward migration은 이번 합성 내부 승인 범위로 작성하고 격리 test DB에서만 적용한다.

| 필드 | 제안 의미/제약 |
|---|---|
| request_guard_decision_id | UUID PK·#806 request_guard_decision_id FK RESTRICT. 실제 evaluator가 요청마다 발급하며 caller가 선택하지 않음 |
| artifact_version / artifact_content_sha256 | `candidate-request-guard-audit-v1` projection identity. SHA-256 lowercase hex; semantic payload 재계산 exact read |
| user_id / prescription_version_id / prescription_version_medication_id | 실제 user/PV/PVM FK RESTRICT. repository가 SELF 소유 parent와 PV/PVM 관계, #806 user와 일치 검증 |
| request_operation_code | 기존 `medication-candidate.search.create`, `medication-candidate.confirm`, `medication-candidate.reject`만. #806과 exact-match |
| search_id / candidate_search_result_id | nullable FK RESTRICT. create PASS는 Search 필요, create FAIL은 Search 없어도 됨. 최초 confirm/reject는 둘 다 필요하고 실제 Result→Search→PVM 관계 검증 |
| runtime_environment_id / environment_revision / governance_revision_ref / safety_epoch | 실제 환경 ID FK, 양수 revision/epoch, 검증 당시 governance pin. #806 환경/Bundle과 일치하는 현재성 판정 근거 |
| candidate_index_version_id / candidate_index_artifact_sha256 | 실제 Index UUID FK와 issuer artifact digest. Bundle의 exact ref/version/hash·Catalog/Source 정합과 일치 |
| query_digest / resolver_policy_sha256 / retention_policy_sha256 | keyed query digest와 승인 정책 content refs. 원문 이름/함량/키는 저장하지 않음. retention 정책은 미승인 시 실제 데이터 경로 차단 |
| proof_manifest / proof_manifest_sha256 | allowlist JSONB와 canonical hash. 아래 Source/Member/정책 관측만 담고 크기 상한을 계약 테스트로 고정 |
| checked_at / valid_until / outcome / reason_codes | aware timestamp, PASS/FAIL, 닫힌 내부 reason 집합. PASS는 미래 valid_until·실패사유0; FAIL은 과거/없는 deadline을 고정 사유와 함께 표현 가능 |
| created_at | DB 생성 시각. freshness anchor 또는 승인 시각으로 사용하지 않음 |

#806에 이미 저장하는 Bundle ID/hash·environment code·legacy Parent ref는 extension에 불필요하게 중복 저장하지 않고 exact-read 후 canonical projection에 포함한다. FK 하나가 다른 필드 관계를 증명한다고 보지 않는다. 동일 instance ID의 같은 semantic payload는 재시도 가능하며 다른 payload는 거부한다. 새로운 과거 Search backfill·기존 성공 키 rehash·공통 모델의 필드 의미 변경은 없다.

## 3A. 원천 capture 관측의 구체적 델타

제안 table명 `rag_source_capture_observation`. 기존 Snapshot 시각/내용, ingestion run, artifact 이력을 덮어쓰거나 기존 행을 LIVE로 backfill하지 않는다.

| 필드 | 제안 의미/제약 |
|---|---|
| capture_observation_id | trusted capture coordinator가 발급한 UUID PK. caller/일반 replay 입력에서 생성한 ID를 LIVE 권한으로 받지 않음 |
| ingestion_run_id | 기존 run FK RESTRICT·UNIQUE. 실패·미완료·잘못된 operation run과 결속 금지 |
| source_snapshot_id / source_version / canonical_checksum | 검증된 기존 Snapshot과 정확히 결속. NO_CHANGE도 그 run이 대조한 실제 Snapshot/hash여야 함 |
| source_code / endpoint_code / operation_code | frozen stable operation identity와 exact-match |
| capture_protocol_version / issuer_ref / request_profile_sha256 | 발행 구현·역할/관리 증거. 새 profile/version으로만 변경 |
| capture_origin | 고정 `LIVE_PROVIDER`. replay의 새 이름이 아님. replay row는 이 표에 발행하지 않으며 관측 부재를 LIVE로 추정하지 않음 |
| capture_started_at / capture_completed_at / recorded_at | trusted coordinator의 aware UTC 시각과 DB 기록 시각을 구별. 시작≤완료≤기록을 검증하며 단순 DB INSERT 시각을 capture 시각으로 사용하지 않음 |
| raw_manifest_sha256 / endpoint_contract_receipt_sha256 | 해당 run의 raw artifact manifest 재계산 값과 static endpoint 계약 digest. static 계약 receipt를 개별 network capture로 취급하지 않음 |
| full_scan_completed / primary_key_validation_passed / page_count / record_count | full success·PK 검증·실제 run/artifact 집합/건수와 일치. 일부 page·실패/중단·empty policy 위반이면 발행0 |
| observation_content_sha256 | 고정 projection의 canonical digest. exact-read 때 재계산. 임의 raw data/body/URL/자격은 넣지 않음 |

현재 `SnapshotIngestionMetadata`는 caller 시각의 timezone/순서만 검사하고 NO_CHANGE 관측에 그 `finished_at`을 사용한다. 따라서 새 field를 채우는 것만으로 신뢰성이 생기지 않는다. `acquire_mfds_product`의 실제 fetch/receipt 검증 경로에서만 발행하고, 원본 재처리 importer는 해당 권한을 가지지 않아야 한다. [metadata](../../../../ai_worker/tasks/rag/source_ingestion/snapshot_lifecycle.py:333), [capture](../../../../ai_worker/tasks/rag/source_ingestion/mfds_product.py:83), [NO_CHANGE](../../../../ai_worker/tasks/rag/source_ingestion/snapshot_lifecycle.py:779).

권고 발행 경계는 기존 general Source/replay writer와 구분되는 **capture 관측 INSERT/SELECT만 가능한 제한 writer 역할**이다. 일반 Source writer·Backend·AI Worker reader는 LIVE 관측을 INSERT/UPDATE/DELETE할 수 없다. 같은 credential을 공유하는 Python class 이름/`origin=LIVE_PROVIDER` 필드·정적 receipt만으로 이 분리를 만족했다고 보고하지 않는다. 신규 역할/자격을 실제 환경에 발급하는 것은 이 코드/합성 DB 검증 승인과 별개이며, 발행 경계를 구성하지 못하면 freshness는 계속 UNKNOWN/차단이다.

각 run은 이미 보존하는 raw artifact metadata에서 manifest를 재계산하므로 Snapshot 최초 raw manifest와 NO_CHANGE run의 raw manifest가 달라도 canonical 내용이 같은 경우를 올바르게 구분한다. 외부 원천의 시점 일관성·허용 capture 지연/갱신 cadence는 별도 Source profile 결정이며 이 표만으로 승인하지 않는다.

## 4. proof manifest와 권한 발행

Manifest는 `projection_version`, 실제 #806 reference와 scope hash, 정책 refs, 정렬된 target 목록/aggregate를 포함한다. target은 exact Source/Endpoint/Operation·Snapshot/version/checksum·required/selected·PRODUCT_IDENTIFICATION approval ID/version/기간/철회 관측·freshness/scope policy ref·capture observation ref·당시 lifecycle/verification 판정·Source/Member Decision refs·고정 outcome/reason/관측 시각이다.

Bundle/Index에서 서버가 expected target 집합을 도출하고 누락/추가/중복·미평가 required target·개별 결과와 aggregate/#806 outcome 불일치를 거부한다. 실제 Source Snapshot Member 대응이 필요한데 없으면 임의 Member ID/locator를 생성하거나 검사 생략으로 보충하지 않는다. 그 경우 새 mapping 계약 근거를 확보할 때까지 실행을 차단한다.

v1 canonical recipe는 UTF-8·ensure_ascii=False·sort_keys=True·compact separators·allow_nan=False다. 문자열은 NFC 검증 후 그대로 사용하고 UUID는 canonical lowercase 문자열, 시각은 UTC `YYYY-MM-DDTHH:mm:ss.ffffffZ`, 없는 optional 값은 명시적 null로 표현한다. source target은 `(source_code, endpoint_code, operation_code, snapshot_id, purpose)` UTF-8 tuple로 정렬하고 중복은 거부한다. member는 실제 kind/parent snapshot/member ID로 정렬하며 proof에는 ID·content hash·Decision ref를 결속하고 자유 locator/원문을 복제하지 않는다. reason은 중복 없는 UTF-8 정렬 배열이다. canonical proof manifest 크기는 기술적 상한 **65,536 bytes**로 제안하며 초과 시 truncate하지 않고 원자 저장을 거부한다. 이 상한은 의료적 freshness/threshold 수치가 아니다. 임상 본문·약명/함량·OCR 원문·raw idempotency key·자격·암호화 응답 복제는 금지한다. user/query digest/Source 접근 이력도 계정 연결 metadata로 취급한다.

정책 package의 자체 `approved=true`는 권한이 아니다. Owner의 별도 Decision과 deployment 승인 digest manifest를 exact-reference하고 Source 목적·환경·기간·Hash를 검사한다. package registry는 기본 empty/deny로 시작하며 승인되지 않은 실제 Source 기간/threshold 예제 파일을 포함하지 않는다. 목적 확장·외부 Source/Privacy 승인 전에는 실제 사용 권한을 발급하지 않는다.

### v1 Source/Member proof 형식과 reason

Manifest 최상위 key는 `projection_version`, `runtime_binding_ref`, `scope_manifest_hash`, `resolver_policy_ref`, `retention_policy_ref`, `targets`, `aggregate_outcome`으로 닫는다. 각 source target은 `source`(stable operation·Snapshot/version/checksum·purpose·required/selected), `approval`(ID/version/env/purpose·valid_from/expires_at/revoked_at), `freshness`(policy ref·capture observation ID/hash·anchor/deadline), `scope_policy_ref`, `source_decision_ref`, `member_decisions`, `outcome`, `reason_codes`를 갖는다. 자유 query·약명·raw body·credential·URL을 허용하지 않는다. Member 대응/평가 집합을 재현하지 못하면 complete observation이 아니므로 구조 오류 경계로 차단한다.

정책 FAIL의 v1 reason vocabulary 제안은 `SOURCE_NOT_ELIGIBLE`, `APPROVAL_NOT_YET_VALID`, `APPROVAL_EXPIRED`, `APPROVAL_REVOKED`, `FRESHNESS_EXPIRED`, `SCOPE_DENIED`, `POLICY_EXPIRED`, `PRODUCT_NOT_ACTIVE`다. 각각 known facts의 판정에만 사용한다. 정책 본문/승인 권한·capture provenance 자체 부재, hash/FK/필드 변조·미정 목적·unknown reason은 이 enum으로 임의 변환하지 않고 unavailable/rollback 경계로 처리한다. 이 reason은 새 공개 status/code가 아니라 내부 감사 계약이다.

## 5. transaction 계약

- **Create PASS:** 현재 소유 처방/Search→환경→Index→Source/approval/capture 잠금·판정→Resolver→최종 clock 판정→Search/Result+#713/#806/extension 같은 outer commit. 부분 READY/PASS/RUNNING commit 없음.
- **Confirm/reject PASS:** 역사적 소유권/관계→기존 성공 replay→mutation savepoint에서 현재 Guard·audit·Identification·성공 snapshot 원자 저장. 공통 멱등 `2xx만 저장`과 unique/도메인 오류 후 승자 조회 보존.
- **정책 FAIL:** 완전한 evaluator denial만 Candidate typed 결과로 전달. mutation savepoint rollback과 공통 승자 재조회 이후 실제 denial이 남은 경우 같은 outer session에서 parent 소유권을 다시 잠금 검증하고 historical FAIL만 저장, route는 고정 오류 body를 정상 return해 commit. 별도 session·독립 commit·전역 commit-on-error 없음.
- **구조 불명/정책 본문 부재·잘못된 hash·저장/DB 오류·취소:** 전체 rollback. 가짜 PASS/의미가 불완전한 FAIL을 생성하지 않음. parent가 삭제/타인/불일치면 404 우선, audit/의료 쓰기0.
- **GET:** 현재성 재검증만, 새 audit/권한 쓰기0.
- **성공 replay:** 최신 Guard/정책 reader·새 audit 쓰기0. 기존 status/body/HMAC/hash/ciphertext/TTL 불변. 새 audit ID를 멱등 fingerprint에 넣지 않음.

### FAIL savepoint rollback 이후의 정확한 write set

1. evaluator가 rollback 전에 immutable denial envelope를 만든다. envelope에는 원래 발급한 instance UUID·checked_at·user/PVM/operation·Bundle/env/Index/scope·policy refs·Source/Member 전체 판정 payload와 그 canonical refs·Candidate manifest/hash가 모두 들어간다. ORM live object나 호출자 자유 JSON이 아니라 순수 검증된 값이며, 새로운 시각/ID로 재발급하지 않는다.
2. typed `CandidateGuardPolicyDenied`만 경계로 사용한다. 공통 execute가 nested rollback 후 이미 저장된 성공 승자를 재조회해 반환하면 그대로 replay하며 FAIL 쓰기0이다. 다른 ApiError·fingerprint conflict·DB 오류를 typed denial로 잡아 바꾸지 않는다.
3. 실제 typed denial이 outer Candidate coordinator까지 전달되면 savepoint의 Search/Result/Identification/성공 key 쓰기가 rollback됐는지 확인한다. 같은 outer session에서 기존 순서로 부모 소유권/관계를 다시 잠금 검증한다. 소유관계가 사라졌거나 FK 대상이 정리됐으면 기존 404/안전한 rollback으로 끝내며 거짓 audit를 만들지 않는다.
4. 당시 checked_at의 historical envelope를 exact-validate하고 한 savepoint 안에서 **(a) #713 Guard Parent → (b) 해당 Source/Member Decision 전체 → (c) 원래 instance UUID/outcome=FAIL의 #806 binding → (d) Candidate extension**을 함께 기록한다. 이미 존재하는 immutable semantic #713 row는 같을 때만 재사용하고 서로 다르면 전량 rollback한다. mutation savepoint에서 생성됐다가 지워진 row도 이 write set으로 다시 저장한다.
5. #713 targets/aggregate·#806 user/op/stage/env/Bundle/scope/FAIL·extension provenance/hash가 모두 일치해야 한다. 현재 Source 상태를 대신 끼워 과거 denial을 새 PASS/FAIL로 바꾸지 않는다. FAIL은 현재 사용 권한이 아니며 재시도 성공 receipt가 아니다. 부분/불완전 envelope·권한/저장 실패는 (a)~(d)와 domain을 모두 rollback한다.
6. 전체 write set의 flush/검증 후 coordinator가 고정 오류 DTO stop-result를 반환한다. route가 정상 return하면 기존 outer session이 audit만 commit한다. 별도 DB session이나 service 내부 commit을 추가하지 않는다. response 송신/commit 실패에 대해 audit commit을 보장한다고 보고하지 않는다.

### Capture 발행의 testable protocol

1. trusted live coordinator가 frozen endpoint/request profile로 network fetch를 직접 수행하기 직전에 capture UUID와 시작 시각을 발급한다. 임의 SourceRunResult·기존 raw bytes·외부 caller가 만든 completed_at를 LIVE 발행 API 입력으로 받지 않는다. 운영 clock/endpoint profile이 미승인이라면 발행 경로는 미구성이다.
2. 실제 fetch 완료와 모든 페이지/body/PK/manifest 검증 후 그 capture의 완료 시각을 고정한다. `ingest_and_persist`에 별도로 들어온 metadata 시각과 acquisition identity/시각/endpoint receipt를 대조하고 불일치이면 capture 관측0이다. 재수집 시 새 ID를 발급하며 raw replay는 이 coordinator 경로를 사용하지 않는다.
3. 기존 Source writer transaction으로 run/artifact/Snapshot을 commit한다. capture publisher는 별도 제한 credential로 **이미 commit된 그 run**의 operation/상태/실제 artifact 집합/원본 manifest/canonical Snapshot/endpoint digest를 잠금 재조회해 private live acquisition 관측과 대조한 뒤 관측 한 건만 저장한다. 이 전후 간격에서는 아직 관측이 없어 freshness UNKNOWN으로 차단된다. Source Snapshot 활성화를 이 관측이 대신하지 않는다.
4. 일반 replay/Source writer credential은 capture table INSERT 권한을 갖지 않고, capture publisher는 필요한 Source metadata SELECT와 capture table INSERT/SELECT만 가진다. publisher credential을 importer에 넘기거나 동일 login으로 양 역할을 자유 전환시키지 않는다. 같은 DB privilege를 공유하는 Python 객체 구분은 권한 분리의 대용이 아니다.
5. 최초 Source commit 뒤 publisher 실패/프로세스 소실이면 fresh receipt를 추정 복원하지 않고 다음 완전 network capture를 요구한다. 관측 INSERT 재시도는 같은 original ID/semantic payload일 때만 허용한다. writer 자신이 거짓 network 이벤트를 발행하는 위협까지 DB schema로 방지한다고 주장하지 않으며, credential·trusted issuer 운영 경계의 승인이 실제 사용 선행 조건이다.

## 6. 삭제·ACL·migration

권고는 계정 연결 Candidate audit를 계정 삭제 범위에 포함하는 것이다. 별도 탈퇴 후 보존을 신설하지 않는다. active 계정의 실제 보존 기간/일괄 purge·legal hold는 수치·정책 승인 전 미구성으로 둔다. Job 90일을 복사하지 않는다.

삭제 순서는 Candidate extension과 그 전용 #806 instance, 실제 consumer가 없는 Candidate Source/Member/Parent refs → 기존 Candidate Result/Identification/Search 정리 순서를 검증한다. Guide/Evaluation 공유 row를 일괄 삭제하지 않고 live key/사용중 참조가 있으면 일반 만료 purge로 자르지 않는다. 계정 삭제의 인증 차단·의료 데이터 삭제와 재시도 예외는 기존 계약을 따른다. Source 승인 actor가 탈퇴 user를 참조하는 별도 FK는 이 요청에서 임의로 삭제/익명화하지 않고 기존 거버넌스 경계로 남긴다.

Backend는 필요한 SELECT/INSERT만, Source writer는 기존 승인 철회 제한 필드만, cleanup 역할은 정확한 표 DELETE만 가진다. FOR SHARE에 필요한 권한을 실제 역할로 검증하며 approval 등 관측 table에 최소 lock-marker가 필요하면 해당 한 열/제약/권한의 별도 forward delta로 명시한다. broad UPDATE/DELETE·superuser·trigger/RLS/업무 DB 함수는 추가하지 않는다.

Source capture 관측은 사용자 Candidate 감사와 달리 Source ingest provenance다. active Bundle·Search·감사/원본 consumer가 참조하는 동안 유지하고, Source 수명주기 cleanup이 해당 관측과 종속 run의 정리 순서를 별도로 검사해야 한다. 계정 삭제가 다른 사용자가 쓰는 Source 관측을 지우지 않는다. 운영 capture 보존/원본 cleanup 정책이 미확정이면 실제 데이터 purge를 활성화하지 않는다.

이번 v1 승인 요청의 retention 범위는 **합성 검증 데이터만**이며 테스트 schema 정리로 제거한다. active 실제 계정의 숫자 TTL/legal hold나 실제 환경 DB 역할·자격 발급은 별도 결정 전까지 활성화하지 않는다. 이 제한 때문에 아래 schema가 승인돼도 실제 Source 사용 승인이 되지 않는다.

신규 forward migration의 parent는 실행 시 실제 single head를 확인한 뒤 정한다. downgrade는 데이터가 있으면 손실을 거부한다. 기존 적용 migration·legacy Search·기존 key 데이터를 수정하지 않는다. 운영 DB에는 적용하지 않고 격리 test DB에서만 검증한다.

## 7. 수치 정책과 검증 기준

수치 max-age/cadence/승인 TTL·운영 relevance/margin/weights·최소 표본/허용 오류·retention 숫자는 미정이며 이 계약 요청에 포함하지 않는다. 별도 source/평가 보고서 후 결정한다. 잘못된 Exact/Alias 공식 identity·함량은 threshold 증가로 해결할 수 없으므로 identity·상태·속성·별칭 증거 hard gate를 유지한다. Exact 결과 LIMIT의 완전성/overflow도 평가하고 불완전하면 READY를 차단하는 내부 evidence 확장을 추가 검토한다.

승인 후 필요한 증거는 실 PostgreSQL PASS 원자성·FAIL만 commit·DB/cancel rollback·현재 GET/replay 쓰기0·두 세션 revoke/ABA/처방/탈퇴 경쟁·실제 역할 ACL·cleanup/refcount·legacy/v2/B/C 회귀·문서/계약/index 정합이다. snapshot/property를 실제 validator 대신 stub한 사례만으로 완료 판정하지 않는다. 이번 Owner 승인은 합성 내부 Freeze이며 실제 Source 정책·운영·공개와 Current 승격은 별도 결정이다.

## 8. 이번 조사에서 발견한 실제 Source 선행 조건

공식 문서는 현재 Service08을 안내하지만 저장소 제품 receipt는 2026-09-05 Service07 실측이다. endpoint 계약 버전 차이·현재 필드/PK/pagination/전체 capture 의미를 별도 검증하기 전 실제 Source profile을 활성화하지 않는다. Source07 이력/기존 Bundle·Search·키는 덮어쓰지 않는다. [조사 근거와 한계](../../../testing/candidate-policy-evidence-20261104.md#4-공식-source-문서-근거-2026-11-04-열람)를 따른다. 이 DB/감사 계약 요청에 실제 Service08 전환이나 Provider 호출 승인은 포함하지 않는다.

## 9. 기계 판독 projection과 hash vector

[동일 합성 내부 Contract Freeze 상태의 JSON Schema](candidate-request-guard-audit-v1.schema.json)가 proof manifest·extension semantic projection·capture projection의 정확한 nested key/type/null/enum을 명시한다. [합성 hash vector](candidate-request-guard-audit-v1.hash-vector.json)는 serialization recipe만 고정하며 가짜 refs/예시 시각은 승인·원천 capture·실행 가능한 정책이 아니다. proof에는 lifecycle 상태와 Snapshot seal/selection/publication digest도 포함한다. 실제 Member 집합의 완전성, Source/approval/Bundle 관계·시간 비교, ref/hash·outcome 집계·NFC·byte cap은 JSON Schema만으로 충족되지 않으며 repository/evaluator가 추가 검증한다.

extension content digest는 Schema의 `extension_projection` 전체이며 DB의 artifact digest/created_at는 제외한다. proof digest와 원래 #806 ref를 포함하므로 transitive binding을 재계산해야 한다. capture observation digest는 `capture_projection` 전체이고 그 digest 자체를 입력에 넣지 않는다. 재시도는 recorded_at를 새로 찍어 의미를 바꾸지 않고 원래 관측 전체를 재현한다. schema의 환경 enum 지원은 실제 환경 활성화 권한이 아니다.

## 10. 합성 구현의 발행 경계와 남은 제한

`record()`는 Source metadata에 쓰기 권한을 주지 않으며, caller가 시작한 REPEATABLE READ 또는 SERIALIZABLE transaction의 일관된 읽기에서 저장한다. READ COMMITTED에서는 저장하지 않는다. 이 isolation 요구는 Source 행 잠금을 위한 UPDATE 권한을 추가하지 않기 위한 저장 구현 조건이다. 실제 발행 자격으로 committed 합성 run을 읽고 관측을 저장하는 검증과 별도 reader 검증을 수행한다. 실제 trusted live capture coordinator와 credential 배포는 연결하지 않는다.

**v1 당시의 차단 사유와 후속 범위:** 일관된 읽기만으로 terminal run의 후속 artifact INSERT를 금지하지 못한다. 11절의 Owner 승인 후 Python Writer의 기존-run append 포트를 제거하고 새 run/전체 artifact 원자 저장·cleanup 참여를 합성 두 세션으로 검증했다. 이는 지원 Python 경로의 보장이며 직접 SQL INSERT 권한자의 우회·물리 원본·운영 issuer/freshness 정책을 자동 해결한 것이 아니다. capture reader의 Source metadata SELECT 경계와 실제 freshness 사용 차단은 유지하며 Source 쓰기 권한이나 schema를 확대하지 않는다.

영속 감사는 완전한 evaluator envelope를 저장하는 내부 부품이다. 실제 수치 정책 registry/evaluator, 새 Source freshness Guard와 일반 Candidate DI/API의 자동 연결은 미구성이며, 기존 합성 Guard를 운영 권한으로 승격하지 않는다. 공개 flag/기본 미구성 차단은 유지한다.


## 11. Source 원자 발행 연계 — 합성 내부 개정 (2026-11-04)

[Owner 승인 Decision](../../../governance/decisions/2026-11-04-source-run-artifact-close.md)에 따라 신뢰된 Source 발행 Python 경로의 새 complete run·전체 artifact 원자 저장과 기존 run append API 제거를 합성 내부 범위로 구현한다. 신규 합성 admission protocol은 `v2`와 검토된 issuer code/version/content hash, 같은 새 private acquisition의 새 run을 요구한다. v2라는 문자열/일반 caller metadata만으로 권한을 발행하지 않으며 raw replay·과거 run을 새 live 관측으로 올리지 않는다.

기존 `v1` capture 및 issuer ref version `1.0`의 역사적 exact-read·hash·감사 재시도는 유지한다. capture 허용 run 상태는 SUCCEEDED/NO_CHANGE뿐이며 SUCCEEDED_WITH_REJECTIONS/FAILED로 확대하지 않는다. RR/SERIALIZABLE 요구·별도 issuer의 Source metadata SELECT-only·기존 fields/hash recipe는 유지한다. 운영 issuer/수치 정책·실제 Source freshness/공개는 계속 미구성이며, 10절의 직접 SQL 우회·물리 원본·운영 검증 한계가 자동 해소된 것으로 보지 않는다. Source 원자 API·잠금 경합의 실제 합성 검사 결과는 별도 evidence를 따른다.

[Source 원자 발행 합성 검증](../../../testing/source-run-artifact-close-20261104.md): 새 run/artifact 원자성·실제 역할·발행/cleanup/capture 경합과 기존 재시도를 검증했다. 실제 Source·공개/Current 승격 증거로 사용하지 않는다.

## 12. Source aggregate publisher B1 연계 — 합성 내부 (2026-11-05)

[Owner B1 Decision](../../../governance/decisions/2026-11-05-source-trusted-publisher-b1.md)에 따라 Source aggregate INSERT는 기존 Writer에서 별도 publisher login으로 이관한다. 이 publisher는 **capture issuer가 아니다**. 기존 Writer와 Source publisher 모두 capture table INSERT를 받지 않으며, capture issuer는 Source metadata INSERT를 받지 않는다. Source publisher가 전체 Source outer transaction을 commit한 뒤 별도 issuer가 기존 acquisition facts·run/manifest와 대조하는 순서는 유지한다.

신규 Source credential/config 이름은 capture DTO/issuer ref/observation hash를 바꾸지 않는다. v1/v2 exact read·같은 ID/semantic payload 관측 재시도·Candidate 성공 재시도·GET 감사0·commit 이후 freshness gap을 유지하며 raw replay를 live로 승격하지 않는다. Source publisher 직접 SQL의 잔여 권한과 실제 credential/process 격리는 Source B1 계약의 신뢰 경계다. 실제 Source·운영 수치/credential·일반 DI·공개/Current 승격 승인이 아니다.
