# Chat RAG 로컬 PostgreSQL/pgvector 설정 및 샌드박스 검증 기록

- 날짜: 2026-11-26
- 작업 ID: `local-rag-db-setup`
- 기준 리비전: `3297d83`
- 상태: `BLOCKED` (샌드박스 내 Docker daemon 소켓 접근 불가 — 실컨테이너 기동·마이그레이션·확장 확인은 `NOT_RUN`)
- 보안 원칙: 이 문서는 `docker-compose.yml`과 `envs/example.local.env`만 참조했으며, `envs/.local.env`는 읽거나 출력하지 않았다.

## 1. `docker-compose.yml` `postgres` 서비스 및 `envs/example.local.env` 확인 결과

`docker-compose.yml`의 `postgres` 서비스 정의와 `envs/example.local.env`의 PostgreSQL 기준 설정은 다음과 같다.

| 항목 | `docker-compose.yml` 정의 | `envs/example.local.env` 예시 값 | 비고 |
| --- | --- | --- | --- |
| 이미지 | `pgvector/pgvector:0.8.6-pg17-bookworm` | — | PostgreSQL 17 + pgvector 0.8.6 |
| 컨테이너 이름 | `postgres` | — | `container_name: postgres` 고정 |
| 재시작 정책 | `always` | — | — |
| 노출 포트 | `"${DB_EXPOSE_PORT}:5432"` | `DB_EXPOSE_PORT=5432`, `DB_PORT=5432` | 컨테이너 내부 포트 `5432` |
| 호스트 | — | `DB_HOST=postgres` | Compose 내부 네트워크 `ws` 기준 |
| 데이터베이스 | `POSTGRES_DB: ${DB_NAME}` | `DB_NAME=five_pills` | — |
| 사용자 | `POSTGRES_USER: ${DB_USER}` | `DB_USER=app_user` | 개발환경 DB owner |
| 비밀번호 | `POSTGRES_PASSWORD: ${DB_PASSWORD}` | 플레이스홀더(`replace-with-local-db-password`) | 실제 값은 로컬 `envs/.local.env` 전용 |
| 인코딩 / 타임존 | `POSTGRES_INITDB_ARGS: "--encoding=UTF8"`, `TZ: Asia/Seoul`, `PGTZ: Asia/Seoul` | `TIMEZONE=Asia/Seoul` | — |
| 볼륨 / 네트워크 | `postgres_data:/var/lib/postgresql/data`, `ws` (`bridge`) | — | Compose 프로젝트별 볼륨/네트워크 격리 |
| 헬스체크 | `pg_isready -U ${DB_USER} -d ${DB_NAME}` (`interval: 10s`, `timeout: 5s`, `retries: 10`, `start_period: 5s`) | — | 실제 대상 DB 연결 가능 여부 확인 |

주의할 점:
- `docker-compose.yml`에 `container_name: postgres`가 명시되어 있다. 따라서 `-p dosey-rag-eval`로 볼륨/네트워크를 격리하더라도 동일 호스트에 `postgres`라는 이름의 컨테이너나 `DB_EXPOSE_PORT`(`5432`) 점유 프로세스가 이미 실행 중이면 충돌할 수 있다(기존 `ah_05_04` 리소스는 건드리지 않는다).
- `apps/backend/alembic/env.py`는 `apps/backend/app/core/config.py`의 `database_url`(`DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`)을 읽으며 `env_file=("envs/.local.env", ".env")`를 사용한다. `envs/.local.env`에 `DB_HOST=postgres`가 들어 있는 경우 호스트에서 직접 `alembic`을 실행하려면 `DB_HOST=localhost`를 명시적으로 덮어써야 한다.

## 2. 샌드박스 내 실행 결과

### 2.1 `knowledge_evidence_index --help` CLI 파싱 스모크 (`PASS`)

실행 명령:

```bash
PYTHONPATH=apps:packages:. uv run --no-sync python -m ai_worker.admin.knowledge_evidence_index --help
```

관측된 출력(exit code `0`):

```text
usage: knowledge_evidence_index.py [-h] --expected-item-seq EXPECTED_ITEM_SEQ
                                   --expected-canonical-checksum EXPECTED_CANONICAL_CHECKSUM
                                   [--expected-source-version EXPECTED_SOURCE_VERSION]
                                   --evidence-key-binding EVIDENCE_KEY_BINDING
                                   [--verify-replay]
                                   snapshot_id

Build immutable Knowledge Evidence Index for verified MFDS Source Snapshot

positional arguments:
  snapshot_id           Target Source Snapshot UUID

options:
  -h, --help            show this help message and exit
  --expected-item-seq EXPECTED_ITEM_SEQ
                        Expected 9-digit MFDS item sequence (e.g. 200610660)
  --expected-canonical-checksum EXPECTED_CANONICAL_CHECKSUM
                        Expected 64-character SHA-256 canonical checksum
  --expected-source-version EXPECTED_SOURCE_VERSION
                        Expected authoritative source version string
  --evidence-key-binding EVIDENCE_KEY_BINDING
                        Opaque authoritative binding in
                        CHUNK_UUID=EVIDENCE_KEY form; repeat once per chunk
  --verify-replay       Verify exact replay in an immediately following
                        execution
```

### 2.2 Alembic 단일 head 검증 (`PASS`)

실행 명령:

```bash
PYTHONPATH=apps:packages:. uv run --no-sync python scripts/ci/verify_database_head.py --heads-only
```

관측된 출력(exit code `0`):

```text
Alembic head 단일 확인 (925a1b2c3d4e)
```

### 2.3 `docker info` 데몬 상태 확인 (`BLOCKED`)

실행 명령:

```bash
docker info 2>&1 || true
```

관측된 출력:

```text
WARNING: Error loading config file: open /Users/junghyunwoo/.docker/config.json: operation not permitted
Client:
 Version:    28.4.0
 Context:    default
 Debug Mode: true

Server:
permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock: Get "http://%2Fvar%2Frun%2Fdocker.sock/v1.51/info": dial unix /var/run/docker.sock: connect: operation not permitted
```

샌드박스 환경에서 `~/.docker/config.json` 및 `/var/run/docker.sock` 접근이 `operation not permitted`로 차단되어 Docker daemon에 연결할 수 없다.

## 3. 미실행(`NOT_RUN`) 항목

Docker daemon 접근이 샌드박스에서 `BLOCKED` 상태이므로 아래 실시간 검증은 실행하지 않았다.

| 검증 항목 | 상태 | 사유 |
| --- | --- | --- |
| `docker compose -p dosey-rag-eval up -d postgres` | `NOT_RUN` | 샌드박스 내 `/var/run/docker.sock` 접근 불가(`BLOCKED`) |
| `alembic upgrade head` (로컬 PostgreSQL 적용) | `NOT_RUN` | 대상 PostgreSQL 컨테이너 미기동 |
| PostgreSQL `vector` 및 `pg_trgm` 확장 설치/버전 확인 | `NOT_RUN` | 대상 PostgreSQL 컨테이너 미기동 |

## 4. Human Owner / Integrator용 샌드박스 외부 재현 명령

기존 `ah_05_04` 컨테이너 및 볼륨을 건드리지 않고 격리된 Compose 프로젝트명 `-p dosey-rag-eval`로 실행하기 위한 복사-실행용 명령은 다음과 같다.

```bash
# 1) 격리된 Compose 프로젝트(-p dosey-rag-eval)로 postgres만 기동 및 헬스체크 대기
docker compose -p dosey-rag-eval --env-file envs/.local.env up -d --wait postgres

# 2) Alembic 마이그레이션 적용 (envs/.local.env의 DB_HOST=postgres를 호스트 접속용 localhost로 덮어씀)
#    DB_EXPOSE_PORT를 5432 외 값으로 설정했다면 DB_PORT도 해당 포트로 맞춘다.
DB_HOST=localhost PYTHONPATH=apps:packages:. uv run --no-sync alembic -c apps/backend/alembic.ini upgrade head

# 3) 현재 DB 마이그레이션 리비전 확인 (예상 단일 head: 925a1b2c3d4e)
DB_HOST=localhost PYTHONPATH=apps:packages:. uv run --no-sync alembic -c apps/backend/alembic.ini current

# 4) vector 및 pg_trgm 확장 확인 (비밀번호를 명령줄에 노출하지 않고 컨테이너 내부 env 사용)
docker compose -p dosey-rag-eval --env-file envs/.local.env exec -T postgres \
  sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT extname, extversion FROM pg_extension WHERE extname IN ('\''vector'\'', '\''pg_trgm'\'') ORDER BY extname;"'
```

종료 시 `dosey-rag-eval` 프로젝트 리소스만 정리하려면(데이터 볼륨 보존 시 `-v` 제외):

```bash
docker compose -p dosey-rag-eval --env-file envs/.local.env stop postgres
```
