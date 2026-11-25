# Source Writer run 권한 축소 — 합성 내부 검증

- 날짜: 2026-11-05
- 승인 범위: [Owner Decision](../governance/decisions/2026-11-05-source-writer-run-privileges.md)의 A안.
- 정본: [Source Target](../contracts/targets/post-mvp-1/rag-source-ingestion-v1.md), [계약 인덱스](../contracts/README.md).
- 상태: **합성 내부 구현·검증 완료**. 운영 적용·실제 Source·Current 승격·공개 승인 아님.

## 변경 결과

`infra/python/source_role_policy.py`에서 Source Writer의 run 업무 열 `snapshot_id`, `run_status`, `failure_code`, `failure_message`, `duration_ms`, `finished_at` UPDATE 부여를 제거했다. run을 기존 `WRITER_LOCK_TABLES`에 포함하여 `knowledge_index_lock_marker` UPDATE와 NOT NULL·검증된 zero CHECK 검증을 재사용했다. 새로운 schema/migration·DTO/API/enum·의존성·별도 발행 역할은 추가하지 않았다.

단일 production 파일의 변경은 위 두 부분이다. 기존 snapshot/Operation/approval 권한, 전체 revoke/regrant·role 경계 검증은 유지한다. 기존 원자 Writer API, outer transaction, acquisition fail-fast/global artifact 잠금 순서, capture clock/hash 및 Candidate 멱등 키는 변경하지 않았다. 상세 원자 발행 경계는 [기존 결정](../governance/decisions/2026-11-04-source-run-artifact-close.md)을 따른다.

실제 Source Writer 로그인으로 새 aggregate를 저장하고 `append_snapshot_member`의 ARTIFACT provenance 경로를 호출했다. 별도 Knowledge Index Builder 로그인으로 ARTIFACT origin을 가진 Index를 발행하여 run/artifact 행 잠금을 검증했다. 두 역할을 서로 대체한 결과가 아니다. Source readiness는 해당 합성 fixture 안에서만 준비했다.

**직접 SQL artifact append 차단은 구현하지 않았다.** Writer의 artifact INSERT는 유지된다. 실제 Writer가 이미 commit된 run에 새 page/key의 artifact를 직접 INSERT할 수 있음을 확인한 뒤 그 진단 transaction을 rollback했다. 이 검사를 SQL append 거부 PASS로 기록하지 않는다. Runtime(reader)·capture issuer·cleanup은 각각 실제 역할로 artifact INSERT를 시도하여 `42501` 거부를 확인했다. 이 권한 경계는 물리 원본 불변성이나 실제 acquisition 진위를 증명하지 않는다.

## 검증 환경과 방법

Docker는 사용할 수 없어 기존 서비스를 재시작하지 않았다. PostgreSQL 17.11 공식 소스/공식 SHA-256과 pgvector v0.8.6의 고정 commit `8ee86c96f0fd72390f890aa8a336fda6d3ab4c6c`을 확인하고, 작업 소유 `/private/tmp/dosey-source-acl-pg17-20261105/` 아래에만 빌드했다. macOS arm64 native, loopback 55439의 빈 합성 `test` DB와 별도 `dosey_source_acl_20261105_cleanup347_test` DB를 사용했다. cleanup DB에는 기존 migration head를 적용했다. 실제 앱/운영 DB, Source API, S3, Provider를 사용하지 않았다.

작업 전용 Python venv는 기존 합성 테스트 환경의 설치 패키지를 읽기 전용으로 재사용했다. 공유 환경에 설치하거나 프로젝트 dependency/lock·전역 서비스 설정을 변경하지 않았다. sandbox의 네트워크/공유 메모리 제한에 대해서는 작업 소유 공개 소스 다운로드와 합성 DB 실행만 별도 실행 권한을 사용했다. 인증서 검증은 유지했다. 기존 credential은 읽거나 사용하지 않았다.

테스트 우선 순서는 다음과 같다.

1. 변경 전 policy와 보관한 기존 ACL 테스트: **1 PASS**.
2. 변경 전 policy에서 실제 Writer ARTIFACT member와 Builder 두 origin case: **3 PASS**.
3. 새 payload UPDATE 거부 테스트를 변경 전 policy에 실행: 기존 UPDATE가 허용되어 예상대로 **1 FAIL (`DID NOT RAISE`)**.
4. 최소 policy 변경 후 같은 ACL 테스트: **1 PASS**. 최종 회귀 결과에 이 실행만 합산한다.

## 최종 검사 결과

서로 다른 관련 테스트 **1,215건 PASS**. baseline·예상 RED·중복 실행은 합산하지 않았다.

| 검사 | PASS | 범위 |
|---|---:|---|
| Source ingestion·cleanup·합성 admission 단위 | 781 | 기존 원자 저장·실패 보존·잠금·안전 입력 |
| Candidate 단위·계약·합성 eval | 218 | 기존 Guard·runtime binding·멱등·DTO·합성 평가 |
| Source Writer ACL | 1 | 6열 각각과 marker 혼합 UPDATE의 `42501`, stale table/column/PUBLIC 정리·2회 적용, run 포함 marker 검증·0 허용/1 거부 |
| Source·capture·Index PostgreSQL | 104 | lifecycle·reject·complete attempt·실제 역할·ARTIFACT member/origin·두 세션 publication 경합·v1/v2 capture |
| Role provisioning·배포 계약 | 22 | 실제 migration head의 권한 경계·provisioning·설정 계약; Docker 전용 2건은 별도 skip |
| Cleanup PostgreSQL | 57 | 참조 보호·별도 역할·실패·삭제 재시도·경합; 초기 opt-in skip 해소 |
| Candidate PostgreSQL | 32 | 기존 성공 replay·Bundle/environment revision·소유권·영속 감사 transaction |

Source PostgreSQL 실행에서 cleanup opt-in 설정이 없어 49건이 처음 skip되었다. 별도 합성 DB를 준비한 최종 cleanup 57건으로 그 49건을 실행했다. 최종 미실행은 `test_database_role_provisioning.py`의 Docker 전용 bootstrap/redeploy·approval bootstrap **2건**이다. native 역할 테스트 통과를 Docker bootstrap 실행 증거로 바꾸어 쓰지 않는다.

Ruff check·format(변경 Python 5개), 대상 production 파일 mypy, Python test inventory, DB 함수/Trigger/RLS 재도입 검사, 단일 migration head `925a1b2c3d4e`, 문서 참조·scope·`git diff --check`를 통과했다. Source/Candidate 관련 회귀 외 전체 pytest·전체 app typecheck·Linux Docker CI·운영 credential 전달/배포·외부 Claude 검수는 실행하지 않았다.

독립 검토에서 Source Target에 남은 종전 lifecycle UPDATE 허용 문장이 BLOCKING으로 지적되어, 해당 문장을 이번 합성 내부 개정의 최초 INSERT·marker-only UPDATE 규칙으로 수정했다. 구현·테스트 변경 없이 정본 계약의 내부 충돌을 해소했다. 검토 원문과 수정 후 재검토 결과는 로컬 증거에 구분해 보관한다.

초기 runner의 `ENV` 대소문자, 공유 venv의 Ruff 실행 파일 경로, backup Python 파일의 inventory 분류 문제는 작업 전용 환경/증거 파일만 조정하여 해결했다. 최초 sandbox DB 접속은 권한 제한으로 실패했으며 해당 실패와 최종 native 실행을 구분해 보존한다. 기존 테스트 파일 사본은 inventory 대상과 혼동되지 않도록 `.py.snapshot` 확장자로 보관한다.

명령·JUnit·stdout·체크섬·기준 snapshot·변경 파일 hash·독립 검토는 로컬 `outputs/source-writer-role-boundary-20261105/`에 보관한다. 현재 검증은 합성 내부 범위이며 실제 운영 ACL/저장소 밖 caller 사용은 미확인이다. 운영 role 재적용, 신뢰 publisher/permit 설계, Source 운영 수치·공개 gate·merge/release는 별도 결정 대상이다.
