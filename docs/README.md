# 프로젝트 문서

프로젝트의 설계·기술 계약·검증 근거는 이 디렉터리에서 관리합니다. 개인 프로젝트 목적은 [PRD](../PRD.md), Agent 운영은 [Codex workflow](ai/workflow.md), 작업 명세는 [템플릿](ai/task-template.md)을 참고합니다.

## 문서 구성

주제별 정본과 기록은 다음 위치에서 찾습니다.

- `architecture.md`: 시스템 구성과 서비스 간 데이터 흐름
- [technology-stack.md](technology-stack.md): 실제 의존성·잠금 버전·코드 사용 범위
- `api.md`: API 명세와 요청·응답 예시
- `data-schema.md`: ERD, 테이블 및 주요 데이터 구조
- `ai-pipeline.md`: 비동기 OCR·기본 동기 Guide/Chat, 제한된 RAG 경로와 전체 목표 흐름
- `deployment.md`: 개발·운영 환경과 배포 절차
- `privacy-safety.md`: 개인정보와 의료 안전 기준
- `testing.md`: 현재 자동 검증 범위와 Post-MVP E2E·AI 평가 전략
- `contracts/`: 현재 구현 계약과 승인된 Post-MVP-1 목표 계약. 상태 구분은 [계약 인덱스](./contracts/README.md)를 확인합니다.
- `backend/`: Backend 내부 구현 가이드(DI Provider·Repository 테스트 패턴 등). 공유 계약이 아니라 구현 방식 참고용입니다.
- `adr/`: 주요 아키텍처 결정 기록
- `governance/`: 문서 권위, 상태와 원본 provenance
- `release-gates/`: 구현과 분리된 외부 승인·공개 조건
- `validation/`: 합성 데이터 기반 release validation 실행·정리 절차와 비민감 결과 양식
- `testing/`: 목표 계약 추적표와 구현 증빙 인덱스
- [designs/](designs/README.md): 기능별 상세 설계·구현 계획과 이전 경로 대응표
- `ai/`: Agent 운영·모델 설정 설명·작업 명세 템플릿

외부 문서 도구를 사용한다면 문서의 원본 링크와 최종 갱신일을 이 파일에 기록합니다.

기존 링크 8건의 수정 근거와 규범 참조 재지정을 보류한 항목은 [링크 복구 기록](governance/link-repair-record-2026-10-23.md)에 정리합니다.

## 외부 원본

- [Post-MVP-1 착수 전 게이트](https://app.notion.com/p/3c3233603e2780a7bcc2ff86de5abb74) — 최종 동기화: 2026-08-23
- [Post-MVP-1 세부 결정사항](https://app.notion.com/p/3d7d841cc6c0444399e6e20037a2fd5d) — 최종 동기화: 2026-08-23

## 작성 원칙

- 실제 환자 정보나 원본 진료기록을 포함하지 않습니다.
- API Key, 비밀번호, 토큰 등 인증정보를 포함하지 않습니다.
- 코드 또는 API가 변경되면 관련 문서도 같은 Pull Request에서 갱신합니다.
- 현재 구현, schema-only 골격과 Post-MVP 목표를 같은 상태로 표현하지 않습니다.
- Post-MVP-1 상태 해석과 원본 충돌 처리는 [문서 권위 규칙](./governance/post-mvp-1-document-authority.md)을 따릅니다.
- `.superpowers/`와 `docs/superpowers/`는 로컬 작업 초안 경로이며 Git에 올리지 않습니다. 검토할 설계와 구현 계획은 `docs/designs/`의 관련 기능 문서에 연결합니다. 작성자별 기존 폴더에는 보호 계약·코드·테스트 참조 때문에 경로를 유지한 예외가 있으며 현재 Agent 소유권을 뜻하지 않습니다. 이동·보존 내역은 [설계 인덱스](designs/README.md)에서 확인합니다. 후속 이동도 manifest와 참조 검증을 거칩니다.

## 개발 에이전트 연속성

- [시작·복구·종료 workflow](ai/workflow.md)와 [task 템플릿](ai/task-template.md)
- [지식 인덱스](ai/knowledge-index.md)와 [실패 기록 안내](failures/README.md)
- [검증 기록](testing/agent-workflow-20261111.md)과 [시각 보고서](testing/agent-continuity-20261111.html)
