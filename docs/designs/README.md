# 기능별 설계와 구현 계획

설계·구현 계획을 기능 도메인별로 관리하는 인덱스입니다. 과거 팀 시절 작성자별 폴더(`ceohwj/`, `jye-rookie/`)에 남아 있던 문서도 모두 기능 도메인 폴더로 통합했습니다.

현재 실제 동작은 코드·현재 계약·테스트로 확인하며, 작업 배정과 연속성 운영은 [Agent workflow](../ai/workflow.md)와 [AGENTS](../../AGENTS.md)를 따릅니다.

## 새 기능 설계 추가 방법 (반복 템플릿)

새 기능을 시작할 때는 [`_template/`](_template/)의 [설계 템플릿](_template/design.md)과 [구현 계획 템플릿](_template/plan.md)을 해당 기능 폴더로 복사해 작성합니다.

```bash
cp docs/designs/_template/design.md docs/designs/<도메인>/<기능>-design.md
cp docs/designs/_template/plan.md docs/designs/<도메인>/<기능>-plan.md
```

## 도메인별 디렉터리

| 경로 | 내용 |
| --- | --- |
| [`_template/`](_template/) | 새 기능 설계·구현 계획 표준 템플릿 |
| [`account/`](account/) | 계정·회원탈퇴 |
| [`ocr/`](ocr/) | OCR·구조화·동의·사용자 확인 |
| [`medication-management/`](medication-management/) | 복약 일정·Check-in·Barrier/Support |
| [`frontend/`](frontend/) | 화면·디자인 시스템 |
| [`provider/`](provider/) | Provider·관측성·검증 |
| [`evaluation/`](evaluation/) | 평가·Metric·Dataset |
| [`guide-chat/`](guide-chat/) | Guide·Chat 생성 및 Backend 연동 |
| [`rag/`](rag/) | Source·검색·식별·인덱스 |

## 이전 작성자 폴더 통합 내역

- `docs/designs/ceohwj/` (정현우 본인 작성분) → `evaluation/`, `guide-chat/`, `provider/`, `rag/`로 전체 이동 완료
- `docs/designs/jye-rookie/` → `medication-management/`, `rag/`로 전체 이동 완료
- `docs/designs/ocr-consent-gate-458-implementation.md` → `ocr/`로 이동 완료
