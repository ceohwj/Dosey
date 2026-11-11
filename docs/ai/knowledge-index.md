# 개발 작업 지식 인덱스

새 작업은 아래 정본에서 필요한 근거를 찾아 task의 `knowledge_refs`에 경로를 남긴다. 이 인덱스는 결정·계약·진행의 복사본이나 별도 승인 기록이 아니다. 문서 상태·revision과 확인한 코드/테스트를 구분하고 미확인은 UNKNOWN으로 남긴다.

| 찾는 내용 | 정본·확인 범위 |
| --- | --- |
| 권한·파일 배정·결정 경계 | [AGENTS](../../AGENTS.md), [기여 기준](../../CONTRIBUTING.md) |
| 제품 목적·구조·UX | [프로젝트 README](../../README.md), [아키텍처](../architecture.md), [DESIGN](../../DESIGN.md). DESIGN은 현재 Report/clinic 범위 |
| 보안·Privacy·의료 안전 | [SECURITY](../../SECURITY.md), [Privacy/safety](../privacy-safety.md) |
| 현재·목표·제안 계약 | [계약 인덱스](../contracts/README.md). current는 구현·DTO·관련 migration·테스트를 확인하고 targets는 승인 목표, proposed는 미확정으로 읽음 |
| Decision·역사적 provenance | [문서 권위·상태](../governance/post-mvp-1-document-authority.md)에서 연결한 기존 `docs/governance/decisions/` 기록. 과거 팀 역할·승인을 현재 staffing/권한으로 승계하지 않음 |
| 아키텍처 결정 상태 | [ADR 인덱스](../adr/README.md). Accepted current, Accepted target/Not implemented, Proposed를 구분하고 결정 사본을 만들지 않음 |
| 검증 범위와 근거 | [테스트 전략](../testing.md)의 해당 검증 기록. 환경·revision·실행 여부를 대조하며 과거 PASS를 현재에 승계하지 않음 |
| 공개 조건 | [외부 승인 게이트](../release-gates/post-mvp-1-external-approvals.md). 구현·review와 별도 조건 |
| 작업 시작·복구·종료·진행 | [workflow](workflow.md), [task 템플릿](task-template.md). 새 opt-in task 진행은 해당 canonical task.json |
| 역할 설정의 설명·관측 | 현재 환경이 제공하는 역할·도구 정보를 확인하고 설정과 실제 실행 관측을 구분. 이 게시 범위는 역할 등록·모델 설정을 포함하지 않음 |
| 기각·관측·가설·확인된 실패 | [실패 기록 안내](../failures/README.md). 증거와 재검토 조건 확인 |

문서와 코드가 다르면 차이를 task의 known gaps로 보고한다. 이 인덱스에서 target을 current로 승격하거나 제품·계약·안전 결정을 내리지 않는다. 상세 Decision은 기존 위치에서 갱신하고 여기는 링크만 유지한다. 무관한 역사 문서·전체 채팅·raw data를 전부 읽지 않는다.
