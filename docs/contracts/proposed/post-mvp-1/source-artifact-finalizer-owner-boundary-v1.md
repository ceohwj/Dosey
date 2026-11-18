# Source Artifact finalizer owner boundary v1 — CONTRACT_REQUEST

작성 2026-11-05, 개정 2026-11-12. Task P3a / S1–S5. 상태 **SYNTHETIC INTERNAL CONTRACT FREEZE (`PD-DOSEY-20261112-SOURCE-ARTIFACT-OWNER-V1-1`) / 합성 구현·검증 완료 (Current/공개 미승격)**. 이 문서는 합성 내부 계약 정본이며 운영 준비·공개 승인이 아니다.

Owner는 [저장·읽기 우선 방향](../../../governance/decisions/2026-11-05-source-artifact-owner-read-direction.md)에 이어 [합성 PUT·verified READ·OUTCOME 프로토콜 동결 결정](../../../governance/decisions/2026-11-12-source-artifact-owner-protocol-v1.md) (`PD-DOSEY-20261112-SOURCE-ARTIFACT-OWNER-V1-1`)을 승인했다.

## 권고와 요청 범위

첫 연결은 **합성 전용의 별도 non-root owner에서 artifact PUT + verified READ + 제한된 결과 관측만 제공**한다. 로컬 Unix domain socket(UDS) client는 비특권으로 실행하고 기존 child `env={}`를 유지한다. owner vault의 0700/0600 보호를 유지하며 기존 Python publisher의 전체 DB transaction을 바꾸지 않는다.

**첫 연결에는 DELETE/cleanup endpoint·helper·공통 파일 gate를 추가하지 않는다.** 새 owner vault에 대한 물리 cleanup은 차단된 상태로 둔다. 기존 로컬 경로의 cleanup 계약·코드와 과거 기록은 유지한다. 새 경로의 고아 원본 삭제를 지원한다고 표시하지 않는다. 동시 삭제와 DB 참조 발행을 안전하게 묶는 lifecycle이 아직 증명되지 않았으므로 별도 계약으로 검토한다. 이는 기존 B1의 cleanup 보존 목표를 새 경로에서 충족했다는 주장이 아니라, 새 경로의 명시적 기능 제한 제안이다.

대상은 새로 만든 작업 소유 합성 vault·DB·UID만이다. 실제 Source/Provider/API/S3·운영 DB·credential 발급·운영 적용·공개·Current 승격은 제외한다. DB schema/migration·새 패키지·Trigger/RLS/업무 DB 함수도 제안하지 않는다. [B1 Decision](../../../governance/decisions/2026-11-05-source-trusted-publisher-b1.md)과 [외부 승인 게이트](../../../release-gates/post-mvp-1-external-approvals.md)는 유지한다.

## 현재 코드와 발견된 차이

| 근거 | 현재 동작과 한계 |
|---|---|
| `ai_worker/adapters/local_private_source_artifact_finalizer.py:109` | 고정 명령 실행·응답 검증 후 finalized 등록·실제 bytes 재검증. 응답 손실/timeout이 나면 durable put이 finalized 목록에 없을 수 있다. |
| 같은 파일 `:153` | executable owner·수정 불가 경로를 검사하지만 실행 파일의 소유자가 다르다는 사실이 caller UID를 바꾸지는 않는다. |
| `ai_worker/admin/source_artifact_finalizer.py:26`, `:85` | owner roots·private staging·길이/hash 검사와 저장 코드는 있다. 별도 UID 전달 수단과 독립 절대 입력 상한은 없다. |
| `ai_worker/adapters/local_private_source_artifact_store.py:25`, `:74`, `:123` | owner root/dirs 0700, file 0600·checksum key·불변 link/fsync. 다른 UID의 직접 pathname reader는 RO mount만으로 접근할 수 없다. |
| `ai_worker/admin/mfds_product_rematerialization.py:391`, `:435` 및 `ai_worker/tasks/rag/source_ingestion/mfds_product.py:134` | rematerialization은 owner reader로 읽은 뒤에도 acquisition에 vault pathname을 넣고 canonical checksum에서 파일을 직접 다시 연다. 단순 read_verified adapter 교체로는 보존되지 않는다. |
| `ai_worker/adapters/sqlalchemy_orphan_artifact_references.py:24` | PG global lock은 transaction 종료/DB 연결 상실 시 풀린다. 별도 owner/DELETE 작업이 계속되는 경우의 OS 작업 수명주기를 보장하지 않는다. |
| `ai_worker/tasks/rag/source_cleanup/orphan_artifact.py:184` | 참조 재조회·durable DELETE intent·DELETE UNKNOWN이 있다. PUT 결과 불명 또는 원격 owner inflight 의미로 재사용하지 않는다. |
| `infra/docker/docker-compose.prod.yml:560` 및 [기존 검증](../../../testing/source-publisher-b1-20261105.md) | owner/client가 미제공이며 실제 별도 계정 격리는 미실행. 기존 DB 검증의 finalizer는 대역이다. |

## 읽기와 권한 설계 — 제안

검증한 별도 영속 RO 사본 대신 owner의 bounded READ를 권고한다. 영속 복제본의 별도 원자 발행·보존·삭제 수명주기를 피하고 vault 하나를 유지할 수 있기 때문이다. 코드 구조에 근거한 판단이며 성능 비교 결과는 아니다. 임시 spool이 전혀 필요 없다는 보장은 아니다.

client/owner의 endpoint·namespace·허용 peer UID/GID는 publisher가 변경할 수 없는 고정 설정으로 제공한다. private socket directory DAC와 Linux kernel peer credential을 함께 검증한다. 요청 JSON의 UID/actor만 신뢰하지 않는다. UID namespace/mapping·부모 경로 신뢰를 증명하지 못하면 거부한다. owner의 IP outbound는 차단하고 DB credential/API key를 전달하지 않는다. root/sudo/setuid/Docker socket/helper 권한은 사용하지 않는다. host/root/orchestrator 관리자는 외부 신뢰 경계다.

| 주체 | 허용 연산 | 금지 |
|---|---|---|
| 합성 publisher client | 고정 namespace의 PUT·verified READ·자신의 전송 결과 관측 | DELETE·임의 path/command·SQL·Source fetch |
| 등록된 reader UID | 필요한 namespace의 verified READ | PUT·DELETE·다른 namespace |
| 별도 owner UID | 고정 vault/staging·최소 outcome 기록·socket의 저장/읽기 | DB 접속·Source fetch·IP outbound·발행 object의 business DELETE |
| 기존 cleanup 실행자 | 기존 승인된 로컬 경로만 | 새 owner vault/namespace 접근·새 경로 삭제 |

owner는 PUT과 임시 파일 정리를 위해 vault 쓰기 DAC를 가진다. 따라서 owner가 OS 수준에서 삭제할 수 없다는 보장은 하지 않는다. 보장 대상은 허용 코드·endpoint·배치에서 발행 object의 business DELETE를 제공하지 않는 경계이며 임시 파일 unlink와 구분한다.

새 vault는 기존 cleanup의 root allowlist에 포함하지 않고 기존 cleanup UID가 직접 접근할 수 없도록 한다. owner UID로 구 cleanup CLI를 실행하는 배치를 허용하지 않는다. owner endpoint에는 삭제 연산을 구현하지 않는다. 단순 문서상 금지가 아니라 실제 UID/DAC·config·negative test로 확인해야 한다. 기존 object key 형식/LOCAL_PRIVATE 의미는 유지하되 새 합성 fixture의 물리 root를 분리한다. 새 storage backend enum을 조용히 추가하지 않는다.

추가 조사에서 `ExistingProductRawArtifactStore`는 pathname 동일성도 검사하고(`ai_worker/admin/mfds_product_rematerialization.py:265`), 공통 RawArtifactStore는 파일 경로를 입력으로 받는 것으로 확인했다(`ai_worker/tasks/rag/source_ingestion/mfds_product.py:312`). 따라서 첫 연결은 rematerialization에 한해 **publisher 소유 0700/0600 임시 input spool**을 권고한다. bounded owner READ → caller hash/길이 검증 → exclusive spool 파일 → exact page/path/metadata/key mapping 순서로 구성하고 기존 parser/canonical 재읽기를 유지한다. 기존 object adapter는 그 mapping만 확인해 기존 key를 반환하며 새 PUT을 하지 않는다. 전체 transaction·post-commit 재조회가 끝나면 caller가 만든 transient 파일만 정리하고, crash 잔존은 작업 종료 확인 후 합성 harness가 소유 root만 정리한다. owner vault/outcome은 삭제하지 않는다. 영속 RO 사본은 만들지 않지만 임시 spool 수명주기는 새 내부 계약에 명시한다. Catalog/Knowledge 등 이미 bytes 기반인 소비자는 verified reader 주입을 유지한다. 상세 wire/intent·spool 상태는 로컬 `.omx/plans/plan-finalizer-owner-put-read-protocol-2026-11-05.md`의 검토된 후보이며 아직 계약 freeze가 아니다.

## wire·상한·실패 의미 — 제안

현 `source-artifact-finalize@1`과 구분되는 wire version, namespace·연산·checksum·정확한 byte size·허용 content type·오류 분류를 구현 전에 명세한다. owner는 key를 기존 checksum 형식으로 유도하고 외부 pathname을 받지 않는다. request identity는 전송 관측용이며 기존 Source run_group_key/receipt/Candidate key나 DB 재시도를 대체하지 않는다.

client/owner 각각 독립 크기 상한·정확한 EOF·hash·최대 동시 작업/staging 총량을 검사한다. control reply는 현재 4096 bytes를 유지하는 권고이며 4096+1에서 streaming 중단한다. READ body도 선언/상한+1에서 중단하고 전체 hash/길이 검증 뒤에만 parser로 전달한다. 기존 bytes 반환 port는 bounded memory를 사용하며 무제한 streaming이라고 표현하지 않는다. 합성 fixture 상한은 운영 정책 수치가 아니다.

PUT 전에 caller는 key/hash/size를 결속한 최소 private intent를 남기고 owner는 수락·terminal 결과를 관측 가능하게 기록한다. 기록 형식·fsync·손상/세대 처리·메타데이터 보존은 새 내부 파일 계약으로 명세해야 하며 일반 raw body/secret 로그를 추가하지 않는다. 불변 동일 bytes PUT의 물리 dedup와 Source DB 요청 재시도를 구분한다.

응답 손실·timeout·invalid/oversize response·owner 상태 불명은 성공을 반환하지 않는다. 파일 존재만으로 특정 전송 또는 DB commit 성공을 단정하지 않는다. 새 request key·자동 DB 재발행·자동 삭제는 하지 않는다. 기존 DB 결과·참조·bytes를 확인하는 수동 복구 절차와 연결하고, 확인할 수 없으면 보류한다. 새 경로의 삭제가 닫혀 있으므로 늦은 owner PUT이 기존 참조 원본을 삭제/교체할 수는 없어야 한다. 실제 owner/store가 불변 저장만 수행한다는 검증이 필요하다.

ACCEPTED generation을 durable 기록하지 못한 caller는 wire OUTCOME을 호출하지 않고 수동 조사·보류만 허용한다. generation 자동 발견·null/wildcard·새 PUT 재전송을 추가하지 않는다. owner가 durable TERMINAL COMPLETE를 남긴 뒤 reply가 유실되면 owner COMPLETE는 유지하고 caller만 INDETERMINATE/CLIENT_TRANSPORT_UNKNOWN으로 기록한다. terminal을 덮어쓰거나 두 번 기록하지 않는다. generation이 확인된 이후의 OUTCOME COMPLETE도 artifact 관측이며 DB 자동 재개/재발행 허가가 아니다.

## 수명주기와 잠금 — 첫 연결의 경계

| 상황 | PG/Operation·owner 동작 | 결과와 종료 조건 |
|---|---|---|
| Product/Label 정상 PUT | 기존 caller가 기존 PG global·Operation 순서를 그대로 소유. owner는 DB에 접속하지 않고 불변 PUT/READ만 수행 | bytes 재검증 뒤 기존 전체 DB commit·post-commit 재조회. 부분 commit 없음. 기존 단계별 잠금 시점은 코드/테스트와 대조하여 고정. |
| rematerialization/NO_CHANGE | 기존 global·Operation/Source lock 및 DB transaction 유지. verified bytes 입력으로 재사용 | owner가 직접 DB 성공을 판단하지 않음. canonical/manifest/source-version 검사와 기존 NO_CHANGE 의미 유지. |
| caller 죽음/DB 연결 상실 | 기존 PG lock 해제 가능. 이미 수락된 owner PUT은 늦게 완료될 수 있음 | 성공 추정 없이 intent/outcome으로 추적. 새 vault는 삭제가 닫혀 있고 PUT은 불변. 확인 전 자동 재발행·자동 삭제 없음. |
| owner 재시작·구 worker 생존 | generation/worker lifetime 확인이 되지 않으면 신규 전송/복구 보류 | 구 worker를 종료했다고 단정하지 않음. 같은 key immutable-store 경합과 결과 불명 시험 필요. |
| DB commit 결과 불명 | 기존 실제 DB 참조·receipt 재조회 절차 유지 | owner 응답/파일 존재로 성공 또는 rollback을 추정하지 않음. 삭제·재발행 보류. |
| cleanup 요청 | 새 owner endpoint에 DELETE 없음. legacy cleanup의 새 root 접근도 거부 | 새 원본의 자동/수동 business purge는 미지원. 기존 로컬 경로 삭제는 기존 계약을 유지. |
| 합성 시험 자원 종료 | 모든 task worker/owner가 정지하고 task DB·관련 실행이 종료됐음을 확인 | 작업 소유 전체 fixture root를 정리. 이는 운영 object cleanup 성공 증거가 아님. |

첫 연결은 삭제 capability가 없으므로 새 파일 gate·cleanup helper를 도입하지 않는다. 추후 DELETE를 추가하려면 PG 연결 상실 뒤 살아 있는 DELETE와 새 참조 발행의 경합까지 막는 **단일 lifecycle 명세**가 필요하다. gate 보유 주체·fd 상속·worker 종료·해제 조건·owner generation·DB commit 불명·기존 참조 재사용 경로를 표/시퀀스로 확정하고 경합 시험을 통과해야 한다. UDS close/lease 만료/PG advisory lock만으로 해결됐다고 주장하지 않는다. 별도 cleanup helper가 owner와 같은 UID라면 capability 분리이며 둘 사이 OS 권한 분리라고 표현하지 않는다.

## 영향과 검증

| 소비자/설정 | 필요한 변경과 검증 |
|---|---|
| finalizer caller/owner | 비특권 transport·peer auth·bounded input/output·intent/outcome. 실제 별도 UID의 PUT/READ·sentinel 비노출·변조 경로/미등록 peer/DELETE 거부. |
| Product/Label | `ai_worker/admin/mfds_product_ingestion.py:153`, `mfds_label_writer.py:209`: transport config와 기존 저장/실패 처리 연계. CREATED→NO_CHANGE·FAILED+RAW/REJECTS·Label rollback/기존 재시도·receipt 보존. |
| Catalog/approval | `ai_worker/admin/catalog_writer.py:139`, `catalog_approval.py:642`: read-only identity·verified bytes. 승인 권한/의미 유지. |
| Knowledge/rematerialization/parser | `ai_worker/admin/knowledge_materialization.py:275`, `mfds_product_rematerialization.py:391`, `ai_worker/tasks/rag/source_ingestion/mfds_product.py:138`: owner vault 직접 pathname 재읽기를 임시 input spool/exact mapping으로 연결·canonical 재검증. 실제 distinct UID에서 vault 직접 open 없이 전체 재구성 및 정상/실패/취소 spool 정리 통과. |
| cleanup/config | 새 합성 root/UID/namespace 격리·legacy cleanup 접근 거부. 기존 cleanup contract/DELETE UNKNOWN은 유지. 새 root의 삭제 미지원·고아 원본 보류를 명확히 보고. |
| contracts/tests | 새 Decision·Source Target·wire/file 계약·인덱스·계약/통합 테스트 정렬. B1/기존 Candidate v1/v2 재시도·Bundle/environment 변경 시 새 Search·RET-H 차단·안전 eval 회귀. |
| DB/dependencies | 새 schema/migration/패키지는 제안하지 않음. 필요 발견 시 별도 CONTRACT_REQUEST. 일반 DI/public DTO/enum 변경 없음. |

실패 주입: PUT 직전/직후 kill, durable put 뒤 lost/invalid/oversize reply·timeout, 늦은 PUT, 동일 key 경합, short/extra/상한 초과 bytes, journal 실패, owner 재시작·구 세대 생존, DB 연결 상실/commit 불명. 성공 오인·참조 원본 변조/삭제 0건과 추적 가능한 보류를 입증한다. 기존 재시도 결과가 바뀌면 계약 보존에 실패한 것으로 처리한다.

## Human Owner 결정과 현재 상태

Owner는 **합성 내부 PUT/verified READ/결과 관측 우선 설계와 새 owner root의 삭제 미지원 방향**(`PD-DOSEY-20261105-SOURCE-OWNER-READ-DIRECTION-1`)에 이어, 2026-11-12에 **`source-artifact-owner@1`, `source-artifact-intent@1`, `source-artifact-outcome@1` 세부 프로토콜 동결 및 S1–S5 합성 구현**([Decision](../../../governance/decisions/2026-11-12-source-artifact-owner-protocol-v1.md), `PD-DOSEY-20261112-SOURCE-ARTIFACT-OWNER-V1-1`)을 승인했다.

동결된 프로토콜 요약:
- **Wire schema (`source-artifact-owner@1`)**: 1 UDS connection = 1 operation (`PUT`, `READ`, `OUTCOME`), LF 포함 최대 4096 bytes 제어 프레임, 중복 키·미등록 키·`NaN`·boolean 크기·개행 문자열 거부.
- **Result / Reason 코드**:
  - `result`: `ACCEPTED`, `COMPLETE`, `REJECTED`, `INDETERMINATE`, `NOT_OBSERVED`, `UNKNOWN_GENERATION`
  - `reason`: `AUTH_REJECTED`, `PROTOCOL_REJECTED`, `LIMIT_REJECTED`, `INPUT_MISMATCH`, `IMMUTABLE_OBJECT_MISMATCH`, `IO_RESULT_UNKNOWN`, `ATTEMPT_BINDING_MISMATCH`, `ATTEMPT_IN_PROGRESS`, `OUTCOME_NOT_OBSERVED`, `GENERATION_UNKNOWN`, `JOURNAL_INVALID`, 및 caller 전용 `CLIENT_TRANSPORT_UNKNOWN`
- **Private 저널**: caller `source-artifact-intent@1` (`PREPARED`, `ACCEPTED`, `OBSERVED`) 및 owner `source-artifact-outcome@1` (`ACCEPTED`, `TERMINAL`), 각각 `O_EXCL`·`0600`·파일/디렉터리 fsync.
- **Rematerialization 일시 input spool**: `0700` 배타 작업 디렉터리 아래 `0600` 검증 파일로 staging 후 `ExistingProductRawArtifactStore`가 exact path·metadata·checksum을 검증하고 종료 시 일시 spool만 정리한다.
- **Business DELETE 차단**: 새 owner endpoint에는 `DELETE`가 없으며 `OwnerBackedSourceArtifactStore`는 트랜잭션 롤백 시 legacy cleanup 요청을 발행하지 않는다.

기존 B1 권한·전체 transaction·재시도·공개 차단은 그대로 유지한다. 새 DELETE capability·공통 gate/fencing, 운영 byte/time/concurrency/retention 수치·실제 Source·UID 운영 배치·DB 적용·배포·공개는 이번 승인 범위에서 제외한다.
