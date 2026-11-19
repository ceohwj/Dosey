# CR-A 동기 Guide 연결 검증 — 2026-11-04

## 범위와 구현

Human Owner가 승인한 [CR-A 결정](../governance/decisions/2026-11-04-guide-sync-request-authority.md)과 [목표 계약](../contracts/targets/post-mvp-1/guide-sync-request-authority-v1.md)을 구현했다. 기준 commit은 `bcac7835420118e13ee235410c6b9fdd21224b14`이고 기존 미커밋 변경은 보존했다. 이 증거는 Current 승격·공개·merge 승인이 아니다.

- `backend/app/services/guide_request_authority.py`: 명시한 환경의 canonical Bundle과 유일한 retrieval binding을 검증하고, 필수 주입 evaluator의 실제 PASS/FAIL 관측을 새 요청 UUID로 기록한다. 성공 결과는 기존 불변 `GuideRuntimeContextSnapshot`이다. 최신 Guard 재사용이나 기본 PASS evaluator는 없다.
- `backend/app/main.py`, `backend/app/dependencies/services.py`: app에는 동기 `for_session(session)` factory를 보관하고, 요청의 repository와 동일한 세션으로 issuer를 만든다. 요청별 `resolve`/`revalidate`는 async 계약이다. 기존 opaque Provider 의존성과 모든 공개 flag OFF 경계는 유지한다.
- `backend/app/services/guide_sync_runtime_execution.py`, `guide_sync_runtime_lifecycle.py`: A에서 처방 잠금, Guard·Guide·PROCESSING Job·Full Context와 Patient Context digest를 저장하고 commit한다. Job의 임시 5분 lease는 이 동기 실행에만 결속한다. B에는 불변 carrier만 넘기고 요청 DB transaction을 유지하지 않는다. 별도 Worker reader의 짧은 조회 transaction까지 금지하는 의미는 아니다.
- `backend/app/services/guide_sync_runtime_finalizer.py`: C에서 identity map을 만료하고 User `FOR SHARE` → Prescription → Job → Guide → Environment 순서로 잠근다. 계정 활성 상태/token version, 실행 lease, 처방·동일 버전 Identification 집합·Context·환경 revision·Bundle·execution/retrieval manifest를 재확인한다. evaluator는 현재 Patient Context/Source 사실과 권한을 다시 검증하고 관련 사실의 동시 변경을 보호할 책임이 있다.
- C 성공은 Guide projection과 Job을 함께 commit한 뒤 `201`이다. 현재성 불일치는 답변·인용 없이 STALE projection/Job을 commit하고 기존 `409`로 끝난다. Provider 실패 보상도 같은 잠금과 exact lease를 확인하여 다른 처리자의 terminal 상태를 덮지 않는다. `guides.py`는 coordinator가 확정한 STALE/실패를 다시 `mark_failed()`로 덮지 않는다.
- 일반 A 준비 실패는 Guard/Job을 포함한 A 전체를 rollback한 뒤 별도 transaction으로 실패 Guide만 기록한다. 명시적 evaluator FAIL만 감사와 실패 Guide를 commit하며 실행기는 호출하지 않는다. 취소·프로세스 중단 후 자동 재실행/복구는 이번 구현에 없다.

공개 API·DTO·DB schema·migration·의존성·Outbox는 추가하지 않았다. 실제 Source/환자 정책 evaluator, CR-B identity 공급, CR-C 승인 verifier, LangGraph 및 durable 비동기 실행은 이 합성 검증에 포함하지 않는다.

## 동기 기준 수치

최종 코드, Python 3.13.14, PostgreSQL 17.11에서 측정했다. 단위는 ms이며 HTTP는 in-process 애플리케이션 전체 시간이다.

| 대역 지연 | HTTP p50 | HTTP p95 | 접수 A p50 | 실행 B p50 | 최종화 C p50 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 176.42 | 207.46 | 72.27 | 0.04 | 72.39 |
| 500 | 847.60 | 951.79 | 104.21 | 501.29 | 191.73 |
| 2000 | 2388.81 | 2791.74 | 136.89 | 2001.23 | 197.80 |

조건별 warmup 5회 + 측정 20회, 동시성 1이다. 세 조건은 0→500→2000ms 순서로 측정했다. DB·캐시·실행 환경의 변동을 분리한 실험이 아니므로 조건 간 차이를 정확한 고정 overhead로 해석하지 않는다. 0ms 조건의 p50 약 176ms는 이 합성 요청 경계의 기준값이며 실제 RAG 생성 시간·운영 응답 SLA 또는 비동기 성능이 아니다.

원본 JSON은 로컬 `.omx/plans/guide-sync-cra-20261104/benchmark.json`이다. benchmark의 runtime 실행·lifecycle·finalizer 세 파일 결합 SHA256는 `1c9403c37a1a2c2c4efffdb7c94e9c258f6a70a5ae97e1f41c607754f1988526`이다. 별도 전체 선택 파일 hash는 같은 디렉터리의 evidence에 보관한다.

## 검증

| 검사 | 결과 |
| --- | --- |
| 오프라인 단위·wiring·계약·release·인용/안전·OpenAI 오류 대역 회귀 | **217 passed** |
| 실제 PostgreSQL CR-A 통합 전체 최초 실행 | **13 passed**, benchmark opt-in 1 skipped |
| 실제 PostgreSQL 추가 pin 변경 회귀 | **3 passed**, 별도 실행 9.04s; JUnit 미생성 |
| 기존 동기 API wiring/저장 회귀, 독립 schema | **1 passed** |
| 대역 동기 benchmark | **1 passed**, warmup 15 + 측정 60 requests |
| 관련 파일 Ruff check/format | **16 files**, 통과 |
| 관련 source/benchmark Mypy | **8 files**, 통과 (`--follow-imports=silent`) |
| DEV foundation manifest validation | COMPLETED, 오류 없음; N/A / release_eligible=false |
| diff whitespace·문서 상대 링크 | 통과 |

추가 3종은 실제 새 처방 Version, DB CHECK를 만족하는 Identification UNRESOLVED 전환, Execution Manifest의 canonical 입력 `worker_artifact_ref` 변경이다. 각각 HTTP409, Guide STALE/is_current=false/content=NULL/citations=0, Job STALE을 확인했다. manifest hash와 Bundle→manifest 포인터의 직접 변경은 기존 FK가 차단하므로 허용되는 canonical 입력 drift로 검증했다.

**16건을 한 번에 다시 실행한 검사는 미완료다.** 처음 2건 이후 전용 PostgreSQL이 연결/SSL handshake에도 응답하지 않아 멈춘 자기 테스트 프로세스만 종료했다. 같은 생산 코드의 이전 13건과 별도 신규 3건 결과를 보존하며 이를 단일 16건 PASS로 표기하지 않는다. 로컬 XML은 이전 13건 receipt다. 저장소 전체 suite·운영 DB migration replay·실제 Source/Provider·비동기·holdout/임상 평가는 실행하지 않았다.

최종 수치는 위 표에 기록했다. PostgreSQL 통합 검증은 기존 DB와 분리한 합성 전용 PostgreSQL 17/pgvector 컨테이너에서 각 테스트가 소유한 `cra_<uuid>` schema만 만들고 삭제했다. 일반 테스트 실행은 명시적 `DOSEY_CRA_TEST_ALLOWED=1`이 없으면 이 독립 fixture를 건너뛰며, opt-in 후 포트/계정이 전용 구성과 다르면 실패한다. 기존 public schema를 drop하거나 기존 서비스 DB를 사용하지 않았다.

오프라인 검사 harness는 `RELEASE_VALIDATION_RUNNER=1`과 합성 설정으로 실제 `.env` 로딩을 차단하고 모든 socket 연결을 거부했다. DB 통합 fixture는 전용 localhost PostgreSQL 연결만 허용했다. consent gate·정책 evaluator·executor는 테스트 대역이며 실제 인증/동의 원장이나 의료 판단 품질을 검증했다고 해석하지 않는다. OpenAI SDK client와 legacy generator도 대역으로 치환했다. 실제 OpenAI 호출은 0회다.

DEV foundation 평가 manifest validation은 `COMPLETED`, `error_codes=[]`, `decision_status=N/A`, `release_eligible=false`였다. 이는 schema/hash/provenance 검증이며 생성 품질·holdout·Release PASS가 아니다. 기존 인용·안전·release 회귀는 선택 테스트로 별도 실행했다.

독립 내부 검토에서 부분 접수 commit, PROCESSING lease 누락, identity map 재사용, 탈퇴 경쟁과 늦은 실패 덮어쓰기 문제를 수정하고 최종 재검토에서 추가 BLOCKING/HIGH가 없음을 확인했다. 외부 Claude bridge는 선택 파일 dry-run에 성공했으나 실제 CLI가 exit 1로 종료해 유효한 외부 보고서를 얻지 못했다. 외부 검토 통과로 표시하지 않는다.

## 측정 방법과 재현

`scripts/benchmark_guide_sync_cra.py`와 `tests/integration/rag/test_guide_sync_cra.py`의 명시적 benchmark opt-in을 사용한다. Python 3.13의 임시 환경은 기존 lock의 app/worker/dev 그룹을 사용했으며 저장소 lock을 변경하지 않았다.

```bash
# 합성 전용 DB와 .env 차단 설정을 구성한 뒤, 별도 DB autouse conftest 없이 실행한다.
DOSEY_CRA_TEST_ALLOWED=1 CRA_TEST_DB_PORT=55217 DOSEY_CRA_BENCHMARK=1 \
  /private/tmp/dosey-cra-20261104-env/bin/python \
  .omx/plans/guide-sync-cra-20261104/run_pg_checks.py \
  tests/integration/rag/test_guide_sync_cra.py -k benchmark
```

단일 실행기·동시성 1, 조건별 warmup 5회와 측정 20회, 지연 0/500/2000ms로 총 75개 요청이다. HTTP는 in-process ASGITransport이며 DB는 실제 PostgreSQL이다. 외부 HTTP 왕복·프록시·네트워크·queue·검색·embedding·실제 LLM을 포함하지 않는다. p50/p95는 조건별 20개 raw sample의 선형 보간이다. A 시간은 GuideService의 초기 소유권/동의 확인과 Guide 생성 이후 coordinator 진입부터 A commit까지다. HTTP 전체에는 그 밖의 애플리케이션 비용도 포함된다.

이 동기 기준값으로 비동기 접수 응답 시간이나 처리량 개선을 주장하지 않는다. 후속 비동기 비교는 같은 입력·DB·대역 지연·동시성에서 접수 응답과 최종 완료 시간을 따로 측정해야 한다.

로컬 재현 harness·JUnit·raw JSON·소스 SHA256는 `.omx/plans/guide-sync-cra-20261104/`에 보관한다. 이 경로의 자료는 로컬 증거이며 commit된 자료로 간주하지 않는다.

## 테스트 자원 정리의 제한

테스트 종료 후 전용 컨테이너 `dosey-cra-20261104-319521d2`(포트 55217, 전체 ID는 로컬 evidence)에만 정리를 시도했다. Docker CLI의 해당 컨테이너 조회/로그와 로컬 Docker API의 정확한 ID force DELETE 모두 timeout으로 종료해 **컨테이너 제거 완료는 확인하지 못했다**. 멈춘 자기 pytest runner와 해당 컨테이너의 psql용 `docker exec` 클라이언트만 종료했다. Docker Desktop 전체나 다른 컨테이너를 재시작/종료하지 않았다. 전용 DB에는 합성 fixture만 사용했다.
