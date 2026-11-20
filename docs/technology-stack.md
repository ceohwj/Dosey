# 기술 스택 — 현재 저장소 기준

확인일: 2026-10-23. 이 문서는 기술·버전·코드 사용 범위를 찾는 목록이다. 데이터 흐름은 [아키텍처](architecture.md), AI 경계는 [파이프라인](ai-pipeline.md), 실제 환경값과 배포 증거는 [배포 가이드](deployment.md)가 정본이다. 패키지 설치나 코드 존재가 기능 완료·운영 활성화·공개 승인을 뜻하지 않는다.

## 버전 근거

- Python 허용 범위와 의존성 그룹: [pyproject.toml](../pyproject.toml). Python 패키지의 정확한 해석 버전: [uv.lock](../uv.lock).
- Frontend의 허용 범위·실행 명령: [package.json](../frontend/package.json). 정확한 설치 해석: [pnpm-lock.yaml](../frontend/pnpm-lock.yaml). 예를 들어 TypeScript 선언은 `~6.0.2`지만 잠금 버전은 `6.0.3`이다.
- 아래 패키지 버전은 잠금 파일 기준이다. Python·Node·컨테이너 값은 별도로 표기한 설정/이미지 기준이며 현재 서버의 실제 설치 버전은 조사하지 않았다.

## 서비스와 개발 스택

| 영역 | 기술·버전 | 코드에서의 용도·근거 |
| --- | --- | --- |
| Backend 언어·서버 | Python `>=3.13` 허용, CI/이미지 `3.13`; FastAPI `0.128.0`, Uvicorn `0.40.0` | REST API·OpenAPI, [Backend 이미지](../backend/app/Dockerfile), [앱 진입점](../backend/app/main.py) |
| 입력·설정 검증 | Pydantic `2.12.5`, pydantic-settings `2.12.0` | DTO·환경 설정·엄격한 입력 검증 |
| 영속 저장 | PostgreSQL `17`, SQLAlchemy asyncio `2.0.52`, asyncpg `0.31.0`, Alembic `1.19.1` | Service/Repository transaction, ORM·migration. [모델](../backend/app/models/), [migration](../backend/alembic/) |
| 벡터 검색 확장 | Compose 이미지 `pgvector/pgvector:0.8.6-pg17-bookworm`, Python pgvector `0.5.0` | PostgreSQL 기반 dense 검색. 서버 확장 버전과 Python 클라이언트 버전은 별개다 |
| 작업 큐 | Redis Streams·Consumer Group, Python Redis `7.1.0` | OCR의 Job·Outbox·Publisher·Worker 처리. [Worker 진입점](../ai_worker/main.py), [조립](../ai_worker/core/runtime_assembly.py) |
| 인증·암호 도구 | PyJWT `2.10.1`, Passlib `1.7.4`, cryptography | 인증 토큰·암호화 지원, 새 비밀번호 Argon2 및 기존 bcrypt 검증. [암호 처리](../backend/app/core/utils/security.py), [보안 정책](../SECURITY.md)을 따른다 |
| Frontend | React/React DOM `19.2.8`, TypeScript `6.0.3`, React Router `7.18.2` | SPA 화면·타입·라우팅. [라우터](../frontend/src/routes/AppRouter.tsx) |
| 화면 상태·API·스타일 | React hooks, Fetch API, CSS·디자인 토큰 | [API client](../frontend/src/api/client.ts), [tokens](../frontend/src/design-system/tokens.ts), [공통 CSS](../frontend/src/design-system/prototype.css) |
| Frontend 빌드 | Vite `8.2.1`, Node `24.x`, pnpm `11.21.0` | `tsc -b && vite build`; [개발 이미지](../frontend/Dockerfile), [배포 이미지](../frontend/Dockerfile.prod) |
| 알림 | pywebpush `2.3.0`, 브라우저 Push API·Service Worker | [브라우저 연결](../frontend/src/features/push/webPush.ts), [알림 검증 범위](testing.md#469-web-push-검증). 실제 기기 수신 증거는 별도다 |
| 인프라 | Docker·Compose, Nginx, TLS/Certbot 구성 | [로컬 Compose](../docker-compose.yml), [배포 Compose](../infra/docker/docker-compose.prod.yml). Redis는 `redis:alpine`, 로컬 Nginx는 `nginx:latest`, Frontend 배포 이미지는 `nginx:1.27-alpine`을 사용하므로 모두 동일한 수준의 고정 버전이라고 쓰지 않는다 |
| 검증·CI | pytest `9.0.2`, Ruff `0.14.14`, Mypy `1.19.1`, GitHub Actions | unit·contract·migration·integration, lint·typecheck·안전 검사. [CI](../.github/workflows/checks.yml), [테스트 전략](testing.md) |
| Frontend 검증 | Vitest `3.2.7`, Testing Library, jsdom, Playwright `1.63.0`, Oxlint `1.78.0` | 컴포넌트·브라우저 E2E·접근성/화면 검증 및 lint |

## OCR·LLM·RAG의 구현 범위

| 영역 | 사용 기술 | 현재 확인한 코드 경계 |
| --- | --- | --- |
| OCR 기본 처리 | CLOVA OCR, 공용 Python 규칙 구조화 | API가 Job·Outbox를 접수하고 Redis Stream의 Worker가 처리한다. `202`는 비동기 접수이며 사용자 검수·확정은 별도다 |
| OCR LLM 조립 | `gpt-4o-mini` 설정, OpenAI SDK | Worker에 feature flag·동의 gate·LLM 구조화기 조립 경로가 있다. 기본 flag는 `false`다. 현재 공용 [구조화기](../ocr_runtime/llm/structurer.py)는 승인된 최소전송 selector가 연결되기 전 Provider를 호출하지 않고 규칙 결과와 `SKIPPED_MINIMIZATION`을 반환한다. flag만 켜면 OCR 텍스트가 LLM으로 전송된다고 설명하지 않는다 |
| Guide·Chat 기본 생성 | OpenAI SDK `3.3.0`, Responses API, 서비스 모델 `gpt-4o` | FastAPI 요청 안에서 동기 생성·검증·저장한다. [Config](../backend/app/core/config.py)의 `OPENAI_MODEL` 기본값과 validator 기준이며 실제 배포값·외부 전송 승인은 별도다 |
| Embedding | OpenAI `text-embedding-3-large`, `1536`차원 adapter 계약 | [adapter](../ai_worker/adapters/openai_text_embedding.py). 검색용 모델이며 Guide·Chat 생성 모델과 다르다 |
| 검색·결합 kernel | PostgreSQL exact·pg_trgm·FTS·pgvector dense 검색, 결정적 RRF | [검색 adapter](../ai_worker/adapters/postgresql_evidence_search.py), [검색 계약 구현](../ai_worker/tasks/rag/evidence_search.py). Source·승인·현재성·Citation 검증과 결합할 구성요소다 |
| 제한된 Guide·Chat RAG | 별도 CLOSED_DEMO 조립과 설정 | [lifespan](../backend/app/main.py)에 조건부 초기화 경로가 있다. 기본 비활성화이며 환경·허용 대상·기간·공개 flag 조건과 승인 기록을 따른다. [Guide 경계 결정](governance/decisions/2026-09-21-guide-closed-demo-rag-provider-boundary.md)의 승인 대기 상태도 유지한다 |
| 일반 공개 RAG·Citation·Safety | 상태별 계약과 후속 통합 대상 | 구현 조각·제한 경로를 일반 사용자용 전체 RAG 완료나 Release PASS로 합산하지 않는다. [계약 인덱스](contracts/README.md)와 [외부 게이트](release-gates/post-mvp-1-external-approvals.md)를 확인한다 |

서비스의 `gpt-4o`·OCR의 `gpt-4o-mini`와 개발 Agent의 Codex 모델 설정은 서로 다른 사용처다. Agent 역할·모델 목록은 [model-routing](ai/model-routing.md), Claude Code 검토·Antigravity 수동 작업은 [workflow](ai/workflow.md)를 따른다.

## 설치·계획과 실제 사용의 구분

- `ai` 의존성 그룹에는 PyTorch·torchvision·torchaudio·sentence-transformers·scikit-learn이 있다. [Worker 이미지](../ai_worker/Dockerfile)는 이 그룹을 설치하지만, 현재 서비스 모듈에서 이 라이브러리를 직접 소비하는 경로는 이번 조사에서 확인하지 못했다. 모델 학습·로컬 embedding이 실제 운영된다고 주장할 근거로 삼지 않는다.
- LangGraph는 [RAG Runtime target](contracts/targets/post-mvp-1/rag-runtime-v1.md)에 있는 목표 기술이다. 현재 잠금 의존성이나 실행 구현으로 확인되지 않았으므로 현재 실행 스택으로 표시하지 않는다.
- boto3와 private S3 artifact adapter가 있어도 실제 운영 bucket·IAM·Source 이용 승인·적재 완료가 확인된 것은 아니다. 관련 조건은 배포·Source 계약을 따른다.
- 이 정리는 현행 목록의 문서화다. 런타임·의존성·모델 선택은 변경하지 않았다. 향후 기술 교체는 고도화할 기능·평가 기준·운영 환경을 정한 후 별도 판단한다.
