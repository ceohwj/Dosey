# 검토용 Python 복사본의 inventory 충돌 — 2026-11-11

- ID: review-snapshot-inventory-20261111
- CATEGORY: verified
- SCOPE: Superpowers 검토 artifact 보관 형식. 제품 실행 코드는 변경하지 않음.

## 관측·원인·수리

Task3 fix 이전 본문을 로컬 검토 artifact 디렉터리의 `task-3-fix1-before/tests/services/test_agent_hygiene.py`로 복사했다. 이 경로가 Git ignore여도 기존 테스트 inventory는 저장소의 Python 테스트 파일을 찾아 실행 lane 분류를 요구한다. 최종 검사에서 161통과/1실패였으며 실패 경로가 이 복사본과 정확히 일치했다. 앞선 baseline 복사본에서도 같은 충돌을 수정했으나 fix snapshot 생성 지시에 그 교훈을 적용하지 못했다.

소유한 복사본 두 개의 bytes를 유지하며 확장자를 `.py.snapshot`으로 바꿨다. 실제 구현·테스트·분류기·ignore 예외는 바꾸지 않았다. 실패했던 inventory 계약 테스트 파일을 재실행해 **17통과/exit0**를 확인했다. 경로 매핑과 처음 실패 로그는 원래 로컬 SDD 작업 근거에 보존했다. 게시용 [근거 JSON](../testing/evidence/agent-continuity-20261111.json)에서 역사적 결과와 새 검증 상태를 구분한다.

## 재사용 규칙

검토용 소스 복사본은 실행·수집되는 원래 확장자로 두지 않는다. patch·해시 manifest·`.snapshot`으로 저장한다. 기존 검사에서 제외하도록 임시 면제를 만들지 않는다. 실행 가능한 합성 fixture는 제품 저장소 밖의 정확한 소유 temp root에 두고 증거를 보존한 뒤 정리한다.

RECHECK_WHEN: inventory 수집 패턴·CI lane·artifact 저장 경로 변경.
LIMITS: 해당 inventory와 복사본 형식에서 확인한 결과. 모든 도구의 자동 수집 방지를 보장하지 않음.
DEDUP_REF: 같은 artifact 원인은 이 기록에 새 증거 연결.

[통합 검증 기록](../testing/agent-workflow-20261111.md)을 함께 확인한다.
