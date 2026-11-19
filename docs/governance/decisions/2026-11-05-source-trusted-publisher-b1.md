# Source aggregate publisher B1 — 합성 내부 결정

- Decision ID: `PD-DOSEY-20261105-SOURCE-PUBLISHER-B1-1`
- 상태: **OWNER APPROVED / SYNTHETIC INTERNAL CONTRACT FREEZE**. 구현/검증과 운영/공개 승인은 별개다.
- 승인 근거: Human Owner가 “권고한 B1을 합성 내부 범위로 확정하고 계획대로 구현”, “기존 Writer의 INSERT 권한을 회수하고 별도 publisher가 전체 발행 트랜잭션을 맡도록” 지시했다. Trigger·RLS·업무 DB 함수 금지 및 기존 재시도·안전 규칙·공개 차단을 유지한다.
- 계획: 로컬 `.omx/plans/plan-source-trusted-publisher-b-2026-11-05.md`. [기존 A 결정](2026-11-05-source-writer-run-privileges.md)을 이번 합성 범위에서 확장한다. A의 역사적 승인 기록은 수정하지 않는다.
- 정본: [Source Target](../../contracts/targets/post-mvp-1/rag-source-ingestion-v1.md), [Capture Target](../../contracts/targets/post-mvp-1/candidate-request-guard-audit-v1.md), [계약 인덱스](../../contracts/README.md).

## 권한과 실행 계약

1. 기존 Source Writer에서 `rag_source_ingestion_run`, `rag_source_ingestion_artifact`, `rag_source_snapshot`, `rag_source_snapshot_member`의 INSERT를 회수한다. column/PUBLIC/default/membership 우회도 정리·검증한다. 기존 Snapshot 선택/상태 전이·Verification 감사, hierarchy/Source Use Approval 등 별도 승인된 기존 governance 권한은 이번에 제거하거나 Publisher로 이관하지 않는다. 즉 모든 감사 INSERT를 없애는 변경이 아니다.
2. 별도 Source aggregate publisher login은 Source 8개 표 SELECT/INSERT와 실제 provenance 잠금에 필요한 기존 constrained marker UPDATE만 가진다. marker는 Source/Endpoint/Operation/Member/Run/Artifact의 `knowledge_index_lock_marker`, Snapshot의 `management_lock_marker`를 재사용한다. Source Use Approval 표 쓰기·Catalog/Knowledge 쓰기, 업무 열 UPDATE·metadata DELETE/TRUNCATE는 부여하지 않는다. 관리자/소유권/membership/SET ROLE을 허용하지 않는다.
3. `SOURCE_PUBLISHER_{HOST,PORT,NAME,USER,PASSWORD,ACTOR}`를 전용 수집 설정으로 확정한다. Product·Label·rematerialization config의 기존 `writer` 필드는 `publisher: SourcePublisherConfig`로 이관하며 Writer credential fallback/호환 alias는 없다. 기존 Writer 선택 CLI는 유지한다. 다른 실행 역할은 publisher password 혼입을 거부하고 publisher는 다른 역할 password 혼입을 거부한다. finalizer의 `env={}`와 읽기 전용 원본 경계는 보존한다.
4. publisher가 현재 수집 CLI의 **전체 caller transaction**을 소유한다. Product의 acquisition부터 Snapshot/Verification/run/artifact, Label의 member, rematerialization의 hierarchy를 같은 connection/outer transaction에 유지한다. `persist_complete_attempt`만 별도 connection에 위임하거나 부분 commit·새 큐/RPC/staging을 추가하지 않는다. 기존 내부 savepoint, global artifact → Operation 잠금, nonblocking acquisition try-lock, 실패 원본/NO_CHANGE/receipt/clock을 유지한다.
5. 기존 수집의 새 run_group_key·source-version/canonical 비교 의미를 유지한다. commit 결과 불명·post-commit 검증 실패에서 자동 재시도하지 않는다. 새 request-key 프로토콜·재해시·과거 run/capture backfill을 추가하지 않는다. Candidate legacy/runtime 성공 key·TTL/암호화·GET 감사0·Bundle/environment 변경 시 새 Search 규칙도 유지한다.
6. 별도 capture issuer는 관측만 발행하며 Source publisher와 합치지 않는다. Source commit과 새 acquisition facts를 확인하는 기존 v2 admission 및 v1/v2 exact read/hash를 유지한다. raw replay/과거 run을 live freshness로 올리지 않는다.

Publisher를 구성하는 Source policy는 해당 schema의 PUBLIC table/sequence/column 권한과 owner의 schema default grant도 정리하여 간접 권한을 닫는다. 이는 full provisioning의 기존 PUBLIC reset 범위와 정렬하며 다른 business role의 직접 grant를 회수하지 않는다. Publisher의 실효 권한 read-back은 다른 일반 schema를 통한 추가 접근도 거부하고, 범위 밖 권한을 발견하면 자동 확장하지 않고 실패한다.

## 관리 cutover 계약

기존 job/선택 명령 신규 실행 중지·transaction 종료 확인 → 별도 publisher credential/새 고정 CLI 준비(ingestion OFF) → **T1** Writer 4개 표 INSERT 거부를 별도 관리 transaction으로 commit → **T2** 최종 Writer/publisher 최소 권한을 원자 적용·fresh session read-back → 실제 역할 smoke 후 새 CLI만 재개 순서로 합성 리허설한다. Source 업무 transaction을 T1/T2로 나누는 뜻이 아니다.

T1 실패 시 구 A 권한이 남을 수 있으므로 B 차단 완료를 주장하지 않고 수집을 계속 중지한다. T1 이후 T2/smoke 실패 시 commit된 Writer 거부는 유지하고 roll-forward로 복구한다. 구 이미지/구 policy로 Writer INSERT를 되살리거나 이미 commit된 run/bytes를 삭제·자동 재발행하지 않는다.

`apply_source_role_policy`와 `provision_roles`의 내부 `publisher=None`은 발행자를 구성하지 않는 fail-closed 상태만 뜻한다. 이때도 Writer의 4개 표 INSERT는 거부되며 구 A 권한으로 fallback하지 않는다. 실제 full provisioning 명령은 publisher USER를 요구한다. `--source-writer-deny-only`는 T1 전용이며 추가 publisher grant나 역할 생성·비밀번호 변경을 하지 않는다. 역할 생성은 기존 bootstrap 관리 경계에서만 수행한다.

## 보장과 제외

### 발견한 RET-H legacy Stage1 경계

구 `ret_h_bootstrap_stages` Stage1은 Writer로 Snapshot 생성과 CURRENT seal을 한 transaction에 묶었다. 이 경로는 B1의 INSERT/선택 권한 분리와 충돌한다. Human Owner는 두 선택지 중 “1번”을 선택하여 **B1을 먼저 완료하고 기존 RET-H bootstrap Stage1 CLI를 명시 차단하며, 단계 분리 이관은 별도 계획으로 남기는 것**을 승인했다. Stage1 CLI는 credential 로드·DB 연결·fixture/receipt 쓰기 전에 안전한 오류로 종료한다. 이를 위해 Publisher에 Snapshot 업무 UPDATE를 추가하거나 Writer INSERT를 복원하지 않는다. Stage2 자체와 과거 receipt/data는 변경하지 않는다. 테스트 전용 admin fixture를 지원 운영 경로로 취급하지 않는다.

별도 이관 계획은 로컬 `.omx/plans/plan-ret-h-stage1-b1-migration-deferred-2026-11-05.md`에 미승인 상태로 남긴다. 향후 Stage1의 발행/선택 transaction·CLI 분리 계약이 확정되기 전 legacy CLI를 다시 열지 않는다.

B1은 **기존 Writer credential의 직접 run/artifact 발행을 차단**한다. 신뢰 publisher 자체의 직접 SQL은 여전히 허용 경계다. Snapshot/Verification INSERT 권한으로 CURRENT 초기 상태나 publication-approval 이력을 작성할 수 있으므로, 지원 Python 경로의 PENDING/자동 integrity 규칙을 row별 DB ACL 보장으로 표현하지 않는다. 별도 Source Use Approval 표 쓰기 금지와 Verification의 check_name은 구분한다.

publisher secret·실행 명령·배포 코드에 접근 가능한 주체를 비신뢰 importer로 간주하지 않는다. 같은 역할 이름/클래스만 나누는 것으로 격리를 주장하지 않는다. 설정/실제 DB 역할 검증과 OS/container secret/기동 격리의 실제 검증은 구분하며 미실행은 기록한다. 관리자·host/orchestrator 관리자는 별도 신뢰 경계다.

새 API/DTO/row schema/migration/의존성·Trigger/RLS/업무 DB 함수는 추가하지 않는다. B2 비신뢰 importer/RPC·C permit schema·실제 Source/Provider·운영 DB/credential 발급·Source purge/보존 수치·공개 flag/일반 DI·Current 승격·merge/release는 승인 범위가 아니다. 프로덕션 구성 파일 수정은 계약 정합을 위한 코드 변경이며 실제 운영 적용이 아니다.

구현/검증 증거는 [B1 합성 검증 기록](../../testing/source-publisher-b1-20261105.md)에 남긴다. Agent 검토는 운영/공개 승인을 대신하지 않는다.
