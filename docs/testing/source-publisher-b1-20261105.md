# Source publisher B1 — 합성 내부 구현·검증

- 날짜: 2026-11-05. [Owner B1 결정](../governance/decisions/2026-11-05-source-trusted-publisher-b1.md)을 따른다.
- 정본: [Source Target](../contracts/targets/post-mvp-1/rag-source-ingestion-v1.md), [Capture Target](../contracts/targets/post-mvp-1/candidate-request-guard-audit-v1.md), [계약 인덱스](../contracts/README.md).
- 상태: **합성 내부 구현·검증 완료**. 운영 적용·실제 Source·Current 승격·공개 승인이 아니다.

## 바뀐 동작

기존 Writer는 저장된 Source Snapshot을 선택하고 기존 governance·감사를 처리한다. run/artifact/snapshot/member INSERT는 더 이상 받지 않는다. 별도 Source aggregate publisher는 새 자료와 관련 기록 전체를 기존 한 transaction으로 저장한다. 수집이 중간에 실패해 일부 DB 기록만 남지 않도록 기존 savepoint/outer rollback·물리 원본 cleanup 경계를 유지했다.

Publisher에는 Source 8개 표 SELECT/INSERT와 기존 marker 7개의 UPDATE만 부여한다. Writer와 Publisher의 관리자/소유권·membership/SET ROLE·PUBLIC/default/column 우회와 금지 table/업무 열 쓰기를 검사한다. Publisher가 구성되면 target schema의 PUBLIC table/sequence/column·owner default grant를 정리하며 다른 business role의 직접 grant는 유지한다. Publisher 자체의 직접 artifact INSERT·CURRENT Snapshot INSERT·publication-approval Verification 작성은 신뢰 범위로 남는다. 지원 Python 수집 경로의 PENDING/자동 integrity 규칙을 row별 SQL ACL 보장으로 표현하지 않는다.

Product·Label·rematerialization config는 `SOURCE_PUBLISHER_*`와 `publisher` 필드를 사용한다. 구 Writer credential/필드 fallback은 없다. 전체 명령의 acquisition·put·Snapshot/Verification/run/artifact·Label member·rematerialization hierarchy·commit 후 재조회를 같은 publisher 실행 경계로 옮겼다. `persist_complete_attempt`만 다른 connection에 맡기지 않는다. Source version 비교·NO_CHANGE·실패 원본·기존 clock/receipt·Candidate 재시도·의료/Privacy·공개 차단은 유지한다.

## 권한 전환과 검토 보완

관리 T1은 Writer의 4개 표 INSERT 거부를 별도 transaction으로 commit한다. T2는 Writer/Publisher 최종 ACL을 적용한다. 두 단계 모두 commit 뒤 NullPool의 새 물리 admin session에서 read-only로 실효 권한을 재조회한다. T1 이후 T2/read-back 실패 시 기존 거부를 복원하지 않는다. T1 자체 실패는 구 권한이 남을 수 있음을 보고한다. 운영 deployment script의 명령 순서·실패 시 재시작 금지는 mock으로 검증했으며 실제 운영 cutover는 실행하지 않았다.

독립 검토에서 필수 Product CLI 통합 테스트 누락과 T2 fresh-session read-back 누락을 지적하여 보완했다. 실제 Publisher login으로 Product 전체 CLI를 호출해 CREATED→NO_CHANGE와 invalid-record FAILED+RAW/REJECTS·commit 후 receipt를 검증했다. Label의 member 실패 rollback/cleanup/재시도와 rematerialization도 실제 Publisher login에서 확인했다. 외부 HTTP는 합성 MockTransport, 물리 finalizer는 테스트 대역이며 DB/Source gate·manifest·저장·재조회 검증을 우회하지 않았다.

새 read-back의 첫 실제 PostgreSQL 실행에서는 optimizer가 WHERE 조건 순서를 바꿔 일반 table/index에 sequence 권한 함수를 적용하는 오류가 발생했다. sequence 함수에 명시적 CASE 분기를 적용하여 보완했고, 같은 영향 역할/관리/RET-H 전체 63건을 최종 재실행하여 통과했다. 최초 실패 로그도 보존하며 PASS로 합산하지 않는다.

## RET-H legacy Stage1

Owner의 1번 선택에 따라 구 Stage1 CLI는 credential 읽기·engine/fixture/helper/receipt I/O 전에 고정된 안전 오류로 종료한다. Writer의 직접 SQL Stage1 시도도 실제 DB에서 `42501`로 실패하고 transaction의 hierarchy INSERT까지 rollback됨을 확인했다. Stage2 로직과 과거 receipt/data는 변경하지 않았다. Stage2 검증을 위한 admin fixture 준비는 테스트 전용이며 운영 Stage1 우회 경로가 아니다. [기존 실행 문서](ret-h-aws-synthetic-smoke-178.md)에 제한을 명시했고 로컬 `.omx/plans/plan-ret-h-stage1-b1-migration-deferred-2026-11-05.md`에 후속 미승인 이관 계획을 남겼다.

## 환경과 최종 결과

작업 소유 macOS arm64 native PostgreSQL 17.11을 loopback 55439에서 사용했다. 공식 PostgreSQL 소스/공식 SHA-256과 pgvector v0.8.6 고정 commit `8ee86c96f0fd72390f890aa8a336fda6d3ab4c6c`을 확인하고 `/private/tmp/dosey-source-b1-pg17-20261105/` 아래에만 빌드했다. `test`와 별도 이름의 cleanup/bootstrap 합성 DB만 사용했다. 임시 venv는 기존 합성 테스트 패키지를 읽기 전용으로 재사용했다. 프로젝트 dependency/lock·전역 서비스·실제 `.env`/credential·운영 DB·실제 Source/API/S3/Provider를 사용하거나 변경하지 않았다.

JUnit의 test identity로 중복을 제외한 **1,516건 PASS**, 최종 실패 0건이다. 이전 A안 PASS나 agent별 중복 실행은 합산하지 않는다.

| 검사 | 중복 제외 PASS | 확인한 범위 |
|---|---:|---|
| Source·cleanup·admission·RET-H 단위 | 852 | 기존 저장/실패·CLI 설정·credential 격리·Stage1 I/O 전 차단·최종 MAINTAIN 거부 |
| Source·capture·Index·Publisher 명령 PostgreSQL | 121 | 실제 역할·원자 저장·Product/Label/rematerialization·post-commit receipt·두 세션 경합 |
| 역할·Source 관리·RET-H PostgreSQL | 63 | T1/T2·새 session ACL read-back·drift·Writer 거부·기존 선택·Stage2 경계 |
| Credential·배포 설정 계약 | 173 | secret 혼입·역할 충돌·고정 명령/RO mount·실패 시 재시작 금지 |
| Candidate 단위·계약·합성 eval | 218 | 기존 Guard·멱등·DTO·의료/Source 안전 회귀 |
| Cleanup PostgreSQL | 57 | 참조 보호·권한·실패/삭제 재시도·경합 |
| Candidate PostgreSQL | 32 | 기존 성공 replay·Bundle/environment revision·영속 감사 transaction |

Docker 전용 bootstrap/redeploy 2건과 Management bootstrap 1건은 **3건 skip**이다. 별도로 실제 native PostgreSQL에서 수정한 bootstrap SQL 두 파일의 실행·재적용, publisher의 nonadmin LOGIN/no-membership, Management↔Publisher identity 충돌 거부를 확인했다. 이 SQL 검사 결과를 Docker container bootstrap PASS로 표시하지 않는다.

Ruff/format은 변경 Python 40개, mypy는 영향 production Python 16개에 통과했다. Python test inventory·보호 table writer·DB 로직 재도입 검사·단일 migration head `925a1b2c3d4e`·배포 shell 구문·문서 참조 202건·scope/diff 검사도 통과했다. 새 schema/migration·Trigger/RLS/업무 DB 함수·의존성·공개 flag 변경은 없다. 전체 pytest/전체 app typecheck·Linux Docker CI·외부 Claude 검수는 실행하지 않았다.

## 실행 환경 격리의 한계

Source publisher one-shot profile은 기본 OFF다. 설정에는 nonroot `10001:10001`, 고정 Product 명령, read-only 입력/원본과 고정 finalizer client mount(`/publisher-bin/source-artifact-finalizer`, host path 자동 생성 금지)를 명시했다. 다른 일반 서비스/Writer에는 Publisher password·그 실행 mount·Docker socket을 추가하지 않았다. finalizer 자식의 `env={}`도 유지한다.

**Docker render/container smoke·실제 OS/container secret 접근/기동 격리·외부 artifact owner와 trusted finalizer client 준비는 NOT_RUN이다.** client/owner 구현을 새로 만들거나 root/setuid/sudo·쓰기 가능한 최종 원본 mount로 우회하지 않았다. 설정 파일만으로 운영 가능한 publisher나 완성된 process 격리를 주장하지 않는다. 실제 운영 secret/host ACL·배포/공개는 별도 조건이다.

## AI agent 모델 변경

B1 중 Owner가 최신 모델 반영을 요청하여 로컬 지원 카탈로그와 현재 역할 도구 정의를 확인했다. 프로젝트 main 기본값·조사/문서/검수는 GPT-6.1 Sol, 구현 6개 역할은 GPT-6.1 Terra로 갱신했다. 기존 reasoning/권한/역할 지침과 역할 수 10개는 유지했다. 실제 서비스 LLM/전역 Codex·OMX 설정은 바꾸지 않았다. 현재 채팅의 UI 모델을 소급 변경한 것으로 기록하지 않는다. [모델 배분과 증거 범위](../ai/model-routing.md)를 따른다.

명령·JUnit·실패/최종 로그·source checksum·전후 snapshot·파일 hash·검토·모델 TOML 검증은 로컬 `outputs/source-publisher-b1-20261105/`에 보관한다. Agent 검토와 합성 PASS는 merge·운영/실제 Source·공개 승인이 아니다.
