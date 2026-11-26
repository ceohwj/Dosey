# 복약 Chat Claim·Citation 응답 및 영속화 계약 v1

- **Status**: `APPROVED (2026-11-26 — Implementation PR in progress; kept in proposed/ until full end-to-end verification)`
- **Task / Branch**: `chat-citation-contract` (`feat/chat-rag-measured-slice-v1`)
- **작성일**: 2026-11-26 (KST)
- **선행 결정 근거**: Human Owner 2026-11-26 A3 방향 선택 — "(Recommended) POST·GET 응답 DTO 모두에 선택 필드 `citations` 추가 + 기존 `chat_citation` 테이블 보완(migration 1개)으로 재조회까지 지원"
- **관련 문서**:
  - [복약 Chat RAG 실측 고도화 v1 제품 요구사항](../../designs/guide-chat/chat-rag-measured-slice-requirements.md) (`CRM-05`–`CRM-12`)
  - [복약 Chat RAG 실측 고도화 v1 설계](../../designs/guide-chat/chat-rag-measured-slice-design.md) (3.3절, 4절)
  - [복약 챗봇 Backend–AI Core 계약](../current/medication-chat-ai-backend.md)
  - [#180 Guide public projection·Chat 후속 경계](./rag-guide-chat-public-projection-180.md)
  - [ADR 0004: 복약 Chat RAG 실측 고도화 v1 선정과 측정 기준](../../adr/0004-chat-rag-measured-slice-selection.md)

> **경계 및 비승인 선언**: 이 문서는 `AGENTS.md`에 따른 `CONTRACT_REQUEST` 초안이다. Human Owner가 `APPROVED`를 결정하기 전까지 runtime DTO, API router, SQLAlchemy 모델, Alembic migration, 테스트를 수정하지 않는다. 또한 이 계약 제안은 기존의 `PUBLIC_TRACK_F`(`PUBLIC_TRACK_F_ENABLED=false` 및 [외부 승인 게이트](../../release-gates/post-mvp-1-external-approvals.md)), `LICENSE_VERIFIED`(MFDS Source 이용 승인 경계), [ADR 0004](../../adr/0004-chat-rag-measured-slice-selection.md)의 비공개·폐쇄형 데모·안전 경계를 약화하거나 해제하지 않는다.

---

## 1. 문제 정의 및 현행 Baseline 근거

현재 복약 Chat runtime 구현은 아래 경계로 고정되어 있다.

1. **API 및 DTO Baseline** (`apps/backend/app/apis/v1/chat_routers.py`, `apps/backend/app/dtos/chat.py`, [medication-chat-ai-backend.md](../current/medication-chat-ai-backend.md)):
   - `POST /api/v1/chat-sessions/{session_id}/messages` (`201 Created`)는 `SendChatMessageData`를 반환한다 (`user_message_id`, `assistant_message_id`, `session_id`, `generation_status`, `content`, `model_name`, `prompt_version`, `created_at`, `completed_at`).
   - `GET /api/v1/chat-sessions/{session_id}/messages` (`200 OK`)는 `ChatMessageData` 목록을 반환한다 (`message_id`, `role`, `content`, `generation_status`, `created_at`).
   - `ChatReplyOutput`(`apps/backend/app/services/chat_ai/__init__.py`)은 `content`, `model_name`, `prompt_version`만 반환하며, 두 HTTP 응답 DTO 모두 `citations` 필드가 없다.
2. **왜 `content` 본문에 인용을 합치면 안 되는가 (`AssistantMessageContent.tsx`)**:
   - Frontend `apps/frontend/src/pages/AssistantMessageContent.tsx`는 `referenceDefinitionPattern`(`^[\t ]{0,3}\[...\]:`) 또는 `fencedCodePattern`(` ``` ` / `~~~`)이 감지되면 Markdown 렌더링을 중단하고 원문 평문 fallback(`chat-message__plain-fallback`)으로 전환한다.
   - 또한 `allowedMarkdownElements`는 `p`, `h1`~`h6`, `ul`, `ol`, `li`, `strong`, `em`, `br`, `a`, `img`만 허용하고 링크·이미지도 비활성 텍스트로 치환한다.
   - 따라서 답변 본문 `content`에 JSON 블록이나 Markdown 참조 링크 정의 형태로 인용을 섞으면 UI 서식이 깨지고 구조화 검증 결과(`SUPPORTED` / `NEEDS_VERIFICATION`)를 화면에서 안전하게 분리 표시할 수 없다.
3. **왜 `chat_citation` 영속화와 `POST`·`GET` 양쪽 DTO 확장이 필요한가**:
   - Frontend `apps/frontend/src/pages/ChatPage.tsx`는 전송 직후에는 `POST` 응답(`SendChatMessageResponse`)으로 메시지를 반영하고, 재접속·새로고침·오류 복구 시에는 `GET /api/v1/chat-sessions/{session_id}/messages`(`getChatMessages`)로 대화 목록을 재조회한다.
   - DB에는 `chat_citation`(`apps/backend/app/models/chat.py`) 테이블이 이미 존재하지만 현재 미사용 상태이며(`ChatRepository`가 저장·조회하지 않음), 기존 컬럼만으로는 `claim_key`, `claim_kind`, `verdict`, 공식 출처 좌표(`source_code`, `source_version`, `locator`), `content_sha256`를 저장할 수 없고 `knowledge_chunk_id`가 `NOT NULL`이라 지원 청크가 확정되지 않은 비치명적 `NEEDS_VERIFICATION` Claim을 저장할 수 없다.
4. **왜 기존 `GuideCitationData`를 그대로 재사용할 수 없는가**:
   - `GuideCitationData`(`apps/backend/app/dtos/guides.py`)는 `source_type` (`guide_citation`의 `LIFESTYLE_GUIDELINE` CHECK 제약), `source_code`, `source_version`, `locator`, `display_order`만 가진다.
   - Chat 요구사항(`CRM-08`, `CRM-09`)은 답변 내 **Claim 단위 근거 연결**(`claim_key`, `claim_text`)과 비치명적 `neutral` Claim의 미확정 표시(`verdict: "SUPPORTED" | "NEEDS_VERIFICATION"`)를 요구하므로 Chat 전용 `ChatCitationItem` 및 `chat_citation` 보완이 필요하다.

---

## 2. 제안하는 공유 계약 Delta

### 2.1 API DTO Delta (`apps/backend/app/dtos/chat.py`)

기존 필드의 이름·타입·필수 여부는 일체 변경하지 않고, 하위 호환 선택 필드 `citations`만 추가한다.

#### 신규 DTO: `ChatCitationItem`

```python
class ChatCitationVerdict(StrEnum):
    SUPPORTED = "SUPPORTED"
    NEEDS_VERIFICATION = "NEEDS_VERIFICATION"


class ChatCitationItem(BaseModel):
    claim_key: str = Field(min_length=1, max_length=64)
    claim_text: str = Field(min_length=1, max_length=1000)
    verdict: Literal["SUPPORTED", "NEEDS_VERIFICATION"]
    source_code: str = Field(min_length=1, max_length=100)
    source_version: str = Field(min_length=1, max_length=200)
    locator: str = Field(min_length=1, max_length=500)
    display_order: int = Field(ge=1)
```

- **Fail-closed 제외 규칙 (`CRM-09`)**:
  - `CRITICAL` Claim이 `neutral` 또는 `contradict`로 판정되거나, `NON_CRITICAL` Claim이 `contradict`로 판정된 경우 영속화 이전 단계에서 반드시 제외(fail-closed)한다.
  - `ChatCitationItem.verdict`는 `"SUPPORTED"`와 비치명적(`NON_CRITICAL`) `"NEEDS_VERIFICATION"`만 허용한다. `CONTRADICT` 또는 미확인 `CRITICAL` Claim은 DB와 API DTO 어느 곳에도 나타나서는 안 된다.
- **Public-safe 필드 경계 (내부 필드 제외)**:
  - 다음 내부 DB·감사·모델 판정 필드는 public `ChatCitationItem`에서 제외한다.
    - `knowledge_chunk_id` (내부 DB UUID)
    - `content_sha256` (내부 무결성 digest)
    - `claim_kind` (내부 분류 축; `CRITICAL` 미검증은 저장·노출 자체가 차단되므로 public DTO에 별도 분류를 노출하지 않음)
    - `cited_text` 및 `knowledge_chunk.chunk_text` 원문, 검색 score/rank, NLI logit/confidence

#### 기존 DTO 확장 (`SendChatMessageData`, `ChatMessageData`)

| DTO | 엔드포인트 | 추가 필드 | 기본값 |
| --- | --- | --- | --- |
| `SendChatMessageData` | `POST /api/v1/chat-sessions/{session_id}/messages` (`201 Created`) | `citations: list[ChatCitationItem] \| None = None` | `None` |
| `ChatMessageData` | `GET /api/v1/chat-sessions/{session_id}/messages` (`200 OK`) | `citations: list[ChatCitationItem] \| None = None` | `None` |

#### `citations` 상태 의미 (`None` vs `[]` vs `[...]`)

| 상황 | `POST` (`SendChatMessageData.citations`) | `GET` (`ChatMessageData.citations`) | 설명 |
| --- | --- | --- | --- |
| `USER` 메시지 | N/A (`SendChatMessageData`는 ASSISTANT 결과 중심) | `None` | 사용자 발화에는 인용이 없음 |
| `PENDING` / `GENERATING` / `FAILED` `ASSISTANT` | N/A (HTTP 오류 또는 실패) | `None` | 미완료·실패 메시지는 인용이 없음 |
| Citation pipeline 비활성 / legacy 메시지 | `None` | `None` | 검증 파이프라인 미실행 또는 저장된 `chat_citation` row 없음 |
| Citation pipeline 활성 · 표면화 Claim 0건 | `[]` | `None` (저장 row 0건) | `POST`에서는 파이프라인 활성·0건(`[]`)을 구분하며, `GET`은 `chat_message` 컬럼 추가 없이 `chat_citation` row 유무 기준으로 `None` 반환. Frontend는 `citations == null`과 `citations.length === 0` 모두 인용 미표시로 동일·안전 처리한다 |
| Citation pipeline 활성 · 표면화 Claim ≥ 1건 | `list[ChatCitationItem]` (`display_order` 오름차순 `1..N`) | `list[ChatCitationItem]` (`display_order` 오름차순 `1..N`) | `SUPPORTED` 및 비치명적 `NEEDS_VERIFICATION` Claim 목록을 동일 순서로 반환 |

---

### 2.2 DB 스키마 및 Forward Migration Delta (`chat_citation` in `apps/backend/app/models/chat.py`)

#### 현재 `chat_citation` 스키마 (검증된 Baseline: `apps/backend/app/models/chat.py:141-161`)

| 컬럼 / 제약 | 현재 정의 | 비고 |
| --- | --- | --- |
| `id` | `UUIDChar()`, `primary_key=True` | 유지 |
| `message_id` | `UUIDChar()`, `ForeignKey("chat_message.id")`, `nullable=False` | 유지 |
| `knowledge_chunk_id` | `UUIDChar()`, `ForeignKey("knowledge_chunk.id")`, `nullable=False` | **변경 필요 (`nullable=True`)** |
| `claim_text` | `String(1000)`, `nullable=False` | 유지 |
| `cited_text` | `String(2000)`, `nullable=False` | **변경 필요 (`nullable=True`)** |
| `display_order` | `Integer`, `nullable=False` | 유지 |
| `created_at` | `DateTime(timezone=True)`, `nullable=False`, `server_default=func.now()` | 유지 |
| `uq_chat_citation_order` | `UniqueConstraint("message_id", "display_order")` | 유지 |
| `chk_chat_citation_order` | `CheckConstraint("display_order > 0")` | 유지 |

#### 제안하는 단일 Forward-only Alembic Migration (`down_revision = "925a1b2c3d4e"` 또는 승인 시점 head)

1. **기존 컬럼 완화**:
   - `knowledge_chunk_id`: `nullable=False` → `nullable=True` (`ForeignKey("knowledge_chunk.id")`는 유지하며 `NULL` 외래키 허용). 검증된 지원 청크 없이 남기는 비치명적 `NEEDS_VERIFICATION` Claim에 필요하다.
   - `cited_text`: `nullable=False` → `nullable=True`. 비치명적 `NEEDS_VERIFICATION` Claim 또는 public DTO에서 원문 청크 인용문을 쓰지 않을 때 불필요한 원문 저장을 강제하지 않는다.
2. **신규 컬럼 추가**:

| 컬럼 | 타입 | Nullable | 규칙 및 근거 |
| --- | --- | --- | --- |
| `claim_key` | `String(64)` | `False` | 메시지 내 Claim 안정 식별자. 현재 `chat_citation` 테이블은 미사용 상태로 0건이며, migration 선행 검사(`SELECT count(*) FROM chat_citation`) 또는 빈 테이블 전제로 `NOT NULL` 추가(기존 row가 발견될 경우 migration 실패로 안전 중단) |
| `claim_kind` | `String(20)` | `False` | `'CRITICAL'` 또는 `'NON_CRITICAL'` |
| `verdict` | `String(30)` | `False` | `'SUPPORTED'` 또는 `'NEEDS_VERIFICATION'` |
| `source_code` | `String(100)` | `False` | 대상 출처 식별 코드 (예: MFDS 공식 출처 코드) |
| `source_version` | `String(200)` | `False` | 대상 출처 버전 문자열 |
| `locator` | `String(500)` | `False` | 문서 내 위치 식별자(섹션·조항 등) |
| `content_sha256` | `String(64)` | `True` | 근거 청크 본문 SHA-256 hex digest (`SUPPORTED`일 때 필수, `NEEDS_VERIFICATION`일 때 `NULL` 허용) |

> **참고 (`NEEDS_VERIFICATION`의 출처 좌표)**: `chat-claim-citation-generation` 단계는 답변 Claim과 후보 검색 근거 쌍으로 NLI 판정을 수행하므로, 비치명적 `NEEDS_VERIFICATION`(`neutral`) Claim에도 어떤 후보 출처(`source_code`, `source_version`, `locator`)와 대조했으나 확인이 필요한지 좌표는 유지하고 `knowledge_chunk_id` 및 `content_sha256`만 `NULL`로 둘 수 있다.

3. **일반 제약 조건만 사용 (DB Logic 금지 준수)**:
   - `AGENTS.md` 및 `scripts/ci/check_database_logic.py` 정책에 따라 **DB Trigger, RLS(Row-Level Security) 정책, Stored Procedure, User-Defined DB Function은 절대 추가하지 않는다.**
   - 아래 일반 `UniqueConstraint`와 `CheckConstraint`만 추가한다.
     - `UniqueConstraint("message_id", "claim_key", name="uq_chat_citation_claim_key")`
     - `CheckConstraint("claim_kind IN ('CRITICAL', 'NON_CRITICAL')", name="chk_chat_citation_claim_kind")`
     - `CheckConstraint("verdict IN ('SUPPORTED', 'NEEDS_VERIFICATION')", name="chk_chat_citation_verdict")`
     - `CheckConstraint("NOT (claim_kind = 'CRITICAL' AND verdict = 'NEEDS_VERIFICATION')", name="chk_chat_citation_fail_closed_verdict")` — 치명적 Claim의 미확인 상태 저장을 DB 수준에서도 차단한다.
     - `CheckConstraint("(verdict = 'SUPPORTED' AND knowledge_chunk_id IS NOT NULL AND content_sha256 IS NOT NULL) OR (verdict = 'NEEDS_VERIFICATION')", name="chk_chat_citation_supported_shape")`
     - `CheckConstraint("content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'", name="chk_chat_citation_content_hash")`
     - `CheckConstraint("length(trim(claim_key)) > 0 AND length(trim(claim_text)) > 0 AND length(trim(source_code)) > 0 AND length(trim(source_version)) > 0 AND length(trim(locator)) > 0", name="chk_chat_citation_nonblank_fields")`
4. **Downgrade**:
   - 개발·테스트 환경 호환을 위해 추가 제약·컬럼 제거 후 `knowledge_chunk_id`·`cited_text`를 `nullable=False`로 복원하되(기존 null row 존재 시 방어 검사 포함), 운영 이력은 forward-only 원칙을 따른다.

---

### 2.3 Transaction 순서 및 실패 경계 (`CRM-10`)

`ChatService.send_message()`(`apps/backend/app/services/chat.py`)의 기존 Gate와 동시성 잠금 순서를 유지하며 아래 흐름으로 결합한다.

```
1. Session ownership + SELECT ... FOR UPDATE (get_session_owned_for_update)
2. session_status == ACTIVE 확인
3. _ensure_current_version(chat_session)
4. ConsentPurpose.CHAT 동의 Gate
5. 확정 처방 version medications + history context 조회
6. USER(NOT_APPLICABLE) + ASSISTANT(PENDING → GENERATING) row 생성
7. [session row lock 보유 중 asyncio.timeout 예산 내부]
   ChatGeneratorEngine.reply(chat_input):
     a. Retrieval (검색)
     b. Structured Generation (답변 본문 + Claim·후보 근거 매핑)
     c. NLI Claim–Citation Validator (support / neutral / contradict 판정)
     d. Fail-closed 필터링:
        - CRITICAL + neutral / contradict → 제외·폐기 (또는 치명적 안전 실패 시 예외)
        - NON_CRITICAL + contradict → 제외·폐기
        - NON_CRITICAL + neutral → verdict="NEEDS_VERIFICATION" 보존
        - SUPPORTED → verdict="SUPPORTED" + knowledge_chunk_id + content_sha256 보존
8. 성공 시 저장 직전:
   a. _reject_stale_generation() (Prescription row lock, SET LOCAL lock_timeout = '3s')
   b. 동일 트랜잭션 내에서 assistant content + verified chat_citation rows + mark_completed() + update_last_message_at() 원자적 flush/commit
   c. SendChatMessageData(201 Created, citations 포함) 반환
```

- **실패·Timeout 경계 (`CRM-10`)**:
  - 7단계(검색, 생성, NLI 검증) 또는 8b 저장(인용 영속화 포함) 중 `mark_completed()` 이전에 어떤 예외·timeout이라도 발생하면, 기존 `ChatService.send_message()` 예외 매핑(`ChatTimeoutError` → `504 GATEWAY_TIMEOUT`, `ChatServiceUnavailableError` → `503 SERVICE_UNAVAILABLE`, `ChatGenerationFailedError` / 기타 `Exception` → `500 AI_RESPONSE_FAILED`)으로 전환한 뒤 `commit_failed_message_pair()`를 호출한다.
  - 실패 시 `ASSISTANT` 메시지는 `generation_status = FAILED`, `content = NULL`, `model_name = NULL`, `prompt_version = NULL`로 저장되며, **`chat_citation` row는 0건**만 남는다(부분 인용이나 미검증 초안이 `COMPLETED`로 커밋·노출되지 않는다).
  - 8a에서 처방 버전 불일치 시 기존과 동일하게 `PRESCRIPTION_VERSION_STALE` 실패 쌍을 커밋하고(`chat_citation` 0건) `409 PRESCRIPTION_VERSION_CONFLICT`를 반환한다.

---

## 3. 영향받는 소비자 및 Migration 영향

### 3.1 Backend

- `apps/backend/app/dtos/chat.py`: `ChatCitationVerdict`, `ChatCitationItem` 추가 및 `SendChatMessageData.citations`, `ChatMessageData.citations` 선택 필드 추가.
- `apps/backend/app/models/chat.py`: `ChatCitation` 모델 컬럼·제약 반영, `ChatMessage.citations` relationship에 `order_by="ChatCitation.display_order"` 정렬 지정.
- `apps/backend/alembic/versions/<new_revision>_extend_chat_citation_for_claim_verdicts.py` (신규): 현재 head(`925a1b2c3d4e`) 기반 단일 forward migration.
- `apps/backend/app/services/chat_ai/__init__.py`: `ChatReplyOutput` 내부 carrier에 verified citation 목록 및 citation pipeline 활성 여부 필드 추가(선택·기본값 유지로 기존 test 호환).
- `apps/backend/app/services/chat_generator_engine.py`: 검색·구조화 생성·NLI 검증기 호출, fail-closed 필터링, `asyncio.timeout` 예산 내 도메인 예외 매핑. (주의: `tests/contract/test_backend_ai_worker_import_boundary.py`를 준수하여 `ai_worker`를 직접 import하지 않고 `packages/rag_runtime` 또는 `apps/backend/app/services/chat_ai/` 경계만 사용.)
- `apps/backend/app/services/chat.py`: `send_message()`에서 `mark_completed()`와 함께 verified citation 저장 호출 및 `SendChatMessageData.citations` 채우기, `_to_message_data()`에서 `ChatMessage.citations`를 `ChatCitationItem`으로 직렬화.
- `apps/backend/app/repositories/chat_repository.py`: `create_citations()`(또는 `mark_completed()` 확장) 원자적 flush, `list_messages()`에서 `selectinload(ChatMessage.citations)` 추가.
- `apps/backend/app/dependencies/services.py`: 채택된 검증기 어댑터를 `ChatGeneratorEngine` DI에 연결.
- `docs/api.md`: 계약 승인 및 구현 PR에서 `POST`/`GET` 응답 예시 갱신.

### 3.2 Frontend

- `apps/frontend/src/api/chat.ts`: `ChatCitationVerdict`, `ChatCitationItem` 타입 추가 및 `ChatMessageData`, `SendChatMessageResponse['data']`에 `citations?: ChatCitationItem[] | null` 추가.
- `apps/frontend/src/pages/ChatPage.tsx`: `sendChatMessage()` 결과를 `ChatMessageData`로 변환할 때 `citations: response.data.citations ?? null` 보존, `AssistantMessageContent` 아래에 인용 목록 UI(`SUPPORTED` 출처 표시, `NEEDS_VERIFICATION` "확인 필요" 표시) 렌더링.
- `apps/frontend/src/pages/AssistantMessageContent.tsx`: 변경 없음(Markdown 본문 경계 유지).
- `apps/frontend/src/dev-preview/previewFixtures.ts`: `ChatMessageData` 타입 정합성 유지(선택 필드이므로 하위 호환).

### 3.3 Tests

- 계약 테스트:
  - `tests/contract/test_chat_ai_backend_contract.py`
  - `tests/contract/test_database_logic_policy.py` (신규 Alembic migration 자동 검증)
  - `tests/contract/test_backend_ai_worker_import_boundary.py`
- Backend 단위·API·통합 테스트:
  - `apps/backend/app/tests/chat/test_chat_service.py`
  - `apps/backend/app/tests/chat_ai/test_public_contract.py`
  - `apps/backend/app/tests/chat_apis/test_chat_message_api.py`
  - `apps/backend/app/tests/chat_integration/test_chat_concurrency.py`
  - `apps/backend/app/tests/repositories/test_chat_repository.py`
- Frontend 테스트:
  - `apps/frontend/tests/ChatApi.test.ts`
  - `apps/frontend/tests/ChatPage.test.tsx`
  - `apps/frontend/tests/AssistantMessageContent.test.tsx`

---

## 4. 승인 후 필수 인수 테스트 및 미결 체크리스트

### 4.1 승인 후 구현 PR에서 통과해야 할 테스트

1. **하위 호환성 (Legacy / Disabled / USER / FAILED)**:
   - Citation pipeline 비활성 시 `POST 201`의 `citations`가 `null`이고 `chat_citation` row가 0건인지 검증.
   - `GET 200`에서 `USER` 메시지, `FAILED` 메시지, 저장된 인용 없는 legacy `COMPLETED` 메시지의 `citations`가 `null`인지 검증.
2. **POST·GET 일관성 및 Public-safe 직렬화 (`CRM-08`)**:
   - `SUPPORTED` 및 비치명적 `NEEDS_VERIFICATION` Claim이 `display_order` (`1..N`) 순으로 `chat_citation`에 저장되고, `POST 201`과 후속 `GET 200`에서 동일한 `citations` 배열이 복원되는지 검증.
   - 응답 JSON에 `knowledge_chunk_id`, `content_sha256`, `claim_kind`, `cited_text` 등 내부 필드가 노출되지 않는지 검증.
3. **Fail-closed 안전 검증 (`CRM-09`)**:
   - `CRITICAL` + `neutral`, `CRITICAL` + `contradict`, `NON_CRITICAL` + `contradict` Claim이 영속화 전 제거되어 DB와 `POST`/`GET` 응답 모두에 나타나지 않는지 검증.
   - DB 제약 테스트: `claim_kind = 'CRITICAL'`이면서 `verdict = 'NEEDS_VERIFICATION'`인 row 삽입 시 `chk_chat_citation_fail_closed_verdict`에 의해 거부되는지 검증.
   - DB 제약 테스트: `verdict = 'SUPPORTED'`이면서 `knowledge_chunk_id IS NULL` 또는 `content_sha256 IS NULL`인 row 삽입 시 `chk_chat_citation_supported_shape`에 의해 거부되는지 검증.
4. **실패·Timeout·Stale Version 원자성 (`CRM-10`)**:
   - 검색 실패, 생성 실패, NLI 검증기 실패, `asyncio.timeout` 초과 시 `mark_completed()`가 호출되지 않고 `commit_failed_message_pair()`가 `generation_status = FAILED`, `content = None`, `chat_citation` 0건으로 저장하는지 검증.
   - 저장 직전 처방 버전 변경 시 `PRESCRIPTION_VERSION_STALE` + `409 PRESCRIPTION_VERSION_CONFLICT` + `chat_citation` 0건 검증.
5. **Migration & DB Policy 검증**:
   - `alembic upgrade head` 및 `python scripts/ci/check_database_logic.py` 통과(Trigger/RLS/Procedure/UDF 0건).
6. **Frontend 렌더링 검증**:
   - `ChatPage.test.tsx` / `ChatApi.test.ts`에서 `citations`가 `null` / `[]` / `SUPPORTED`+`NEEDS_VERIFICATION` 포함일 때 `AssistantMessageContent` Markdown이 깨지지 않고, `NEEDS_VERIFICATION` Claim에 "확인 필요" 표시가 렌더링되며, `contradict` 또는 미검증 `CRITICAL` Claim이 표시되지 않음을 검증.

### 4.2 미결 확인 사항 및 Human Owner Sign-off

- **미결 메모**:
  - `GET` 0-row 구분: 현재 제안은 `chat_message` 테이블에 별도 boolean 컬럼을 추가하지 않고 `chat_citation` 테이블만 보완하므로, 활성 파이프라인에서 표면화 Claim이 0건이었던 `COMPLETED` 메시지는 `POST`에서 `[]`, `GET` 재조회에서 `None`으로 표현된다. `GET`에서도 `[]`와 `None`을 엄격히 구분해야 할 제품 요구가 생기면 추후 `chat_message`에 `citation_checked` 컬럼 추가 여부를 별도 검토한다.
  - Migration `down_revision`: 구현 착수 시점의 실제 Alembic head(현재 `925a1b2c3d4e`)를 기준으로 직렬화한다.

#### Human Owner Decision

- [x] `APPROVED` — 위 DTO·`chat_citation` 단일 migration·트랜잭션 경계로 `chat-citation-api` / `chat-citation-validator-wire` / `chat-citation-ui` 진행 승인 (2026-11-26 Human Owner 승인; 구현 PR 진행 중 — 전체 E2E 검증 완료 전까지 `proposed/` 유지)
- [ ] `REJECTED` — 반려 (사유: )
- [ ] `REUSE_EXISTING_CONTRACT` — 기존 계약 재사용 지시 (대상: )
- [ ] `NEEDS_MORE_EVIDENCE` — 추가 근거 필요 (요청 사항: )
