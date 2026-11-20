# 기존 문서 링크 8건 복구 기록

2026-10-23. 탐색 경로 정정 기록이며 새 Product Decision·Contract Freeze·승인 artifact가 아니다. 문서의 상태·본문 규칙·승인 이력·외부 URL은 그대로 두고 링크 대상 문자열만 수정했다. 기존 폴더 정리에서 계약 상태 문서를 이동·수정하지 않은 것은 당시 작업 범위의 보호 조치였으며, 링크의 안전한 복구까지 영구 금지한다는 뜻은 아니다.

## 처리 내역

| 원본 문서 | 문제 | 처리·근거 |
| --- | --- | --- |
| [HandlerConfig #192](../contracts/proposed/track-c-handler-config-192.md) | 상황 선택 계약의 옛 proposed 경로 | `../current/track-c-travel-situation-194.md`로 연결. 동일 #194 문서와 PD-194-3·계약 인덱스의 이동 근거 확인 |
| [Support Plan API #194](../contracts/proposed/track-c-support-plan-api-194.md) | 상황 선택 계약의 옛 proposed 경로 | 같은 Current 경로로 복구. 이 API 제안 문서의 상태를 승격하지 않음 |
| [Support Plan API #194](../contracts/proposed/track-c-support-plan-api-194.md) | 계획별 자료 계약의 옛 proposed 경로 | `../current/track-c-plan-resources-194.md`로 복구. 해당 문서의 병합·외부 승인 제한은 유지 |
| [Web Push 제안](../contracts/proposed/web-push-v1.md) | 알림 계약의 옛 proposed 경로 | 실제 `../current/track-b-notifications-v1.md`로 연결. Push 제안의 승인·공개 상태는 변경하지 않음 |
| [Idempotency target](../contracts/targets/post-mvp-1/idempotency-v1.md) | `#identification-preflight` 앵커가 없음 | 1차 복구에서 잘못된 fragment만 제거. 후속 계약·코드 대조 후 기존 Bundle 정의인 `rag-runtime-v1.md#runtime-release-bundle과-실행-snapshot`으로 탐색 대상 정정. 아래 판단 근거 참조 |
| [Python 무결성 리뷰 보완](decisions/2026-09-10-python-integrity-review-429.md) | 삭제된 `...access-control-candidate.md` 참조 | 현재 [PD-368 기록](decisions/2026-09-09-protected-retrieval-runner-access-control.md)의 실제 경로로 연결. §4·§14의 PD-398-R1 역참조·기존 superseded 설명 확인 |
| [Guide CLOSED_DEMO 경계](decisions/2026-09-21-guide-closed-demo-rag-provider-boundary.md) | consent 계약까지의 상대 깊이 오류 | `../../contracts/proposed/consent-gate-207.md`. 원래 의도한 제안 계약 유지 |
| [Guide CLOSED_DEMO 경계](decisions/2026-09-21-guide-closed-demo-rag-provider-boundary.md) | Guide 계약까지의 상대 깊이 오류 | `../../contracts/proposed/guide-closed-demo-rag-v1.md`. `UNDER_REVIEW / PENDING_APPROVAL`과 공개 차단을 유지 |

## Runtime Release Bundle 참조 — 후속 비교와 해결

Idempotency 문장의 링크 표시명은 **Runtime Release Bundle**인데 기존 대상은 Medication Identification의 존재하지 않는 Preflight 앵커였다. 실제 `Guide·Chat Preflight 경계` 절은 접수 조건을 다루므로 유효한 앵커라는 이유만으로 그 절에 연결하면 의미가 어긋난다.

1차 복구에서는 같은 대상 문서를 유지하고 fragment만 제거한 뒤 의미 판단을 보류했다. 이후 Human Owner의 비교 요청에 따라 기존 계약과 코드의 연결을 조사했다. 아래 근거로 **참조 대상을 기존 정의 절에 맞추는 탐색 수정**으로 판단해 [RAG Runtime의 Bundle 설명](../contracts/targets/post-mvp-1/rag-runtime-v1.md#runtime-release-bundle과-실행-snapshot)으로 연결했다.

- [Medication Identification](../contracts/targets/post-mvp-1/medication-identification-v1.md)의 목적 절은 이미 RAG Runtime v1을 참조한다. 입력 정본에는 Runtime Release Bundle version, Search 재사용 조건에는 같은 Bundle이 포함된다.
- [RAG Runtime](../contracts/targets/post-mvp-1/rag-runtime-v1.md)의 해당 절은 Source·Index·Rule·Policy·Resolver·Graph·Prompt·Validator·Model·Manifest·Worker version을 함께 고정하는 **같은 Bundle**과 실행 Snapshot을 정의한다. 두 Target 문서는 같은 외부 Manifest와 Design `1.50`/DB `1.47` 근거를 공유한다.
- [계약 인덱스](../contracts/README.md)는 이미 RAG Runtime v1을 Runtime Bundle의 Manifest Hash·저장 정합 정본으로 연결한다. 새로운 개념·문서 버전·승인 권한을 채택한 것이 아니다.
- Idempotency 문장의 `candidate_search_result_id`·action·기대 Bundle 포함 요구, 요청/응답 DTO·오류·transaction·재시도 규칙은 바꾸지 않았다. `rag-runtime-v1.md`의 헤더·승인 범위·본문과 계약 상태도 그대로다. 링크가 특정 UUID/hash 직렬화 형식을 새로 정하지 않는다.

## 현재 코드의 별도 미연결 범위와 향후 영향

링크 수정은 실제 Bundle의 멱등 지문 연결을 구현하지 않는다. 현재 확인·거절의 [Service](../../backend/app/services/medication_candidates.py)는 fingerprint의 `runtime_release_bundle`에 [고정 placeholder](../../backend/app/services/idempotency.py) `runtime-release-bundle-not-implemented-issue-175`를 전달한다. 이 경로는 실제 Bundle UUID나 `bundle_manifest_hash`를 지문에 넣지 않는다. 반면 별도 [Bundle 저장·검증 코드](../../backend/app/repositories/rag_runtime_repository.py)에는 실제 Bundle 구현이 있으므로 이를 프로젝트 전체의 Bundle 미구현으로 일반화하지 않는다.

공통 멱등 Service는 전달된 fingerprint를 hash하고 기존 `request_hash`와 다르면 `409 IDEMPOTENCY_KEY_CONFLICT`로 처리한다. [확인·거절 DTO](../../backend/app/dtos/medication_candidates.py)에는 Bundle 입력 필드가 없고, Candidate Search의 저장 모델에는 nullable `runtime_release_bundle_id`가 있다. 향후 실제 Bundle 결속 작업에서는 다음을 먼저 정해야 한다.

| 결정할 사항 | 영향 |
| --- | --- |
| 기대 Bundle의 정본·표현을 Search snapshot, 검증된 Bundle ID/version/hash 중 어떻게 고정할지 | 서버 도출·현재성 검증과 공개 DTO 변경 필요 여부. 최신 active pointer를 임의로 지문에 넣으면 같은 요청의 재현성이 달라질 수 있음 |
| placeholder로 저장된 미만료 멱등 레코드의 전환 방식 | 같은 키·같은 입력도 새 fingerprint와 달라져 기존 성공 재현 대신 409가 될 수 있음. 보존기간·drain·버전 전환 등 호환성 정책 검토 필요 |
| 신규 mutation의 Bundle 검증과 기존 replay의 처리 순서 | 소유권·기존 응답 재현·STALE/오류 의미·잠금/transaction 및 동시 변경 검증 |
| 구현·승인 범위 | 실제 결속을 변경할 때는 CONTRACT_REQUEST로 소비자·API/schema·필요 시 forward migration 영향과 versioned 계약 결정을 먼저 정리. 기존 확인·거절 replay/충돌 테스트, Bundle 변경·기존 레코드·동시 요청 회귀 보강 필요 |

이 영향 분석은 후속 실제 결속 변경을 승인하거나 구현한 기록이 아니다. 이번 후속 작업은 링크와 이 설명만 수정했다.

이번 8건에서 로컬 hash·서명 결속 때문에 경로 수정 자체를 보류한 사례는 발견하지 못했다. 과거 승인 여부나 외부 저장소까지 새로 검증한 것은 아니다. 승인 ID·검토자·시각·상태·기존 approval JSON·Source 및 평가 원본은 수정하지 않았다.
