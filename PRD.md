# Dosey 개인 프로젝트 방향

상태: 개인 프로젝트 목적 확정, 다음 고도화 기능은 미선정. 이 문서는 미래 제품 범위를 결정하며 현재 기능 완료·배포 승인 기록을 대신하지 않는다.

## 목적

기존 팀 프로젝트를 바탕으로 특정 기능을 고도화하고, 설계 판단·구현·테스트·측정 근거를 설명할 수 있는 개인 포트폴리오로 발전시킨다. 향후 1~2개월의 우선 방향은 기능 고도화와 포트폴리오이며, 실제 사용자 대상 공개는 이 목적만으로 승인되지 않는다.

## 범위 결정

Human Owner가 다음 고도화 기능을 선정한다. 각 기능은 사용자 문제, 포함/제외 범위, 현재 baseline, 목표, 평가 데이터와 재현 명령, 통합 범위, 안전 조건을 먼저 기록한다. OCR·검색·생성·사용자 화면을 모두 동시에 개편하는 것을 기본 범위로 삼지 않는다.

정확도·검색 품질·생성 품질·지연시간 같은 지표는 해당 기능에 적합한 경우만 선택한다. 실제 비교 결과 없이 성능 향상을 주장하지 않는다. 합성/승인 데이터와 baseline·candidate 버전을 고정하고, 결과와 제한 사항을 함께 설명한다.

## 완료 기준

- 기능의 문제·대안·선택 이유와 남은 tradeoff를 설명할 수 있다.
- 현재 runtime 연결과 target/proposed 구현을 구분한다.
- 변경된 경계의 관련 테스트/eval과 필요한 Frontend→API→Backend/Worker 연결 근거가 있다.
- 시연은 해당 데이터·안전·동의·공개 조건 안에서 재현 가능하다.
- Human Owner가 결과와 제한 사항을 검토한다. Agent 검토는 근거이며 승인 대행이 아니다.

## 정본 안내

- [프로젝트 소개](README.md): 설치·환경 설정과 전체 구조.
- [문서 인덱스](docs/README.md): 현행 정본 문서와 과거 팀 이력(`docs/archive/`) 구분.
- [아키텍처](docs/architecture.md), [API](docs/api.md), [schema](docs/data-schema.md), [계약 상태](docs/contracts/README.md): 기술 구조와 공유 의미.
- [디자인](DESIGN.md): 현재 Report/clinic 범위. 다른 화면의 설계는 해당 문서·DTO를 사용한다.
- [테스트](docs/testing.md), [안전](docs/privacy-safety.md), [보안](SECURITY.md): 검증과 보호 조건.
- [Agent 운영](docs/ai/workflow.md), [작업 템플릿](docs/ai/task-template.md), [기능 설계 템플릿](docs/designs/README.md): 실행과 반복 설계 규격.

새 ARCHITECTURE.md·RULES.md·TASK.md·MEMORY.md를 추가해 기존 정본을 복제하지 않는다. 기능 선정 이후 작업별 명세와 기존 Decision/ADR를 연결한다.
