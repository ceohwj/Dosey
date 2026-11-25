# Architecture Decision Records

프로젝트·시스템에 영향을 주는 기술 결정을 `NNNN-short-title.md` 형식으로 기록합니다.

각 문서는 다음을 포함합니다.

- 상태: Proposed / Accepted / Superseded
- 배경과 해결할 문제
- 선택한 결정
- 검토한 대안과 제외 이유
- 보안·운영·테스트 영향
- 관련 Issue와 PR

우선 기록할 후보는 Redis Stream 작업 큐, SSE 결과 알림, 파일·모델 저장소, 공통 상태 계약입니다.

## 상태 인덱스

- [ADR 0001](./0001-synchronous-chat-generation-with-session-row-lock.md) — Accepted, 현재 동기 MVP
- [ADR 0002](./0002-post-mvp-1-async-execution.md) — Accepted target / Not implemented, 승인된 목표 결정·미구현
- [ADR 0003](./0003-chat-recent-context-single-v2.md) — Proposed, 단일 `chat-prompt-v2`와 Local-only 최근 대화 조회 결정
- [809: Normalized OCR Image](./809-normalized-ocr-image.md) — OCR 정규화 이미지 처리 결정
- [838: Production Chat History Context Activation](./838-production-chat-history-context-activation.md) — 운영 환경 최근 대화 문맥 활성화 경계

목표 ADR의 존재는 현재 구조의 대체를 뜻하지 않는다. 실제 전환 PR에서 route·DTO·migration·Worker·계약 및 통합 테스트가 함께 병합되고 운영 gate가 확인된 뒤 상태 관계를 갱신한다.
