# Source Writer run 최소 권한 — 합성 내부 결정

- Decision ID: `PD-DOSEY-20261105-SOURCE-WRITER-RUN-ACL-1`
- 상태: **OWNER APPROVED / SYNTHETIC INTERNAL CONTRACT FREEZE**. 구현·테스트 증거는 별도 기록한다.
- 승인 근거: Human Owner가 직접 SQL 경계의 필요성과 구현 계획을 요청했고, A안의 권한 축소·잔여 artifact INSERT 경계·B/C 보류를 설명한 계획 보고 후 “이어서 작업해”라고 지시했다. 이를 직전 권고 A안의 합성 내부 구현 진행으로 해석한다. 더 강한 B/C 구조나 운영 적용 승인으로 확대하지 않는다.
- 계획: 로컬 `.omx/plans/plan-source-writer-direct-sql-boundary-2026-11-05.md`.
- 정본: [Source Target](../../contracts/targets/post-mvp-1/rag-source-ingestion-v1.md), [계약 인덱스](../../contracts/README.md).

## 확정한 권한 계약

1. 전용 Source Writer의 `rag_source_ingestion_run` UPDATE는 기존 `knowledge_index_lock_marker` 열로 제한한다. `snapshot_id`, `run_status`, `failure_code`, `failure_message`, `duration_ms`, `finished_at`의 기존 UPDATE 부여를 제거한다. run은 terminal payload를 최초 INSERT에 담는다. 과거 row 수정·새 호환 append/update 포트는 추가하지 않는다.
2. marker는 기존 NOT NULL·검증된 CHECK(`knowledge_index_lock_marker=0`)를 요구한다. Source Writer의 ARTIFACT SnapshotMember provenance 행 잠금을 유지한다. 별도 Knowledge Index Builder의 기존 marker 권한과 origin 잠금도 유지한다. broad run UPDATE를 복원하여 행 잠금 회귀를 해결하지 않는다.
3. 기존 role provisioning의 명시적 revoke/regrant와 marker 검증을 재사용한다. Runtime/Writer의 관리자 권한·role membership/SET ROLE·객체/스키마/DB 소유권 금지, PUBLIC/default/column 우회 정리, Capture publisher·cleanup 분리를 보존한다. 이 변경은 권한 의미 변경이며 단순 내부 리팩터링으로 분류하지 않는다.
4. [기존 원자 발행 결정](2026-11-04-source-run-artifact-close.md)의 새 UUID run·전체 RAW_RESPONSE/REJECTS 원자 저장, caller outer commit/rollback, global artifact → Operation 잠금 순서와 acquisition nonblocking try-lock, NO_CHANGE·실패 receipt·clock을 유지한다. capture v1/v2 exact-read/hash 및 Candidate legacy/runtime 성공 재시도도 변경하지 않는다.

## 보장과 제외

이 결정은 run payload의 직접 SQL 재작성 권한을 줄인다. **Writer의 artifact INSERT 권한은 유지되므로 기존 run에 대한 직접 SQL artifact 추가를 DB가 차단한다는 보장은 없다.** 신뢰된 Python Writer와 격리 credential 경계를 유지한다. 이 잔여 경계를 합성 characterization 테스트에 명시하며 append 차단 테스트로 오기하지 않는다. Runtime·Capture publisher·cleanup에는 Source 발행 권한을 추가하지 않는다.

비신뢰 importer에 credential을 제공하거나 일반 Writer SQL의 artifact 집합 변경까지 차단해야 한다면, 별도 신뢰 publisher로 전체 aggregate 발행을 이전하는 B안부터 재검토한다. permit/membership schema인 C안은 완전성·미사용 permit·issuer·legacy·cleanup 계약과 실험을 거친 별도 결정 대상이다. B/C는 이번 승인에 포함하지 않는다.

Trigger·RLS·업무 DB 함수를 추가하지 않는다. 기존 run marker를 재사용하며 새 schema/migration·DTO/API/enum/hash/key/TTL 변경은 없다. 실제 Source·운영 수치·운영 DB/credential 배포·Source purge/보존 정책·공개 flag/일반 DI·Current 승격·merge/release는 승인하지 않는다. 실제 운영 ACL과 저장소 밖 caller의 사용 여부는 미확인이며 운영 적용 전에 별도 조사한다. metadata 권한은 물리 객체 불변성이나 실제 acquisition 진위의 증명이 아니다.

구현·검증 결과: [합성 권한 경계 검증](../../testing/source-writer-role-boundary-20261105.md). Agent 검토와 테스트는 운영/공개 승인을 대신하지 않는다.
