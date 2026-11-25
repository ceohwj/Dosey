# Candidate Search 합성 내부 연결 검증

- 날짜: 2026-10-23
- 범위: [합성 내부 연결 v1 결정](../governance/decisions/2026-10-23-candidate-search-synthetic-connection.md)
- 상태: 로컬 합성 구현·검증 완료. Current 승격·실제 Source 사용·의료 품질·공개·merge/release 승인 아님.

## 구현과 변경 파일

| 파일 | 역할 |
|---|---|
| [candidate_search.py](../../backend/app/services/candidate_search.py) | 한 transaction의 생성/재사용/최종화, 현재 후보 조회·최초 선택 검증, 기존 성공 replay 순서 보존 |
| [candidate_source_guard.py](../../backend/app/services/candidate_source_guard.py) | explicit TEST context, 실제 Source lifecycle·Snapshot seal/selection·exact approval·freshness·제품 현재성 검증. receipt는 요청 내부 증거이며 영속 REQUEST 권한이 아님 |
| [candidate_index_binding.py](../../backend/app/services/candidate_index_binding.py) | 실제 READY LEXICAL_ONLY UUID artifact 발급, Bundle Catalog/UUID/version/hash 및 기존 저장 검증 |
| [candidate_search_resolution.py](../../backend/app/services/candidate_search_resolution.py) | async hydration→pure Resolver, 보수적 함량 비교, 실제 제품 UUID/official identity 투영, 실제 성분 exact 진단 |
| [라우터](../../backend/app/apis/v1/medication_candidate_routers.py), [DI](../../backend/app/dependencies/services.py) | POST 202 연결과 flag 선검사, 기본 context=None 차단. 새 운영 설정/우회 경로 없음 |
| [합성 통합 테스트](../../tests/integration/rag/test_candidate_search_synthetic_flow.py) | 실제 PostgreSQL·HTTP·철회·동시성·취소·재시도 |
| [합성 DEV fixture](../../evals/candidate/synthetic-search-v1.json), [평가 검사](../../tests/contract/rag/test_candidate_synthetic_eval.py) | 명시적 fixture 정책으로 16개 판정 사례. 임상 정확도/운영 threshold의 승인 자료 아님 |

새 부품별 단위 테스트 4개 파일, 기존 Candidate API/OpenAPI 회귀, 계약 인덱스와 식별·Runtime·멱등 Target을 함께 갱신했다. API 기존 저수준 fixture 테스트는 명시적 base service override로 격리했고, 새 producer의 실제 검증은 별도 합성 통합 사례에서 수행한다. 기존 모델·migration·공통 멱등 구현과 이전 작업 파일은 유지했다. 변경 전 2,591개 파일 hash와 비교해 이번 작업 밖 기존 파일 변경·삭제는 없었다.

## 실행 결과

| 검사 | 결과 | 로컬 실행 증거 |
|---|---|---|
| 단위·계약·기존 Resolver/hydration·합성 평가 | 203 PASS | `outputs/candidate-search-20261023/unit-contract-eval-final.log` |
| 실제 합성 PostgreSQL 전체 흐름 | 15 PASS | `outputs/candidate-search-20261023/synthetic-postgresql-final.log` |
| 기존 Candidate·멱등성·B/C API·동시성·기존 forward migration 회귀 | 142 PASS | `outputs/candidate-search-20261023/postgresql-regression.log` |
| 변경 Python 14개 Ruff/format | PASS | `outputs/candidate-search-20261023/ruff.log`, `format.log` |
| 변경 서비스/DI/라우터 6개 Mypy | PASS | `outputs/candidate-search-20261023/mypy-final.log` |
| test inventory·DB 함수/Trigger/RLS 재도입 검사·문서 경로·diff whitespace | PASS | `outputs/candidate-search-20261023/verification.json` |

총 **360개 테스트**다. 합성 평가 17개 검사는 16개 matrix 사례와 fixture 상태 검사 1개로 구성된다. matrix는 official provenance 불일치, 함량 누락·충돌·단위/숫자 경계, 활성 상태, relevance 경계, 복수 후보, 성분 진단을 확인한다. 생성 답변/의료 Claim/Citation을 반환하지 않는 Candidate 경로이므로 생성·Citation 품질 평가는 적용하지 않았다. 다른 RAG/Guide의 공개 품질 또는 holdout 평가를 통과했다고 해석하지 않는다.

실행은 임시 Python 3.13 환경과 작업 소유 PostgreSQL `127.0.0.1:55439/test`에 한정했다. 실제 앱 컨테이너의 DB `5432`에 쓰기·migration을 실행하지 않았다. 외부 embedding·LLM·Source 수집·Claude·Antigravity를 호출하지 않았다. 이번 작업에는 새 schema/migration이 없으며 기존 migration 회귀만 다시 실행했다.

주요 재현 명령은 다음과 같다. 환경은 위 합성 DB와 `PYTHONPATH=backend:.`, `ENV=local`, `RELEASE_VALIDATION_ALLOWED=false`, `OCR_STRUCTURE_LLM_ENABLED=false`를 사용했다. 접속 자격은 합성 전용이며 실제 앱 설정을 복원하지 않았다.

```sh
python -m pytest -q -p no:cacheprovider tests/integration/rag/test_candidate_search_synthetic_flow.py
python -m pytest -q -p no:cacheprovider tests/contract/rag/test_candidate_synthetic_eval.py
python scripts/ci/check_python_test_inventory.py
python scripts/ci/check_database_logic.py
git diff --check
```

DB fixture는 loopback·DB 이름 `test`·두 포트 설정 `55439`를 검사한다. 공통 fixture의 extension 스키마(`test_extensions`)를 재사용해 기존 API 회귀 후에도 실행할 수 있게 했다. 소유한 임시 컨테이너와 Python 환경은 검증 후 제거하며 로그·hash 증거는 `outputs/`에 남긴다.

## 검증한 불변식

- 실제 canonical Bundle·READY Index/member/storage·Source 사실로 POST 202→GET 200→confirm/reject→성공 replay를 실행한다. HTTP 응답은 no-store이며 flag=false에서 도메인 호출 0회다.
- 일반 DI에는 합성 context가 없으므로 공개 flag 하나로 신규 검색/선택을 활성화할 수 없다. 실제 성공 replay는 context 없이도 저장 응답을 재현하고 HMAC·request hash·암호화 응답·TTL을 변경하지 않는다.
- Source 철회는 현재 GET/최초 선택을 막는다. 생성 중 approval 잠금을 다른 세션의 revoke가 기다리고, commit 후 revoke가 반영된다.
- 같은 약제의 동시 생성은 처방 잠금 뒤 같은 Search를 재사용한다. 환경 revision 또는 합성 policy identity가 달라지면 old Search를 재결속하지 않고 새 Search가 필요하다.
- Index hash, 제품 ACTIVE 상태, 제품 UUID의 공식 Identity 관계가 달라지면 차단한다. 실제 Ingredient exact 행 없이 INGREDIENT_ONLY를 만들지 않으며 양수 lexical 제품 evidence를 성분 진단으로 숨기지 않는다.
- 생성 이후 resolver 취소는 outer transaction에서 Search/Result를 모두 rollback한다. 고아 RUNNING이나 부분 후보가 남지 않는다.

## 독립 검토와 수정

Codex 내부 읽기 전용 검토에서 다음을 발견해 수정했다: Snapshot state seal과 publication 승인 혼동, 함량 숫자 내부 공백 병합, 잠금 재조회 시 ORM 캐시, Bundle과 Index Catalog 불일치, 제품·성분 official Identity FK 의미 검증, member entity type 필터. 실제 PostgreSQL 검증에서 유사도 0 trigram filler 때문에 성분 진단이 가려지는 현상을 재현해 합성 adapter에 한정해 처리했다. 수정 후 통합·계약·평가 검사를 실행했다.

중단/재개로 기존 검토 agent의 마지막 snapshot 재검토는 수행되지 않았고, 최종 diff·hash와 실행 증거는 Integrator가 확인했다. 외부 Claude Code 검수는 이번 파일 packet의 외부 전송을 수행하지 않았으므로 NOT_RUN이다. 내부 검토나 테스트는 Owner의 merge/공개 승인을 대신하지 않는다.

## 남은 운영 범위

실제 Source별 freshness 기준·운영 후보 정책/threshold, production Candidate REQUEST Guard의 영속 저장·FAIL 감사 commit·retention/역할 권한, 모든 관리 writer/계정 삭제와의 전체 lock graph, HYBRID/Provider 및 공개 적격성은 후속 범위다. 이 항목을 임의 기본값·기존 다른 Guard의 PASS로 채우지 않았다. 합성 내부 연결에 필요한 추가 Owner 질문은 없다. 실제 Source 연결을 시작할 때 해당 결정을 확정해야 한다.
