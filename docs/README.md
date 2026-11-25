# Dosey 프로젝트 문서

설계·기술 계약·운영 가이드를 현행 정본과 과거 팀 이력으로 분리해 관리합니다. 개인 프로젝트 목적은 [PRD](../PRD.md), Agent 운영은 [개발 workflow](ai/workflow.md), 작업 명세는 [작업 템플릿](ai/task-template.md)을 참고합니다.

## 현행 정본 문서 (Current Canonical)

### 아키텍처 · API · 스키마
- [architecture.md](architecture.md): 시스템 구성과 서비스 간 데이터 흐름
- [technology-stack.md](technology-stack.md): 실제 의존성·잠금 버전·코드 사용 범위
- [api.md](api.md): API 명세와 요청·응답 예시
- [data-schema.md](data-schema.md): ERD, 테이블 및 주요 데이터 구조
- [ai-pipeline.md](ai-pipeline.md): 비동기 OCR·동기 Guide/Chat·제한된 RAG 경로와 목표 흐름

### 계약 · 설계 · 결정
- [contracts/](contracts/README.md): 현재 구현 계약(`current/`)과 목표 계약(`targets/`), 제안안(`proposed/`)
- [designs/](designs/README.md): 도메인별 설계·구현 계획 및 새 기능 반복 템플릿(`designs/_template/`)
- [adr/](adr/README.md): 아키텍처 및 주요 기술 결정 기록(`decisions/` 통합)
- [backend/](backend/backend-implementation-patterns.md): Backend 내부 구현 가이드(DI Provider·Repository 테스트 패턴)

### 운영 · 보안 · 검증 기준
- [deployment.md](deployment.md): 개발·운영 환경과 배포 절차
- [runbooks/](runbooks/): 운영·장애 대응 런북(`operations/` 통합)
- [privacy-safety.md](privacy-safety.md): 개인정보와 의료 안전 기준
- [testing.md](testing.md): 현재 자동 검증 범위와 Post-MVP E2E·AI 평가 전략
- [release-gates/](release-gates/): 구현과 분리된 외부 승인·공개 조건

### 개발 에이전트 운영
- [ai/workflow.md](ai/workflow.md) · [ai/task-template.md](ai/task-template.md): 시작·복구·종료 workflow 및 작업 명세
- [ai/model-routing.md](ai/model-routing.md) · [ai/knowledge-index.md](ai/knowledge-index.md): 모델 라우팅 및 지식 인덱스
- [failures/](failures/README.md): 실패·기각 기록 안내

## 과거 팀 이력 아카이브 (`archive/`)

과거 팀 프로젝트([AI-HealthCare-05/AH_05_04](https://github.com/AI-HealthCare-05/AH_05_04)) 시절의 의사결정 로그, 검증 리포트, 이슈별 테스트 증빙은 [archive/README.md](archive/README.md) 아래에 분리 보관합니다.

- `archive/decisions/`: 일자별 팀 의사결정 기록 (`governance/decisions/`에서 이관)
- `archive/validation/`: 릴리스·RAG·Track B 검증 리포트 및 JSON fixture (`validation/`에서 이관)
- `archive/testing/`: 이슈별 테스트 증빙, 계약 추적표, DB 로직 baseline (`testing/`에서 이관)
- [governance/](governance/): 문서 권위 규칙 및 링크 복구 기록

## 작성 원칙

- 실제 환자 정보나 원본 진료기록을 포함하지 않습니다.
- API Key, 비밀번호, 토큰 등 인증정보를 포함하지 않습니다.
- 코드 또는 API가 변경되면 관련 문서도 같은 변경에서 함께 갱신합니다.
- 현재 구현, schema-only 골격과 Post-MVP 목표를 같은 상태로 표현하지 않습니다.
- Post-MVP-1 상태 해석과 원본 충돌 처리는 [문서 권위 규칙](governance/post-mvp-1-document-authority.md)을 따릅니다.
- `.superpowers/`, `docs/superpowers/`, `.omc/`, `.omx/`는 로컬 작업 산출물 경로입니다. 검토가 끝난 설계와 구현 계획은 `docs/designs/_template/` 형식으로 `docs/designs/<도메인>/`에 정리합니다.
