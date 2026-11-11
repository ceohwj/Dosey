# Hygiene의 file URI 검사 누락 — 2026-11-11

- ID: hygiene-file-uri-20261111
- CATEGORY: verified
- RELATED_TASK: dosey-agent-continuity / Task3
- SCOPE: 개발 문서 검사기. 제품·의료 데이터 경로와 무관함.

## 관측과 원인

독립 reviewer가 `_link_problem`의 netloc 외부 URL skip이 file scheme 거부보다 먼저 적용됨을 확인했다. `[local](file://localhost/etc/passwd)`가 unsafe_path로 보고되어야 하지만 findings 없이 허용됐다. 대상 파일을 읽었다는 증거는 없으며 실제 노출 사례를 뜻하지 않는다.

당시 scanner SHA-256: `ac13f3a8443b37ec5a46a10fcaecd391b75d8d869ca4b807cc3e9399917e2e22`. 초기 22개 테스트에 authority가 있는 file URI가 없었다. 다른 file URI 검사는 이 형태의 거부를 증명하지 못했다.

## 수리와 재검토

Integrator가 유효한 finding으로 분류하고 원래 writer에게 회귀 테스트 → RED → 최소 수정 → GREEN → scoped review를 배정했다. writer 기록은 RED 2실패/25통과 → GREEN27통과다. 모든 file scheme을 외부 링크 skip 전에 거부한다. HTTPS·protocol-relative 외부 URL 무시는 보존한다. 독립 scoped review는 ADDRESSED / Spec PASS / Quality APPROVE였다. 주 에이전트의 최신 통합 실행에서도 신규 hygiene 27개가 통과했다. 전체 실행의 유일한 실패는 별도 검토 복사본 inventory 문제였고 검사기 실패가 아니었다. 적용 scanner SHA-256: `93f76ac4f800cc563a71933ed3d9ce4bc99c4e3d80fbd8233beb2510952ef3a7`.

RECHECK_WHEN: URL 판정 순서·허용 scheme·Markdown parser·정책 경계 변경 또는 새 반례.
DEDUP_REF: 동일 원인·증상은 이 기록에 증거를 추가하고 사본을 만들지 않음.
LIMITS: bounded parser이며 완전한 CommonMark·OS 격리·의미적 문서 drift 보장은 아님.

상세 근거는 [통합 검증 기록](../testing/agent-workflow-20261111.md)과 [이식 가능한 근거 JSON](../testing/evidence/agent-continuity-20261111.json)에 연결한다. 원래 로컬 fix/review logs는 역사적 출처이며 게시용 재실행 결과와 구분한다.
