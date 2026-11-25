# Candidate Exact·Alias 실행 및 실제 Source 정책 근거

- 날짜: 2026-11-04
- [실행 범위 결정](../governance/decisions/2026-11-04-candidate-exact-alias-validation.md)
- [합성 내부 Contract Freeze](../contracts/targets/post-mvp-1/candidate-request-guard-audit-v1.md)
- 상태: Exact/Alias adapter와 Owner 승인 두 저장 구조·내부 transaction 검증. 실제 Source 수치/사용·운영 배포·Current 승격·공개는 미승인.

## 1. Source capture inventory

| 검증 항목 | 기존 구현 | 결론 |
|---|---|---|
| full scan/PK | [SourceRunResult](../../ai_worker/tasks/rag/source_client/contracts.py:194) | memory object의 성공·full_scan·PK 상태. LIVE/replay origin·capture issuer는 없음 |
| 실제 network capture | [acquire_mfds_product](../../ai_worker/tasks/rag/source_ingestion/mfds_product.py:83) | acquisition lock+fetch_all_pages와 process clock 관측. wrapper 자체는 영속 capture 권한이 아님 |
| Metadata clock | [SnapshotIngestionMetadata](../../ai_worker/tasks/rag/source_ingestion/snapshot_lifecycle.py:333) | timezone·시작/완료 순서 검사. 외부 capture 시각과 일치하거나 trusted issuer인지 증명하지 않음 |
| 원본 bytes 결속 | [acquire/manifest](../../ai_worker/tasks/rag/source_ingestion/acquire.py:144), [checksums](../../ai_worker/tasks/rag/source_ingestion/checksums.py:25) | page/raw checksum·manifest는 제시된 bytes 무결성. 새 network 취득과 과거 bytes replay는 별개 |
| Snapshot/run/artifact | [모델](../../backend/app/models/rag_source.py:249) | run·page·원본 metadata·checksum·처리 시각은 보존하나 authenticated capture origin은 없음 |
| NO_CHANGE | [persistence](../../ai_worker/tasks/rag/source_ingestion/snapshot_lifecycle.py:779) | 기존 Snapshot 재사용+새 run/artifacts+caller finished_at 관측. 새 외부 조회를 뜻하지 않음 |
| endpoint 측정 | [제품 API receipt](../validation/rag/endpoints/LIST_APPROVED_PRODUCTS.json:51), [receipt 의미](../validation/rag/endpoints/README.md:38) | 과거 실제 endpoint/schema/pagination 검증 자료가 존재. 개별 run의 live freshness·Source 이용 승인은 아님 |
| Source 목적 승인 | [#807](../contracts/proposed/post-mvp-1/source-use-approval-807.md:12) | PRODUCT_IDENTIFICATION 어휘는 예약돼 있으나 해당 목적의 issuer·증거·환경 범위는 별도 계약 결정 필요 |

원본 manifest와 normalized canonical checksum은 서로 다른 hash domain이다. NO_CHANGE의 raw bytes/순서는 달라도 canonical 내용은 같을 수 있어 run별 raw manifest를 재계산해야 한다. Snapshot의 최초 raw manifest를 후속 capture의 원본 증명으로 대체하지 않는다. XML label 수집 기록은 제품 허가 API capture의 증명이 아니다.

**권고:** 기존 run/artifact 무결성은 재사용하고 authenticated capture 관측을 별도로 발행한다. origin/시각 field만 추가해서 replay를 LIVE로 self-assert하는 구현은 피한다. 제한 writer/실제 capture coordinator의 발행 경계를 만들지 못하면 freshness는 UNKNOWN으로 둔다. 구체적 field/권한은 위 계약 요청 3A절을 따른다.

## 2. 수치·평가 상태

- 실제 Source max-age/cadence/승인 TTL: **NEEDS_MORE_EVIDENCE / 미승인**.
- 운영 relevance/margin/weights·Search TTL·retention 숫자: **미승인**. 합성 예제값을 채택하지 않음.
- 실제 official identity 기반 운영 calibration: **NOT_EVALUATED**. 허용 Source Snapshot/정답·그룹 분리·독립 검토가 선행.
- 기존 16개 합성 DEV matrix: 의미/안전 회귀용. 실제 Source precision/coverage나 임상 품질 자료가 아님.
- Pure policy는 release_eligible=False만 허용하며 일반 DI/public flag는 계속 차단.

Exact/Alias SQL 점수는 1.0이고 합성 relevance는 max(stage score)다. 오선택의 identity/속성/Alias 근거 문제를 relevance 숫자 조정으로 해결할 수 없다. 운영 threshold는 relevance와 RRF margin을 구분하고 잘못된 READY 수·coverage/abstention·독립 그룹 수·95% CI·overflow/누락 경쟁 후보를 평가한 뒤 결정한다.

## 3. 실행 결과

Owner가 두 저장 구조·분리 발행 역할·FAIL 감사 transaction의 합성 내부 구현을 승인한 뒤 다음 검사를 실행했다. **서로 다른 관련 검사 375건 통과**이며 전체 CI나 실제 Source 운영 검증은 아니다. 과거 360 PASS와 이번 초기 baseline은 이 수에 더하지 않는다.

| 최종 검사 | 결과 | 범위 |
|---|---|---|
| Candidate 단위·계약·합성 eval | 218 PASS | Exact/Alias 제한, 경쟁 Trigram 유지, closed projection/hash, 기존 DTO·멱등 계약 |
| 기존 API·멱등·인증/탈퇴 | 87 PASS | Candidate API/service, 성공 receipt 재현, 계정 삭제/실패 처리 |
| 기존 PostgreSQL 연결·동시성 | 23 PASS | 기존 합성 Search, Runtime 결속, 식별 동시성 회귀 |
| migration·capture·역할 | 38 PASS | forward head 925, legacy 행 보존, 손실 downgrade 거부, NO_CHANGE per-run manifest, 실제 제한 역할 저장, READ COMMITTED 차단 |
| 신규 영속 감사 PostgreSQL | 9 PASS | PASS 원자성, savepoint 없는 callback/실제 confirm 멱등 실패의 감사-only commit, 무결성 오류/취소 rollback, 성공 replay 감사0, hash/상위 참조 변조 거부, 제한 삭제 역할과 공유 참조 보존 |

Ruff, 새/변경 source 10개에 대한 mypy(`--follow-imports=skip --ignore-missing-imports`), 테스트 분류 inventory, 단일 Alembic head, DB 함수/Trigger/RLS 재도입 검사, 문서 경로 및 `git diff --check`가 통과했다. 새 Python 파일 formatting 전후 AST가 같음을 확인했다. 최종 선택 파일/명령/log/hash는 로컬 `outputs/candidate-policy-20261104/verification.json`, 독립 검토는 같은 디렉터리의 `final-independent-review.md`에 있다. 전체 프로젝트 typecheck/전체 CI/외부 Claude 검수는 실행하지 않았다.

독립 검토에서 발행 역할과 FOR SHARE 권한 불일치, caller savepoint 의존, 재시도 상위 증거 검증 누락을 찾아 수정했다. 최종 검토의 합성 내부 저장·transaction 범위에 남은 BLOCKING/HIGH/MEDIUM은 없다. 검토·테스트는 Owner의 merge/공개 승인이 아니다.

**미구성·미검증 경계:** 실제 trusted live capture issuer, 정책 본문 registry/운영 수치 evaluator, 신규 영속 감사와 일반 Candidate DI/API의 자동 연결, Source capture 운영 purge/retention은 연결하지 않았다. terminal run의 후속 artifact append를 금지하는 발행 계약과 신규 Guard 전체의 두 세션 철회·ABA·처방/탈퇴 경합 matrix는 입증되지 않았다. 일관된 읽기만으로 이 경쟁을 해결했다고 보지 않으며 실제 Source freshness 사용을 차단한다. 기존 공개 flag/기본 DI·성공 키 hash/HMAC/TTL/암호화·공통 idempotency 구현·기술 안전/공개 gate 문서는 작업 시작 hash와 같다.

권고 후속 순서는 Source terminal run/artifact 집합의 불변 발행·writer 참여·cleanup 계약을 먼저 정리하고, Service07/08 호환성과 실제 capture receipt를 별도 검증한 다음 max-age와 오류 허용 기준을 결정하는 것이다. 근거가 없는 숫자는 계속 미구성으로 둔다. 이번 migration은 격리 임시 합성 schema/DB에서만 검증했으며 실제 앱 DB에 적용하지 않았다.

## 4. 공식 Source 문서 근거 (2026-11-04 열람)

[공공데이터포털 공식 영문 상세 페이지](https://www.data.go.kr/en/data/15095677/openapi.do)는 갱신 주기를 `Real-time`으로 표시한다. 포털 수정일은 2026-09-18이며, 개별 레코드나 전체 snapshot 기준시각을 의미하지 않는다. 허용 max-age나 보장 전파 지연/SLA로 환산할 근거는 없다.

현행 문서는 `DrugPrdtPrmsnInfoService08/getDrugPrdtPrmsnInq08`을 안내한다. 저장소의 [기존 제품 endpoint receipt](../validation/rag/endpoints/LIST_APPROVED_PRODUCTS.json)는 2026-09-05 Service07 실측이다. 두 버전의 호환성·Service07 종료일은 이번 조사로 확인하지 못했다. **이 버전 차이를 해소하기 전 기존 receipt를 현행 API 검증으로 취급하거나 parser/Source를 자동 전환하지 않는다.** Service08은 별도 endpoint/schema/pagination/PK/receipt 검증 대상으로 권고한다.

공식 페이지에는 개발 트래픽 10,000과 운영 증설 신청, 무료·이용범위 제한 없음이 표시되지만, 수치형 RPS/초기화 시간대나 현재 상세 응답 필드 정의·snapshot 원자성·ETag/Last-Modified 지원 여부는 접근 가능한 문서에서 입증하지 못했다. 부재를 미지원으로 단정하지 않는다. 이용 조건은 [포털 이용정책](https://www.data.go.kr/en/ugs/selectPortalPolicyView.do)의 제3자 권리·성능 제한과 프로젝트 Source/Privacy 공개 gate를 함께 검토해야 한다.

조사는 공개 문서만 열람했다. 한국어 원본은 도구 오류로 열지 못했고, 영문 공식 페이지는 검색 도구가 반환한 수집본이다. 실제 Provider API·API Key·데이터 수집·로그인은 실행하지 않았다. 운영 수치 calibration은 계속 NOT_EVALUATED다.
