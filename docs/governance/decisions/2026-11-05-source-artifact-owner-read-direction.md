# Source artifact owner — 합성 저장·읽기 우선 설계 방향

- ID: `PD-DOSEY-20261105-SOURCE-OWNER-READ-DIRECTION-1`
- 날짜: 2026-11-05. 상태: **Human Owner 방향 채택 / 세부 계약 미확정 / 미구현**.
- 근거: 이번 대화의 구조화 질문에 Human Owner가 “저장·읽기부터 설계 (권고)”를 선택했다. 질문은 합성 전용 저장·검증된 읽기·결과 확인, 새 원본 폴더 삭제 차단, 기존 로컬 cleanup 유지와 **세부 계약 확정·코드 구현 승인 제외**를 명시했다.

## 채택한 방향

별도 non-root artifact owner와 비특권 client의 합성 PUT/verified READ/결과 관측부터 설계한다. private vault 0700/0600과 기존 publisher 전체 DB transaction·기존 재시도·NO_CHANGE·Candidate key·의료/Privacy/Source 안전·공개 차단을 유지한다. 단순 reader adapter 교체 외에도 canonical/parser의 직접 pathname 재읽기를 영향 범위에 포함한다.

새 owner vault의 business DELETE를 제공하지 않고 legacy cleanup의 새 root 접근을 닫는 범위다. 기존 로컬 cleanup 계약·코드·과거 기록은 유지한다. 새 DELETE endpoint/helper·공통 gate/fencing은 후속 별도 계약으로 남긴다. owner는 PUT/임시 파일 정리에 필요한 쓰기 DAC를 가지므로 owner의 OS상 삭제 불가능성을 보장한다고 표현하지 않는다.

## 미승인·후속 작업

[변경 요청](../../contracts/proposed/post-mvp-1/source-artifact-finalizer-owner-boundary-v1.md)은 계속 Proposed다. wire/intent 상태·durability·peer auth/UID mapping·상한·실제 잠금 시점·실패 복구·소비자/테스트 영향을 구체화하고 독립 검토한 뒤 세부 계약의 Human Owner 결정을 받는다. 방향 선택만으로 freeze·코드 수정·schema/migration·새 dependency·운영 UID/credential·실제 Source·운영 DB·배포/공개·Current 승격을 승인하지 않는다.

B1의 기존 승인 범위와 [공개 게이트](../../release-gates/post-mvp-1-external-approvals.md)는 그대로다. Docker 재시작 허용은 검증 환경 복구에 한정된 별도 실행 권한이며 artifact 계약 승인이 아니다.
