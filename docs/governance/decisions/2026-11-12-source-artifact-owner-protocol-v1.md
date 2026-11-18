# Source artifact owner — 합성 PUT·verified READ·OUTCOME 프로토콜 동결 v1

- Decision ID: `PD-DOSEY-20261112-SOURCE-ARTIFACT-OWNER-V1-1`
- 날짜: 2026-11-12. 상태: **OWNER APPROVED / SYNTHETIC INTERNAL CONTRACT FREEZE**.
- 선행 결정: [방향 채택 Decision](2026-11-05-source-artifact-owner-read-direction.md) (`PD-DOSEY-20261105-SOURCE-OWNER-READ-DIRECTION-1`), [B1 Decision](2026-11-05-source-trusted-publisher-b1.md) (`PD-DOSEY-20261105-SOURCE-PUBLISHER-B1-1`).
- 정본: [Owner 경계 명세](../../contracts/proposed/post-mvp-1/source-artifact-finalizer-owner-boundary-v1.md), [Source Target](../../contracts/targets/post-mvp-1/rag-source-ingestion-v1.md), [계약 인덱스](../../contracts/README.md).

## 승인된 합성 내부 계약

1. **Wire 프로토콜 (`source-artifact-owner@1`)**:
   - 별도 non-root artifact owner와 비특권 UDS client 사이에 1 connection / 1 operation (`PUT`, `READ`, `OUTCOME`) 단일 줄 UTF-8 JSON 제어 프레임(LF 포함 최대 4096 bytes, 중복 키·미등록 키·`NaN`·boolean 크기·개행 포함 문자열 거부)을 동결한다.
   - `PUT`: caller가 `source-artifact-intent@1` (`event="PREPARED"`) 기록 후 요청 → owner가 인증·한도·기존 attempt 결속을 확인하고 `source-artifact-outcome@1` (`event="ACCEPTED"`)을 fsync한 뒤 `result="ACCEPTED"` 응답 → caller가 `event="ACCEPTED"`(generation 포함)를 fsync한 뒤 정확한 `byte_size` 원문을 송신하고 write-half-close → owner가 길이+1/EOF·SHA-256 검증 및 불변 `LOCAL_PRIVATE` 저장(`0700` dir / `0600` file)·`event="TERMINAL"` (`COMPLETE` 또는 `REJECTED`/`INDETERMINATE`)을 fsync한 뒤 terminal 응답을 반환한다.
   - `READ`: 등록된 publisher/reader peer UID만 허용하며, owner가 동일 fd에서 크기·SHA-256·소유권/모드(`0600`)를 검증한 뒤 `result="COMPLETE"` 헤더와 정확한 바이트를 전송한다.
   - `OUTCOME`: publisher peer UID가 자신이 durable 기록한 필수 `expected_generation`과 `attempt_id`로만 단건 조회한다. `PREPARED_LOCAL`(`ACCEPTED` 미수신) 상태에서는 wire `OUTCOME` 호출 및 generation 탐색을 금지하고 수동 보류한다.
2. **저널 불변성과 실패 의미**:
   - caller 저널(`source-artifact-intent@1`: `PREPARED`, `ACCEPTED`, `OBSERVED`)과 owner 저널(`source-artifact-outcome@1`: `ACCEPTED`, `TERMINAL`)은 분리된 private root에서 `O_EXCL`·`0600`·파일/디렉터리 fsync로 기록한다.
   - owner가 `TERMINAL COMPLETE`를 durable 기록한 뒤 응답이 손실되면 owner `COMPLETE`는 불변으로 유지하고 caller는 `OBSERVED` (`result="INDETERMINATE", reason="CLIENT_TRANSPORT_UNKNOWN"`)를 한 번 기록한다. 이후 알려진 generation으로 `OUTCOME COMPLETE`를 관측해도 기존 caller `OBSERVED`를 덮어쓰거나 중단된 DB 흐름을 자동 재개·재시도하지 않는다.
3. **새 Owner Vault의 Business DELETE 차단**:
   - 새 owner endpoint에는 `DELETE` 연산을 두지 않고, `OwnerBackedSourceArtifactStore`는 트랜잭션 롤백 시 구 cleanup journal 발행을 차단한다(`supports_transaction_cleanup = False`).
   - 기존 `source-artifact-finalize@1` 및 기존 로컬 경로의 cleanup 계약과 코드는 그대로 보존한다.
4. **Rematerialization 일시 Input Spool**:
   - `mfds_product_rematerialization`이 owner-backed reader 및 `spool_root`와 함께 실행될 때, publisher 소유의 배타적 `0700` 작업 디렉터리에 검증된 `0600` 페이지 파일을 기록하고 `page_number -> spool_path -> ManifestArtifact` 매핑으로 canonical/parser 재읽기와 `ExistingProductRawArtifactStore` 검증을 수행한다.
   - 소비자(Product/Label/Rematerialization/Catalog/Knowledge)는 어떤 경우에도 owner vault 경로를 직접 열지 않으며, 일시 spool 파일과 디렉터리는 실행 종료 시(성공·실패·취소) 정리한다.

## 제외 범위

- DB schema/migration, 외부 패키지, DB Trigger/RLS/사용자 정의 함수 변경 없음.
- 운영 수치(용량·시간·보존 기간), 실제 MFDS API 호출, 운영 DB/컨테이너 배포, `PUBLIC_TRACK_F` 공개 게이트 및 Current 승격은 이 결정에 포함하지 않는다.
