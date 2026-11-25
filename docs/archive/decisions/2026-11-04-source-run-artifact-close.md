# Source run·artifact 원자 저장 — 합성 내부 결정

- Decision ID: `PD-DOSEY-20261104-SOURCE-ARTIFACT-CLOSE-1`
- 상태: **OWNER APPROVED / SYNTHETIC INTERNAL CONTRACT FREEZE**.
- 승인: 2026-11-04 Human Owner가 “권고한 계약 변경을 합성 내부 범위로 확정하고 계획대로 구현”, “Python Writer에서 새 run과 전체 artifact를 원자 저장하고 기존 run 추가 쓰기는 막아줘”, “직접 SQL 차단은 별도 검토”를 지시했다.
- 계획: 로컬 `.omx/plans/plan-source-run-artifact-close-2026-11-04.md`의 C1–C6.
- 정본 반영: [Source Target](../../contracts/targets/post-mvp-1/rag-source-ingestion-v1.md), [합성 capture Target](../../contracts/targets/post-mvp-1/candidate-request-guard-audit-v1.md), [계약 인덱스](../../contracts/README.md). Current 승격 아님.

## 확정한 계약

1. 지원 Python Writer는 새 UUID의 terminal run과 해당 attempt의 전체 RAW_RESPONSE·REJECTS를 `persist_complete_attempt(record, artifacts) -> UUID`로 저장한다. 내부 savepoint는 run/artifact 부분 저장을 막고 outer commit/전체 Source aggregate rollback은 기존 caller가 소유한다. 기존 run ID를 받아 뒤늦게 원본을 추가하는 지원 API·호환 wrapper는 두지 않는다.
2. 완료 경계는 전체 attempt의 outer commit이다. 기존 terminal enum·실패 코드·receipt·NO_CHANGE 의미와 시각은 유지한다. `finished_at`/capture clock을 DB commit 시각으로 대체하지 않는다. 실패 원본도 최초 저장에 포함하며 duplicate attempt를 append/upsert로 처리하지 않는다.
3. 기존 artifact mutation advisory transaction lock을 permanent object finalization부터 DB 참조 commit까지 재사용한다. blocking 순서는 global artifact → Operation이다. acquisition의 기존 nonblocking try-lock은 유지한다. Source gate 선행 generic/detail은 global 전 Operation 잠금/permanent put을 하지 않고, global 보유 경로는 Source gate를 blocking 대기로 바꾸지 않는다. label·product·generic·detail·failure·rematerialization·cleanup의 모든 지원 발행 경로를 검사한다.
4. 신규 합성 capture admission은 `capture_protocol_version="v2"` 및 검토된 issuer code/version/content hash, 새 private acquisition의 새 complete run을 요구한다. 문자열만으로 발행 자격을 주지 않는다. 현재 허용 상태 SUCCEEDED/NO_CHANGE를 유지하고 SUCCEEDED_WITH_REJECTIONS/FAILED·raw replay·과거 run backfill은 신규 freshness 증명이 아니다. 역사적 capture protocol `v1`과 issuer ref version `1.0`은 별개 축이며 기존 exact-read/hash·감사 재시도를 보존한다.
5. capture가 보존되는 동안 run/artifact 참조를 임의 purge하지 않는다. 현행 orphan cleanup의 참조 재검사·DB metadata 비삭제·제한 역할을 유지한다. 새 보존 숫자·자동 purge·권한 확대·DB schema/migration을 도입하지 않는다. schema가 필요해지면 별도 변경 영향부터 검토한다.

## 보장과 제외

보장은 전용 credential을 사용하는 검증된 Python 발행 경로다. INSERT 권한자의 직접 SQL을 DB가 부모 상태별로 차단하는 보장은 별도 검토로 남긴다. Trigger·RLS·업무 DB 함수를 추가하지 않는다. 물리 원본의 불변성/삭제 보호와 DB metadata의 원자성도 같은 보장으로 취급하지 않는다.

기존 Candidate legacy/v2 성공 key·HMAC/hash/TTL/암호화·GET 감사0·의료/Privacy/Source 통제는 유지한다. 실제 Source/API/S3·운영 정책 수치·운영 DB/credential 배포·공개 flag/일반 DI 활성화·merge/release는 승인 범위가 아니다. 새 경합 검증 통과나 agent 검토만으로 실제 Source freshness/공개 gate를 해제하지 않는다.

구현/검증: [Source 원자 저장 합성 증거](../../testing/source-run-artifact-close-20261104.md). 계약 승인·구현 증거·운영/공개 승인을 구분한다.
