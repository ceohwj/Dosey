# Candidate 영속 Guard·capture 합성 내부 계약

- Decision ID: `PD-DOSEY-20261104-CANDIDATE-AUDIT-1`
- Human Owner 결정: 2026-11-04 “권고 계약으로 합성 내부 구현 승인”.
- 정본: [내부 Contract Freeze v1](../../contracts/targets/post-mvp-1/candidate-request-guard-audit-v1.md).
- 승인 범위: Candidate 감사 extension·Source capture observation 두 구조, 제한된 별도 발행 역할, PASS 원자 저장·FAIL 감사-only transaction, forward migration·저장·삭제 검증. 격리 합성 내부 구현만 승인.
- 제외: 실제 Source/API 호출·운영 수치/retention·운영 DB/역할 배포·Source 활성화·공개/merge/release. Source07→08 전환 승인 아님.
- 기존 성공 replay·key hash/HMAC/TTL/암호화·404·기술 안전·공개 flag/일반 DI 차단은 유지.
- 참조 snapshot: 승인 질문 당시 계약 문서/Schema/vector hash는 outputs/candidate-policy-20261104/contract-request-snapshot.json에 저장했다. 이동 후 값은 상태 표기/참조만 갱신하며 runtime 의미는 동결한다.
