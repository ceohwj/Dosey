# Candidate Search Runtime 결속과 기존 성공 키 재현

| 항목 | 결정 |
|---|---|
| Decision ID | `PD-DOSEY-20261023-CANDIDATE-RUNTIME-1` |
| 권한·상태 | Human Owner의 2026-10-23 대화 지시로 내부 구현 진행 승인. 공개·운영 DB migration·배포·외부 Source 승인은 포함하지 않음 |
| 직접 지시 | “실제 미만료 키와 Search 생성 경로부터 확인하고, 불필요한 호환 계층은 줄이면서 권고안대로 진행해줘. Bundle이나 환경 revision이 바뀌면 새 Search를 요구하고, 기존 재시도와 기술적 안전 규칙은 유지해줘.” |
| 구현 범위 | 내부 Search snapshot·Candidate adapter 현재성·기존 키 호환·forward migration·검증. 실제 Search 생산자와 Candidate Source Guard 연결은 후속 선행 조건 |
| 정본 | [멱등 Target](../../contracts/targets/post-mvp-1/idempotency-v1.md), [Candidate Target](../../contracts/targets/post-mvp-1/medication-identification-v1.md), [Runtime Target](../../contracts/targets/post-mvp-1/rag-runtime-v1.md) |

## 조사와 적용 경계

현재 저장소에는 `.env`·`envs/.local.env`·`backend/.env`가 없고 DB 환경변수도 없었다. 최초 Docker daemon 연결 실패 뒤 로컬 Docker를 시작했고, 기존 `ah_05_04` Compose 프로젝트의 `postgres` 컨테이너를 찾았다. read-only transaction으로 확인한 **기존 로컬 DB의 Candidate 키는 전체 0건·미만료 0건, Search는 0건**, DB revision은 `180a1b2c3d4e`이다. FastAPI의 DB 대상도 `postgres:5432`이며 인증 설정은 컨테이너에 남아 있다. Compose 라벨의 예전 `/Users/junghyunwoo/PycharmProjects/AH_05_04`와 임시 Compose 파일 원본은 현재 없고, 관련 Projects/PycharmProjects 범위에서 실제 `.env`는 찾지 못했다. 인증값은 출력하거나 파일로 복원하지 않았다. 기존 DB에는 쓰기·migration을 실행하지 않았다. 다른 배포 환경의 키 수는 UNKNOWN이며 이 로컬 0건으로 추정하지 않는다.

`scripts/audit_candidate_runtime.py`는 설정된 로컬 DB 하나에 read-only transaction·timeout을 적용하고 행 수/최대 만료 시각/DB revision만 출력한다. 설정·연결 실패는 `UNAVAILABLE`과 null이며 키·의료 행·암호화 응답·접속 문자열을 출력하지 않는다. 이번 컨테이너 집계는 컨테이너 내부 기존 인증 설정으로 psql을 실행한 별도 read-only 관측이다. 로컬 0건으로 불필요한 물리 버전 컬럼은 추가하지 않고, 다른 배포/구 writer를 단정할 수 없으므로 작은 기존 지문 읽기 경계만 보존한다.

공개 `POST /medication-candidate-searches`는 unavailable stub이고, 내부 `record_candidate_search`의 호출은 테스트에서만 확인됐다. Resolver·Index hydration은 존재하지만 생산자 조합과 Candidate 전용 REQUEST Guard는 연결되지 않았다. Guide의 `GUIDE_SYNC_ANSWER` 권한을 Candidate 권한으로 재사용하지 않는다. 이번 내부 결속 검증은 **Source 승인·Index artifact 대응·Resolver 생산자·공개 적격성 검증을 대신하지 않는다.** 기존 공개/외부 승인 gate는 유지한다.

## 승인 지시에 따른 구현 규칙

1. 새 Candidate 지문은 v2 marker/action/Search ID/Result ID와 **불변 Search의 Bundle ID·manifest hash·환경 ID·환경 revision**을 canonical hash한다. fingerprint 계산을 위해 현재 active Bundle을 읽지 않는다.
2. 미만료 성공 기록을 찾으면 새 지문 또는 기존 placeholder 지문과 정확히 비교해 저장된 status/body를 재현한다. legacy 지문 비교는 Candidate 호출자가 명시적으로 제공할 때 기존 행에서만 허용한다. **신규 기록은 실제 Search 결속을 사용한 v2만 쓴다.** 기존 hash·응답·TTL·HMAC scope는 갱신하지 않는다.
3. 두 지원 지문은 저장 digest와의 정확한 일치로 구별할 수 있으므로 공통 `idempotency_record.fingerprint_version`과 Search의 별도 binding-version 컬럼은 추가하지 않는다. 이는 검토 계획의 버전 컬럼안을 축소한 구현 선택이다. 양쪽 hash가 불일치하면 409이며 legacy 실패 뒤 mutation을 허용하는 fallback은 없다. B·C·비동기 Job은 기존 규칙을 사용한다.
4. 최초 조회, unique 경쟁 후 승자 조회, 도메인 오류 후 승자 조회에서 같은 비교·TTL 규칙을 쓴다. 만료 승자를 재현하지 않고 원래 오류를 전파한다. 실제 만료 후 같은 키는 새 요청 규칙을 따른다.
5. replay 전에 역사적 SELF 소유권과 부모/자식 관계를 확인하되 active Version·verified·READY·현재 Runtime 검증은 신규 mutation 안에서 실행한다. 존재하지 않는/타인/불일치 부모·자식은 404가 우선한다. 공개 기능 gate는 기존처럼 replay보다 먼저 적용된다.
6. 신규 Candidate 확인·거절은 완전한 Runtime snapshot만 받는다. Bundle 또는 환경 revision이 다르면 `409 IDENTIFICATION_CONTEXT_STALE`로 차단하고 새 Search를 요구한다. A→B→A, 같은 Bundle의 suspend/resume 후 revision 변화도 같다. 기존 성공 키는 소유권·TTL·공개 gate가 유효하면 당시 응답을 재현한다.
7. 환경 부재/비활성, active 포인터 불완전, Bundle 누락/상태 부적격/환경 불일치/canonical manifest 불일치는 `503 CANDIDATE_RESOLVER_UNAVAILABLE`이다. 이를 다른 Guard의 PASS로 대신하지 않는다.

## Search 저장과 잠금

`medication_candidate_search`에 nullable `runtime_release_bundle_manifest_hash`, `runtime_environment_id`, `runtime_environment_revision` 세 컬럼을 추가한다. 세 값이 모두 NULL인 역사적 행은 기존 Bundle ID-only를 포함해 그대로 둔다. 새 결속은 세 값과 Bundle ID·Candidate Index UUID가 모두 필요하며 lowercase SHA-256·양수 revision을 CHECK한다. Bundle ID/hash composite FK와 환경 ID FK는 RESTRICT로 둔다. 현재 환경 revision과의 FK는 만들지 않는다. 적용 이력 수정·추정 backfill·trigger/RLS/업무 DB 함수 도입은 없다. 결속된 행이 있으면 downgrade가 정보 손실을 거부한다.

서버 내부 `record_candidate_search(runtime_environment_id=...)`는 기존 처방·활성 Search 잠금 이후 환경 행을 잠그고 실제 active ID/hash/revision과 canonical manifest를 검증해 저장한다. 재사용 비교에 세 snapshot 필드를 포함한다. 환경 입력이 없는 기존 저수준 helper는 역사적 fixture/미완성 내부 경계로 유지하지만, **불완전 Search의 신규 Candidate adapter mutation은 차단**한다. 이를 실제 새 Search API 생산 경로로 표현하지 않는다.

신규 adapter mutation의 실제 잠금 순서는 `Prescription → Search → Runtime Environment`이고 이어 Bundle/Result를 읽고 Identification을 저장한다. PVM/Result는 현재 Repository가 별도 row lock을 취하지 않는 조회이다. 환경 잠금은 외부 transaction commit까지 유지한다. Runtime 전환은 기존 `Environment → Bundle`이며 Candidate는 역순 환경 잠금을 추가하지 않는다. Provider/network 호출을 잠금 구간에 넣지 않는다. Source 승인 writer와 Candidate Guard의 전체 경쟁 프로토콜은 생산자 연결 전 별도 검증이 필요하다.

## 검증과 전환

- 공개 DTO 필드/route/operation ID를 유지하고 오류 의미·우선순위와 내부 schema만 위 정본 Target에 반영한다. Current 승격은 하지 않는다.
- 새 계약 테스트는 실제 공통 멱등 execute와 Runtime 정책을 검증한다. 기존 adapter 테스트의 합성 unbound fixture에서는 Runtime validator만 명시적으로 대체하고, 실제 소유권·선택·상태 검증은 유지한다. 실제 validator의 legacy 차단 사례를 별도로 둔다.
- 새 PostgreSQL migration 테스트와 두 세션 환경 잠금/ABA 테스트를 추가한다. 잠금 전용 테스트의 Bundle/manifest는 test double이며, 별도 confirm/reject 통합 사례는 실제 저장된 canonical Bundle·Search·validator·adapter·멱등 응답을 사용한다. 합성 build를 READY/ACTIVE로 설정하는 fixture는 운영 Source 승인 증거가 아니다. collect 성공을 DB 테스트 PASS로 쓰지 않는다.
- DB/생산자 조사 상태와 실행 결과는 작업 evidence에 기록한다. 이번 로컬 코드 변경으로 운영 DB에 migration을 실행하지 않는다.
- 향후 전환은 schema → 양 지문 reader → 구 writer/in-flight 정리 → v2 writer 순서이다. HMAC 혼합 writer 금지를 유지하며 v2를 모르는 구 바이너리로 rollback하지 않는다. legacy 제거는 **관련 배포 환경 전체의** 미만료 키/구 writer가 없다는 증거 후 별도 작업으로 진행한다.

### 로컬 검증 결과 — 2026-10-23

기존 앱 DB(`5432`)와 분리한 임시 합성 PostgreSQL에서 Candidate·멱등·API·B/C·동시성·신규 migration/실제 Bundle 통합 142개, 별도 단위·계약 43개 실행이 통과했다. 빈 합성 DB의 전체 Alembic upgrade와 실제 head 종합 검증도 통과했다(`924a1b2c3d4e`, Trigger/RLS/제거 대상 함수 0개). Mypy 947개 소스, 변경 Python 16개 Ruff/format, 문서 경로 172개, test inventory와 `git diff --check`가 통과했다. 독립 검토의 최초 통합 테스트 누락 BLOCKING은 실제 confirm/reject·replay·revision staleness PostgreSQL 사례 추가와 실행 후 해소됐다. 이는 기존 앱 DB migration·운영 Source 승인·의료 생성 eval·외부 검토·merge/release 완료를 의미하지 않는다. 집계·실행 명령·파일 hash evidence는 로컬 `outputs/candidate-bundle-20261023/`에 기록한다.
