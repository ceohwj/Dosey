# Guide Runtime Bootstrap 구성 검증과 오프라인 회귀

- 날짜: 2026-11-04 (Asia/Seoul)
- 작업: `RAG-AUTH-20261104`
- 기준: `bcac7835420118e13ee235410c6b9fdd21224b14` + 기존 미커밋 작업 트리.
- 범위: [계약 변경 요청](../contracts/proposed/post-mvp-1/guide-runtime-authority-bootstrap-v1.md)의 기존 bootstrap 검증·자원 정리만 구현. REQUEST issuer/실제 authority 선택 provider·shared carrier 변경은 미구현·결정 전이다.

## 변경 내용

| 파일 | 변경 |
| --- | --- |
| `backend/app/main.py` | 누락 dependency·잘못된 authority resolver·비동기 bootstrap/동기여야 할 executor factory를 등록 전에 거부. AsyncExitStack으로 초기화·종료 실패 시에도 남은 자원 정리 시도 |
| `backend/app/tests_unit/rag/test_guide_runtime_bootstrap.py` | malformed 구성·sync/async 불일치·초기화 실패·개별 close 실패의 단위 회귀 13건 |
| `backend/app/tests/core/test_guide_runtime_provider_wiring.py` | 기존 wiring fixture를 실제 async resolver 형태로 수정. 권한을 발급하는 대역은 아님 |
| `ai_worker/tests/rag/test_openai_guideline_generator.py` | HTTP 401의 기존 결과·비노출 로그 검증 추가. source·API key는 합성 값 |
| `docs/contracts/proposed/post-mvp-1/guide-runtime-authority-bootstrap-v1.md` | CR-A issuer/snapshot·transaction·수명, CR-B identity projection, CR-C 승인 verifier 원장 제안 |
| `docs/contracts/README.md` | 위 제안의 Proposed 상태·이번 구현 범위 연결만 추가. 기존 변경 보존 |

DB 정리·client·Chat·Guide 종료 callback은 생성 후 등록하며 역순으로 실행한다. 하나의 close 예외가 다음 cleanup을 생략하지 않는다. 예외를 성공으로 바꾸거나 활성화 flag를 변경하지 않는다.

## 테스트 우선 검증

1. 최초 8개 신규 bootstrap 테스트가 기존 코드에서 **8 FAIL**. 예상 원인은 잘못된 구성의 등록과 initializer 실패 시 cleanup 생략이었다.
2. 최소 수정 후 신규+기존 wiring **11 PASS**.
3. 독립 검토에서 sync resolver·async executor create 및 close 실패 중단을 추가 지적했다. 새 5개 시나리오를 먼저 실행해 **5 FAIL / 기존 8 PASS**를 확인했다.
4. async/sync 검사와 AsyncExitStack 반영 후 최종 관련 회귀는 아래와 같다.

| 검사 | 결과 |
| --- | --- |
| 신규 bootstrap + 기존 wiring + Backend shared runtime 경계 + Worker provider/composition + OpenAI generation/embedding | **59 PASS, 0 skipped** |
| 변경 Python 4파일 Ruff | PASS |
| 변경 Python 4파일 format check | PASS |
| `mypy --follow-imports=silent backend/app/main.py` | **1 source file PASS**; 전체 Backend typecheck가 아님 |
| Python test inventory | PASS; 새 tests_unit도 기존 자동 수집 범위에 포함 |
| 문서 상대 참조·diff whitespace·선택 diff | 확인 |

## OpenAI가 유효하지 않은 환경의 검증 경계

- 실제 API key나 `.env`를 읽지 않았다. 기존 임시 Python 3.13.14 환경을 읽기 전용으로 사용했고 의존성을 설치·수정하지 않았다.
- 실행 harness는 synthetic `ENV=local`, DB port `1`, 합성 key, runtime/demo/public flags OFF, `RELEASE_VALIDATION_RUNNER=1`로 기존 설정 로더의 real `.env` 읽기를 차단했다.
- socket의 `connect`, `connect_ex`, `create_connection`을 차단했다. **실제 Provider/API 호출은 0회**다.
- 401 검사는 기존 Provider fixture에서 합성 HTTP 401 `APIStatusError`를 발생시킨다. 실제 SDK transport·키 유효성 검사는 아니다.
- 현재 승인된 adapter 의미대로 `VALIDATION_FAILED`만 반환하고 response model provenance가 남지 않는다. 로그에는 허용된 401/`PROVIDER_REQUEST_REJECTED` metadata만 남고 key·Provider 원문 오류·의약 데이터 sentinel은 남지 않는다.
- 인증 실패를 성공으로 바꾸거나 테스트용 generator/승인 verifier를 production 경로에 넣지 않았다.

## 재현과 증거

로컬 작업 증거는 `.omx/plans/rag-authority-bootstrap-20261104/`의 `evidence.json`, JUnit XML, `run_offline_checks.py`에 보관한다. `.omx`는 로컬 산출물이며 이 문서와 달리 commit된 결과물이라고 가정하지 않는다. evidence에는 선택 파일 hash·환경·초기 실패·최종 성공·미실행 범위가 있다.

기존 임시 환경이 남아 있을 때 재현 예시는 다음과 같다. harness는 외부 연결을 차단하고 pytest의 DB autouse conftest를 사용하지 않는 **선택 단위 검사**다. 전체 DB suite를 대체하지 않는다.

```bash
PYTHONDONTWRITEBYTECODE=1 /private/tmp/dosey-policy-20261104/bin/python \
  .omx/plans/rag-authority-bootstrap-20261104/run_offline_checks.py \
  backend/app/tests_unit/rag/test_guide_runtime_bootstrap.py \
  backend/app/tests/core/test_guide_runtime_provider_wiring.py \
  backend/app/tests/services/test_guide_runtime_execution_contract.py \
  ai_worker/tests/rag/test_guide_runtime_provider.py \
  ai_worker/tests/rag/test_guide_runtime_composition.py \
  ai_worker/tests/rag/test_openai_guideline_generator.py \
  ai_worker/tests/rag/test_openai_text_embedding.py
```

## 리뷰·미실행·다음 결정

- 독립 Codex 내부 reviewer가 선택 diff·신규 테스트·계약을 대조했다. 두 MEDIUM 지적을 재현·수정하고 frozen source snapshot 재검토에서 추가 BLOCKING/HIGH/MEDIUM은 없었다. reviewer가 테스트를 실행한 결과는 아니다.
- 외부 Claude 검토, 실제 PostgreSQL/API/Worker 전체 RAG 통합, 실제 Source/Provider/credential smoke, 배포·공개는 **NOT_RUN**이다. 이번에 실제 권한 provider를 완성·활성화했다고 표시하지 않는다.
- CR-A의 issuer/transaction과 request-scoped provider 전달을 결정한 뒤 실제 provider를 구현한다. 동일 user/operation에 있는 임의 Guard를 찾아 재사용하는 동작은 구현하지 않았다.
- CR-B carrier/adapter와 CR-C 승인 원장을 결정한 후 identity resolver·approval verifier를 연결한다. Source evaluator·Patient Context 안전 필터·LangGraph는 후속 범위다.
