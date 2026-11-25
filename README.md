# 다섯알 (Dosey) — 본인 중심 만성질환 복약관리

개인 프로젝트 목적과 고도화 방향은 [PRD](PRD.md), 전체 문서 지도는 [문서 인덱스](docs/README.md), Agent 운영은 [개발 workflow](docs/ai/workflow.md)를 참고하세요.

다섯알(Dosey)은 고혈압·제2형 당뇨병·이상지질혈증 등 만성질환 사용자가 처방전을 이해하고 자신의 복약 상태와 어려움을 관리할 수 있도록 돕는 서비스입니다. 처방전을 구조화하고, 사용자가 확인한 처방을 기준으로 복약 안내와 질문 응답을 제공하며, 처방 변경이나 임상 판단이 필요한 상황에서는 약사·의료진 확인으로 연결하는 것을 목표로 합니다.

## 프로젝트 배경 및 개인 개발 범위

이 저장소는 [AI-HealthCare-05/AH_05_04](https://github.com/AI-HealthCare-05/AH_05_04) 팀 프로젝트 코드를 바탕으로 정현우(`@ceohwj`)가 개인 프로젝트로 고도화하는 저장소입니다. 팀 전체의 구현을 개인 단독 성과로 표현하지 않으며, 과거 팀 시절의 결정·검증 증빙은 [`docs/archive/`](docs/archive/README.md)에 분리 보관합니다.

- **핵심 담당 및 고도화 영역**: medication retrieval, evidence/citation/provenance 경계, canonical Guide runtime assembly, Backend provider 연동, 한국어 2단계 Reranker 및 에이전트 워크플로우 개선.
- [원본 팀 프로젝트 기여 기록](https://hyunwoo-jung-portfolio.vercel.app/case-studies/healthcare-rag): [PR #919](https://github.com/AI-HealthCare-05/AH_05_04/pull/919), [PR #924](https://github.com/AI-HealthCare-05/AH_05_04/pull/924), [PR #926](https://github.com/AI-HealthCare-05/AH_05_04/pull/926).

## 핵심 사용자 여정

1. 처방전 업로드 및 비동기 OCR(CLOVA OCR + Outbox/Redis Stream Worker)
2. OCR 구조화 초안 확인·수정 및 처방 확정
3. 확정 처방 기반 복약 가이드 및 Q&A 챗봇(OpenAI Responses API)
4. 복약 일정 및 Check-in 관리
5. 미복용 사유에 따른 Barrier/Support 및 병용(OTC) 확인

> **현재 구현과 목표 계약의 구분**: OCR 접수는 `202 Accepted` 비동기 Worker 경로로 동작하며, Guide·Chat 기본 생성 경로는 동기 `201 Created` 계약입니다. 일반 사용자용 RAG·공개 기능 플래그(`PUBLIC_TRACK_F_ENABLED` 등)는 비활성 상태이며, 상세 현재 runtime vs 목표 계약 구분은 [시스템 아키텍처](docs/architecture.md), [AI 파이프라인](docs/ai-pipeline.md), [API 명세](docs/api.md), [계약 인덱스](docs/contracts/README.md)를 확인하세요.

## 의료 안전 원칙

- 사용자가 확인하기 전 OCR 값은 확정 처방이나 AI 안내의 기준으로 사용하지 않습니다.
- AI가 약명·용량·횟수·복용 시점·기간을 임의로 변경하지 않습니다.
- 근거가 없거나 상충하고 검증에 실패한 결과를 안전한 정상 답변으로 해석하지 않습니다.
- 실제 환자정보와 재식별 가능한 처방을 저장소 예시·fixture·로그에 포함하지 않습니다.
- 응급·고위험 가능성은 일반 안내보다 전문가 또는 응급 도움 연결을 우선합니다.

자세한 기준은 [보안 정책](SECURITY.md)과 [개인정보 및 의료 안전](docs/privacy-safety.md)을 따릅니다.

## 기술 스택

- **Frontend**: React, TypeScript, React Router, Vite, 디자인 토큰
- **Backend**: FastAPI, Pydantic, SQLAlchemy asyncio, Alembic
- **Database & Broker**: PostgreSQL 17 (`pgvector`, `asyncpg`), Redis Streams
- **AI Provider**: CLOVA OCR, OpenAI API
- **Quality**: Ruff, Mypy, Pytest, Oxlint, TypeScript build

상세 잠금 버전과 사용 범위는 [기술 스택](docs/technology-stack.md)을 참고하세요.

## 프로젝트 구조

```text
.
├── apps/                       # 실행 가능한 서비스 애플리케이션
│   ├── backend/                # FastAPI API, 모델·저장소, OCR 접수, 동기 Guide·Chat, Alembic
│   ├── ai_worker/              # OCR Outbox·Redis Consumer·복구 및 RAG/평가 구성요소
│   └── frontend/               # React + Vite 사용자 화면
├── packages/                   # 앱들이 공유하는 순수 Python 패키지 (import명 유지)
│   ├── ocr_runtime/            # OCR 엔진·구조화·정규화
│   ├── provider_contracts/     # Provider 공용 계약·관측성·처방 무결성 타입
│   ├── provider_runtime/       # Provider 관측성 런타임
│   └── rag_runtime/            # RAG preflight·근거 권한·바인딩 순수 계층
├── docs/                       # 현행 정본 문서 (archive/에 과거 팀 이력 분리 보관)
├── evals/                      # AI/RAG 평가 데이터셋·매니페스트
├── tests/                      # 계약·통합·마이그레이션·서비스 테스트
├── envs/                       # example.local.env, example.prod.env, .local.env(gitignore)
├── infra/                      # Nginx·DB 역할 프로비저닝·배포 구성
├── scripts/                    # CI·검증·개발 스크립트
└── docker-compose.yml          # 로컬 개발 서비스 구성
```

## 사전 준비 및 환경 설정

- Python 3.13 이상, [uv](https://docs.astral.sh/uv/), Node/pnpm, Docker & Docker Compose

1. Python 의존성 설치:

```bash
uv sync --group app --group worker --frozen
```

2. 로컬 환경변수 파일 준비 (`envs/.local.env`는 `.gitignore`에 포함되어 커밋되지 않습니다):

```bash
cp envs/example.local.env envs/.local.env
```

`envs/.local.env`에서 최소한 다음 값을 로컬 환경에 맞게 채우세요.
- `DB_PASSWORD`: 로컬 PostgreSQL 비밀번호
- `OPENAI_API_KEY`: OpenAI Platform API 키 (`OPENAI_MODEL=gpt-4o`, `OPENAI_EMBEDDING_MODEL=text-embedding-3-large`)
- (OCR 테스트 시) `CLOVA_OCR_INVOKE_URL`, `CLOVA_OCR_SECRET`

Docker Compose와 로컬 Python(`app.core.config.Config`) 모두 저장소 루트의 `envs/.local.env`를 읽도록 통일되어 있습니다. 운영 환경은 `envs/example.prod.env`를 참고해 별도의 `envs/.prod.env`를 준비하세요.

## 실행 방법

### Docker Compose로 전체 스택 실행

```bash
docker compose \
  --env-file envs/.local.env \
  -f docker-compose.yml \
  up -d --build
```

| 서비스 | 역할 | 접속 주소 |
| --- | --- | --- |
| `fastapi` | API 서버 | `http://localhost:8000/api/docs` |
| `postgres` | PostgreSQL 17 | `localhost:5432` |
| `migrate` | Alembic migration | 초기화 후 정상 종료 |
| `redis` | Redis Streams | `localhost:6379` |
| `ai-worker` | OCR Worker | Outbox Publisher·Consumer 실행 |
| `frontend` | Vite 개발 서버 | `http://localhost:5173` |
| `nginx` | 리버스 프록시 | `http://localhost/api/docs` |

### 로컬 Python / Frontend 직접 실행

```bash
# Backend API (저장소 루트에서 실행)
PYTHONPATH=apps/backend:packages:apps:. uv run --env-file envs/.local.env \
  uvicorn --app-dir apps/backend app.main:app --reload

# DB migration
uv run alembic -c apps/backend/alembic.ini upgrade head

# Frontend
cd apps/frontend && pnpm install && pnpm dev
```

## 테스트와 품질 검사

저장소 완료 기준은 [CONTRIBUTING.md](CONTRIBUTING.md)와 [테스트 전략](docs/testing.md)을 따릅니다.

```bash
uv run ruff check .
uv run ruff format . --check
uv run mypy apps/backend/app apps/ai_worker
bash scripts/ci/run_test.sh

cd apps/frontend && pnpm lint && pnpm build
```

문서만 변경한 경우에는 링크·범위 검토와 `git diff --check`를 실행합니다.

## 새 기능 추가 가이드 (반복 워크플로우)

1. `docs/designs/_template/`의 `design.md`, `plan.md`를 `docs/designs/<도메인>/` 아래로 복사해 범위·계약·안전 기준을 먼저 정리합니다.
2. 공유 DTO·상태·오류 의미를 변경할 때는 [`docs/contracts/`](docs/contracts/README.md), 구현, 계약·통합 테스트를 함께 갱신합니다.
3. DB 모델은 `apps/backend/app/models/`에 정의하고 Alembic forward migration을 추가합니다(Trigger/RLS/Stored Procedure 금지).
4. 비밀정보와 실제 환자 정보는 절대 저장소에 커밋하지 않습니다([SECURITY.md](SECURITY.md), [개인정보 및 의료 안전](docs/privacy-safety.md)).
