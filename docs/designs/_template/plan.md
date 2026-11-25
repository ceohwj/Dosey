# <기능명> 구현 계획

- **설계 문서**: `./design.md`
- **예상 변경 파일**:
  - `apps/backend/...`
  - `apps/ai_worker/...`
  - `apps/frontend/...`
  - `packages/...`
  - `tests/...`

## 단계별 작업

### Step 1. 계약 및 스키마 확인
- [ ] 기존 계약/DTO 재사용 여부 확인
- [ ] (필요 시) DB 마이그레이션 작성

### Step 2. 핵심 구현
- [ ] ...

### Step 3. 테스트 및 검증
- [ ] `uv run ruff check .`
- [ ] `uv run ruff format . --check`
- [ ] `uv run mypy apps/backend/app apps/ai_worker`
- [ ] 관련 `pytest` 실행 및 결과 기록

## 완료 체크리스트
- [ ] 현재 runtime 연결과 미연결/목표 상태를 문서에 구분해 기록함
- [ ] 실제 secret·환자정보가 포함되지 않음을 확인함
