# Source run·artifact 원자 저장 — 합성 내부 검증

- 날짜: 2026-11-04
- 승인: [Owner Decision](../governance/decisions/2026-11-04-source-run-artifact-close.md).
- 정본: [Source Target](../contracts/targets/post-mvp-1/rag-source-ingestion-v1.md), [capture Target](../contracts/targets/post-mvp-1/candidate-request-guard-audit-v1.md).
- 상태: **합성 내부 구현·검증 완료**. 실제 Source·운영 정책/DB·Current 승격·공개 승인 아님.

## 구현 결과

`SqlAlchemySourceSnapshotRepository.persist_complete_attempt(record, artifacts)`가 내부 새 UUID의 terminal run과 전체 RAW_RESPONSE/REJECTS를 같은 savepoint에 기록한다. 기존 run ID를 받아 붙이는 `create_run`/`create_artifacts` 지원 포트와 호출부는 제거했다. 새 자동 재시도·upsert·호환 append wrapper는 없다. 저장 중 오류를 caller가 잡아도 run/artifact 부분 저장이 남지 않으며, 전체 Snapshot/Verification aggregate의 outer commit/rollback은 기존 caller가 소유한다.

성공·NO_CHANGE·정책/수집/parser/version 실패 경로를 이관했다. Detail 실패의 원본도 최초 attempt tuple에 포함한다. 기존 clock·failure code·receipt·attempt unique·Snapshot 재사용 의미를 유지한다. 기존 parser identity REJECTS가 FAILED로 귀결되어 `SUCCEEDED_WITH_REJECTIONS` 분기에 도달하지 못하는 상태 불일치는 원래 존재한 것으로 확인했다. 이를 이유로 parser 안전 규칙을 완화하거나 새 partial-success 정책을 도입하지 않았다.

기존 artifact mutation advisory lock을 permanent put과 Operation 잠금 전에 확보한다. product/label/rematerialization/cleanup의 기존 경계를 재사용하고 reusable ingestion·failure 경로도 참여한다. acquisition의 Source try-lock은 nonblocking 의미를 유지한다. private spool 수집과 영구 원본 발행을 구분한다.

합성 `SyntheticSourceCaptureAdmission`은 서버가 보유한 새 수집 정보·Writer 반환 run ID·issuer pin·원래 clock/profile/Source 좌표를 v2 관측과 대조한 뒤 기존 저장소에 기록한다. 이 context는 일관성 입력이며 위조 불가능한 권한 토큰이나 독립 fetch 증명이 아니다. 권한 경계는 검토된 서버 coordinator와 분리된 DB issuer role이다. 실제 coordinator/일반 API·DI에는 연결하지 않았고 실제 Source code는 허용하지 않는다. `v1` capture와 issuer version `1.0`의 역사적 exact-read/hash는 유지한다.

## 검증 환경

Docker CLI·로컬 socket/API·상태 창이 응답하지 않아 기존 Docker/컨테이너를 재시작하지 않았다. 공식 PostgreSQL 17.11 소스와 공식 SHA-256을 대조해 작업 소유 `/private/tmp` prefix에만 빌드했다. pgvector v0.8.6도 공식 태그의 commit `8ee86c96f0fd72390f890aa8a336fda6d3ab4c6c`으로 고정해 같은 prefix에 설치했다. 프로젝트 의존성·lock·전역 서비스는 바꾸지 않았다.

실행은 **macOS arm64 native PostgreSQL 17.11**의 loopback 전용 합성 DB였다. Source 검사는 55441, 기존 Candidate fixture가 고정 요구하는 55439로는 다른 연결이 없는 작업 소유 서버만 포트를 바꿔 검사했다. Source cleanup은 별도 이름 `dosey_source_close_cleanup347_test`의 빈 DB에 기존 migration을 적용해 검증했다. 기존 앱 DB/실제 Source·Provider·S3는 사용하지 않았다. Linux Docker CI 결과로 표시하지 않는다.

## 최종 결과

서로 다른 관련 검사 **1,269건 통과**. Agent별 검사와 중복 실행은 더하지 않는다.

| 검사 | PASS | 증거 범위 |
|---|---:|---|
| Source ingestion·cleanup·새 admission 단위 | 781 | 원자 포트, parser/실패 원본 보존, publication-before-put/Operation, 기존 nonblocking acquisition |
| Candidate 단위·계약·합성 eval | 218 | 기존 stage/안전·DTO·멱등 계약과 합성 평가 |
| Source 원자 저장·capture PostgreSQL | 12 | run/artifact 부분0, caught DB CHECK 실패·취소/outer rollback, v1/v2, 실제 역할·두 세션 4사례 |
| 기존 Source lifecycle·reject·역할 PostgreSQL | 82 | 기존 Snapshot/NO_CHANGE/실패·별도 credential 회귀 |
| 기존 cleanup 참조·실행 PostgreSQL | 57 | 참조 보호·승인/실패/결과 불명·재시도·기존 삭제 경합 |
| Candidate·Runtime·감사 PostgreSQL | 32 | 기존 성공 replay·환경 revision·소유권·기존 실패 감사 transaction |
| Candidate API·멱등·인증/탈퇴 | 87 | 기존 API·receipt·계정 삭제 및 실패 처리 |

두 세션 검사는 다음을 실제 PostgreSQL 연결과 제한 역할로 확인했다.

1. Source gate 선행 writer와 global 선행 writer가 경합해도 기존 fail-fast를 유지하고 대기 cycle 없이 완료한다.
2. writer가 실제 private bytes와 참조를 먼저 commit하면 기다리던 cleanup이 참조를 확인하고 삭제를 차단한다.
3. cleanup이 먼저 삭제하면 writer가 bytes를 다시 검증/보존한 뒤에만 DB 참조를 commit한다.
4. run INSERT와 artifact INSERT 사이의 미완료 transaction을 RR issuer가 발행하지 못한다. 이전 RR snapshot을 최신으로 취급하지 않고 새 transaction에서만 완성된 v2 관측을 기록한다.

역할 검증은 임시 role의 실제 `SET LOCAL ROLE` 실행이다. 운영 credential 전달/배포 검증과 혼동하지 않는다. Source Writer의 허용 INSERT, payload UPDATE/DELETE 금지, capture issuer의 Source INSERT 금지, cleanup의 metadata DELETE 금지 등을 검사했다. 직접 Writer SQL의 조건부 INSERT 차단은 승인 범위 밖이며 보장하지 않는다.

## 검토·정적 검사·한계

독립 검토의 HIGH 두 세션 증거와 MEDIUM acquisition context 구성 지적을 보완했다. 원래 수집 정보와 실제 Writer 반환 UUID를 먼저 고정한 뒤 observation을 구성하고, 과거 run·issuer·profile·clock·replay 불일치를 검사했다. 합성 내부 범위에서 열린 BLOCKING/HIGH/MEDIUM은 없다. 기존 partial-success 상태 도달성 LOW는 정보성 잔여 사항이다.

Ruff·format, 대상 production source 7개의 mypy, 테스트 inventory, DB 함수/Trigger/RLS 재도입 검사, 단일 migration head(925), 문서 참조·diff/scope 검사를 수행했다. 새 migration·DB schema·dependency·역할 권한 확대는 없다. 전체 pytest/전체 app typecheck/전체 Linux CI·외부 Claude 검수는 실행하지 않았다.

초기 Source cleanup 49건은 명시적 DB 설정 부재로 skip됐고 별도 합성 DB를 준비한 최종 57건 실행으로 해소했다. Candidate 초기 검사는 고정 포트 guard와 병렬 public-schema fixture 충돌로 실패했으며, 포트를 맞추고 API 검사와 직렬 실행한 최종 32건이 통과했다. 실패 로그도 보존하며 그 결과를 PASS로 계산하지 않는다.

명령·로그·source checksum·파일 hash·독립 검토는 로컬 `outputs/source-run-artifact-close-20261104/`에 보관한다. 실제 Source freshness/운영 수치·live issuer·직접 SQL 방어·Source 운영 purge/보존·공개 gate는 여전히 별도 조건이다. 이 검증이나 Agent 검토는 Owner의 merge/배포/공개 승인을 대신하지 않는다.
