from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import ClauseElement

from app.core.errors import ApiError
from app.dtos.chat import ChatCitationItem, ChatCitationVerdict, ChatMessageData, SendChatMessageRequest
from app.models.chat import (
    ChatCitation,
    ChatCitationClaimKind,
    ChatGenerationStatus,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatSessionStatus,
)
from app.models.chat import (
    ChatCitationVerdict as ModelChatCitationVerdict,
)
from app.models.user_consents import ConsentPurpose
from app.repositories.chat_repository import ChatRepository
from app.services.chat import ChatService
from app.services.chat_ai import (
    ChatGenerationFailedError,
    ChatReplyInput,
    ChatReplyOutput,
    ChatServiceUnavailableError,
    ChatTimeoutError,
    ChatVerifiedCitation,
)

VALID_SHA256 = "a" * 64
VALID_SHA256_ALT = "b" * 64


class RecordingEngine:
    def __init__(self, *, result: ChatReplyOutput | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.events: list[str] | None = None

    async def reply(self, chat_input: ChatReplyInput) -> ChatReplyOutput:
        del chat_input
        if self.events is not None:
            self.events.append("engine.reply")
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class RecordingConsentGate:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def require_for_intake(self, *, user: object, purpose: ConsentPurpose) -> None:
        del user
        self.events.append(f"consent.require.{purpose.value}")


class RecordingPrescriptionRepository:
    def __init__(self, medications: Sequence[object], events: list[str]) -> None:
        self.medications = medications
        self.events = events

    async def get_version_medications(self, *, prescription_version_id: object) -> Sequence[object]:
        del prescription_version_id
        self.events.append("prescription.get_version_medications")
        return self.medications


class RecordingChatRepository:
    def __init__(
        self,
        session: SimpleNamespace,
        events: list[str],
        *,
        current_version_at_completion: bool = True,
        create_citations_error: Exception | None = None,
    ) -> None:
        self.owned_session = session
        self.events = events
        self.current_version_at_completion = current_version_at_completion
        self.create_citations_error = create_citations_error
        self.messages: list[SimpleNamespace] = []
        self.persisted_citations: list[ChatCitation] = []
        self.create_citations_calls = 0
        self.mark_completed_calls = 0
        self.commits = 0

    async def get_session_owned(self, *, session_id: object, user_id: object) -> SimpleNamespace | None:
        del user_id
        self.events.append("chat.get_session_owned")
        if session_id == self.owned_session.id:
            return self.owned_session
        return None

    async def get_session_owned_for_update(self, *, session_id: object, user_id: object) -> SimpleNamespace | None:
        del session_id, user_id
        self.events.append("chat.lock_owned")
        return self.owned_session

    async def next_seq(self, *, session: object) -> int:
        del session
        self.events.append("chat.next_seq")
        return len(self.messages) + 1

    async def list_messages(self, *, session: object) -> list[SimpleNamespace]:
        del session
        self.events.append("chat.list_messages")
        return list(self.messages)

    async def create_message(self, **kwargs: object) -> SimpleNamespace:
        role = kwargs["role"]
        session = cast(SimpleNamespace, kwargs["session"])
        self.events.append(f"chat.create.{role}")
        message = SimpleNamespace(
            id=uuid4(),
            session_id=session.id,
            message_seq=kwargs["message_seq"],
            role=role,
            content=kwargs["content"],
            generation_status=kwargs["generation_status"],
            model_name=None,
            prompt_version=None,
            error_code=None,
            error_message=None,
            created_at=datetime(2026, 11, 26, tzinfo=UTC),
            completed_at=None,
            citations=[],
        )
        self.messages.append(message)
        return message

    async def mark_generating(self, message: SimpleNamespace) -> SimpleNamespace:
        self.events.append("chat.mark_generating")
        message.generation_status = ChatGenerationStatus.GENERATING
        return message

    async def lock_if_current_version(self, *, chat_session: object) -> bool:
        del chat_session
        self.events.append("chat.lock_if_current_version")
        return self.current_version_at_completion

    async def create_citations(
        self,
        *,
        message_id: UUID,
        citations: Sequence[ChatVerifiedCitation],
    ) -> list[ChatCitation]:
        self.events.append("chat.create_citations")
        self.create_citations_calls += 1
        if self.create_citations_error is not None:
            raise self.create_citations_error
        rows = [
            ChatCitation(
                id=uuid4(),
                message_id=message_id,
                knowledge_chunk_id=item.knowledge_chunk_id,
                claim_key=item.claim_key,
                claim_text=item.claim_text,
                claim_kind=ChatCitationClaimKind(item.claim_kind),
                verdict=ModelChatCitationVerdict(item.verdict),
                source_code=item.source_code,
                source_version=item.source_version,
                locator=item.locator,
                content_sha256=item.content_sha256,
                cited_text=item.cited_text,
                display_order=item.display_order,
                created_at=datetime(2026, 11, 26, tzinfo=UTC),
            )
            for item in sorted(citations, key=lambda citation: citation.display_order)
        ]
        self.persisted_citations.extend(rows)
        for message in self.messages:
            if message.id == message_id:
                message.citations = list(rows)
        return rows

    async def mark_completed(self, message: SimpleNamespace, **kwargs: object) -> SimpleNamespace:
        self.events.append("chat.mark_completed")
        self.mark_completed_calls += 1
        message.content = kwargs["content"]
        message.model_name = kwargs["model_name"]
        message.prompt_version = kwargs["prompt_version"]
        message.completed_at = kwargs["completed_at"]
        message.generation_status = ChatGenerationStatus.COMPLETED
        return message

    async def update_last_message_at(self, session: SimpleNamespace, *, last_message_at: datetime) -> SimpleNamespace:
        self.events.append("chat.update_last_message_at")
        session.last_message_at = last_message_at
        return session

    async def commit_failed_message_pair(self, message: SimpleNamespace, **kwargs: object) -> SimpleNamespace:
        self.events.append("chat.commit_failed_message_pair")
        self.commits += 1
        message.generation_status = ChatGenerationStatus.FAILED
        message.error_code = kwargs["error_code"]
        message.error_message = kwargs["error_message"]
        message.completed_at = kwargs["completed_at"]
        message.content = None
        message.model_name = None
        message.prompt_version = None
        return message


def _service_fixture(
    *,
    engine: RecordingEngine,
    current_version_at_completion: bool = True,
    create_citations_error: Exception | None = None,
) -> tuple[ChatService, RecordingChatRepository, list[str], SimpleNamespace]:
    events: list[str] = []
    chat_session = SimpleNamespace(
        id=uuid4(),
        prescription_id=uuid4(),
        prescription_version_id=uuid4(),
        session_status=ChatSessionStatus.ACTIVE,
        last_message_at=datetime(2026, 11, 26, tzinfo=UTC),
    )
    chat_session.prescription = SimpleNamespace(active_version_id=chat_session.prescription_version_id)
    medications = [
        SimpleNamespace(
            medication_name="합성 테스트약",
            strength_text="100mg",
            dose_value=Decimal("1"),
            dose_unit="정",
            frequency_per_day=2,
            timing_text="식후",
            duration_days=7,
        )
    ]
    chat_repo = RecordingChatRepository(
        chat_session,
        events,
        current_version_at_completion=current_version_at_completion,
        create_citations_error=create_citations_error,
    )
    prescription_repo = RecordingPrescriptionRepository(medications, events)
    consent_gate = RecordingConsentGate(events)
    engine.events = events
    service = ChatService(
        prescription_repo,  # type: ignore[arg-type]
        chat_repo,  # type: ignore[arg-type]
        engine,
        consent_gate,  # type: ignore[arg-type]
    )
    return service, chat_repo, events, chat_session


def _supported_citation(*, display_order: int = 1, claim_key: str = "claim-1") -> ChatVerifiedCitation:
    return ChatVerifiedCitation(
        claim_key=claim_key,
        claim_text="식후 30분에 복용하는 것이 좋습니다.",
        claim_kind=ChatCitationClaimKind.CRITICAL.value,
        verdict=ChatCitationVerdict.SUPPORTED.value,
        source_code="MFDS_EDB",
        source_version="2026.11.01",
        locator="용법·용량 제1항",
        display_order=display_order,
        knowledge_chunk_id=uuid4(),
        content_sha256=VALID_SHA256,
        cited_text="식후 30분 경구 투여한다.",
    )


def _needs_verification_citation(*, display_order: int = 2, claim_key: str = "claim-2") -> ChatVerifiedCitation:
    return ChatVerifiedCitation(
        claim_key=claim_key,
        claim_text="물과 함께 복용하면 목 넘김이 편합니다.",
        claim_kind=ChatCitationClaimKind.NON_CRITICAL.value,
        verdict=ChatCitationVerdict.NEEDS_VERIFICATION.value,
        source_code="MFDS_EDB",
        source_version="2026.11.01",
        locator="주의사항 참고",
        display_order=display_order,
        knowledge_chunk_id=None,
        content_sha256=None,
        cited_text=None,
    )


async def test_send_message_backward_compatible_none_citations_creates_zero_rows() -> None:
    engine = RecordingEngine(
        result=ChatReplyOutput(
            content="안전한 합성 답변",
            model_name="model-id",
            prompt_version="chat-prompt-v6",
            citations=None,
        )
    )
    service, chat_repo, events, chat_session = _service_fixture(engine=engine)
    user = SimpleNamespace(id=uuid4())

    post_data = await service.send_message(
        user=user,  # type: ignore[arg-type]
        session_id=chat_session.id,
        request=SendChatMessageRequest(content="복용 시간 문의"),
    )
    messages = await service.list_messages(
        user=user,  # type: ignore[arg-type]
        session_id=chat_session.id,
    )

    assert post_data.citations is None
    assert chat_repo.create_citations_calls == 0
    assert chat_repo.persisted_citations == []
    assert "chat.create_citations" not in events
    assert [msg.citations for msg in messages] == [None, None]


async def test_send_message_active_empty_citations_returns_empty_list_and_zero_rows() -> None:
    engine = RecordingEngine(
        result=ChatReplyOutput(
            content="일반 안내 답변",
            model_name="model-id",
            prompt_version="chat-prompt-v6",
            citations=[],
        )
    )
    service, chat_repo, _, chat_session = _service_fixture(engine=engine)
    user = SimpleNamespace(id=uuid4())

    post_data = await service.send_message(
        user=user,  # type: ignore[arg-type]
        session_id=chat_session.id,
        request=SendChatMessageRequest(content="일반 질문"),
    )
    messages = await service.list_messages(
        user=user,  # type: ignore[arg-type]
        session_id=chat_session.id,
    )

    assert post_data.citations == []
    assert chat_repo.create_citations_calls == 0
    assert chat_repo.persisted_citations == []
    assert [msg.citations for msg in messages] == [None, None]


async def test_send_and_list_messages_persist_ordered_public_safe_citations_and_exclude_internal_fields() -> None:
    second = _needs_verification_citation(display_order=2, claim_key="claim-general")
    first = _supported_citation(display_order=1, claim_key="claim-dosage")
    engine = RecordingEngine(
        result=ChatReplyOutput(
            content="복용법 안내입니다.",
            model_name="model-id",
            prompt_version="chat-prompt-v6",
            # Intentionally unsorted input order to verify deterministic display_order sorting.
            citations=[second, first],
        )
    )
    service, chat_repo, events, chat_session = _service_fixture(engine=engine)
    user = SimpleNamespace(id=uuid4())

    post_data = await service.send_message(
        user=user,  # type: ignore[arg-type]
        session_id=chat_session.id,
        request=SendChatMessageRequest(content="어떻게 복용하나요?"),
    )
    messages = await service.list_messages(
        user=user,  # type: ignore[arg-type]
        session_id=chat_session.id,
    )

    assert events.index("chat.create_citations") < events.index("chat.mark_completed")
    assert chat_repo.create_citations_calls == 1
    assert [row.display_order for row in chat_repo.persisted_citations] == [1, 2]
    assert chat_repo.persisted_citations[0].knowledge_chunk_id == first.knowledge_chunk_id
    assert chat_repo.persisted_citations[0].content_sha256 == VALID_SHA256
    assert chat_repo.persisted_citations[0].claim_kind == ChatCitationClaimKind.CRITICAL
    assert chat_repo.persisted_citations[1].knowledge_chunk_id is None
    assert chat_repo.persisted_citations[1].content_sha256 is None

    expected_items = [
        ChatCitationItem(
            claim_key="claim-dosage",
            claim_text="식후 30분에 복용하는 것이 좋습니다.",
            verdict=ChatCitationVerdict.SUPPORTED,
            source_code="MFDS_EDB",
            source_version="2026.11.01",
            locator="용법·용량 제1항",
            display_order=1,
        ),
        ChatCitationItem(
            claim_key="claim-general",
            claim_text="물과 함께 복용하면 목 넘김이 편합니다.",
            verdict=ChatCitationVerdict.NEEDS_VERIFICATION,
            source_code="MFDS_EDB",
            source_version="2026.11.01",
            locator="주의사항 참고",
            display_order=2,
        ),
    ]
    assert post_data.citations == expected_items
    assert len(messages) == 2
    user_msg, assistant_msg = messages
    assert user_msg.role == ChatRole.USER
    assert user_msg.citations is None
    assert assistant_msg.role == ChatRole.ASSISTANT
    assert assistant_msg.citations == expected_items

    post_dumped = post_data.model_dump(mode="json")
    get_dumped = ChatMessageData.model_validate(assistant_msg).model_dump(mode="json")
    forbidden_internal_fields = {"knowledge_chunk_id", "content_sha256", "claim_kind", "cited_text"}
    for dumped_item in post_dumped["citations"] + get_dumped["citations"]:
        assert set(dumped_item) == {
            "claim_key",
            "claim_text",
            "verdict",
            "source_code",
            "source_version",
            "locator",
            "display_order",
        }
        assert forbidden_internal_fields.isdisjoint(dumped_item)


@pytest.mark.parametrize(
    "invalid_citation",
    [
        # CRITICAL + NEEDS_VERIFICATION (fail-closed CRM-09)
        ChatVerifiedCitation(
            claim_key="c1",
            claim_text="중요 용량 안내",
            claim_kind="CRITICAL",
            verdict="NEEDS_VERIFICATION",
            source_code="MFDS_EDB",
            source_version="2026.11.01",
            locator="용법·용량",
            display_order=1,
        ),
        # Invalid verdict (e.g., CONTRADICT / neutral)
        ChatVerifiedCitation(
            claim_key="c1",
            claim_text="모순된 안내",
            claim_kind="NON_CRITICAL",
            verdict="CONTRADICT",
            source_code="MFDS_EDB",
            source_version="2026.11.01",
            locator="주의사항",
            display_order=1,
        ),
        # SUPPORTED missing knowledge_chunk_id
        ChatVerifiedCitation(
            claim_key="c1",
            claim_text="근거 ID 누락",
            claim_kind="CRITICAL",
            verdict="SUPPORTED",
            source_code="MFDS_EDB",
            source_version="2026.11.01",
            locator="용법·용량",
            display_order=1,
            knowledge_chunk_id=None,
            content_sha256=VALID_SHA256,
        ),
        # SUPPORTED missing content_sha256
        ChatVerifiedCitation(
            claim_key="c1",
            claim_text="해시 누락",
            claim_kind="CRITICAL",
            verdict="SUPPORTED",
            source_code="MFDS_EDB",
            source_version="2026.11.01",
            locator="용법·용량",
            display_order=1,
            knowledge_chunk_id=uuid4(),
            content_sha256=None,
        ),
        # SUPPORTED with invalid hex digest
        ChatVerifiedCitation(
            claim_key="c1",
            claim_text="잘못된 해시",
            claim_kind="CRITICAL",
            verdict="SUPPORTED",
            source_code="MFDS_EDB",
            source_version="2026.11.01",
            locator="용법·용량",
            display_order=1,
            knowledge_chunk_id=uuid4(),
            content_sha256="NOT_A_VALID_SHA256",
        ),
        # Blank claim_key
        ChatVerifiedCitation(
            claim_key="   ",
            claim_text="유효한 내용",
            claim_kind="NON_CRITICAL",
            verdict="NEEDS_VERIFICATION",
            source_code="MFDS_EDB",
            source_version="2026.11.01",
            locator="주의사항",
            display_order=1,
        ),
        # Blank claim_text
        ChatVerifiedCitation(
            claim_key="c1",
            claim_text="   ",
            claim_kind="NON_CRITICAL",
            verdict="NEEDS_VERIFICATION",
            source_code="MFDS_EDB",
            source_version="2026.11.01",
            locator="주의사항",
            display_order=1,
        ),
        # Non-empty blank cited_text
        ChatVerifiedCitation(
            claim_key="c1",
            claim_text="유효한 주장",
            claim_kind="NON_CRITICAL",
            verdict="NEEDS_VERIFICATION",
            source_code="MFDS_EDB",
            source_version="2026.11.01",
            locator="주의사항",
            display_order=1,
            cited_text="   ",
        ),
        # Invalid display_order
        ChatVerifiedCitation(
            claim_key="c1",
            claim_text="유효한 주장",
            claim_kind="NON_CRITICAL",
            verdict="NEEDS_VERIFICATION",
            source_code="MFDS_EDB",
            source_version="2026.11.01",
            locator="주의사항",
            display_order=0,
        ),
    ],
)
async def test_send_message_rejects_invalid_citations_fail_closed_before_mark_completed(
    invalid_citation: ChatVerifiedCitation,
) -> None:
    engine = RecordingEngine(
        result=ChatReplyOutput(
            content="실패 처리되어야 할 답변",
            model_name="model-id",
            prompt_version="chat-prompt-v6",
            citations=[invalid_citation],
        )
    )
    service, chat_repo, events, chat_session = _service_fixture(engine=engine)
    user = SimpleNamespace(id=uuid4())

    with pytest.raises(ApiError) as captured:
        await service.send_message(
            user=user,  # type: ignore[arg-type]
            session_id=chat_session.id,
            request=SendChatMessageRequest(content="위험한 질문"),
        )

    assert captured.value.status_code == 500
    assert captured.value.code == "AI_RESPONSE_FAILED"
    assert chat_repo.mark_completed_calls == 0
    assert chat_repo.create_citations_calls == 0
    assert chat_repo.persisted_citations == []
    assert chat_repo.commits == 1
    assert events[-2:] == ["engine.reply", "chat.commit_failed_message_pair"]
    assistant = chat_repo.messages[1]
    assert assistant.generation_status == ChatGenerationStatus.FAILED
    assert assistant.content is None


async def test_send_message_rejects_duplicate_claim_key_or_display_order_fail_closed() -> None:
    dup_key = [
        _supported_citation(display_order=1, claim_key="dup"),
        _needs_verification_citation(display_order=2, claim_key="dup"),
    ]
    dup_order = [
        _supported_citation(display_order=1, claim_key="k1"),
        _needs_verification_citation(display_order=1, claim_key="k2"),
    ]
    for bad_citations in (dup_key, dup_order):
        engine = RecordingEngine(
            result=ChatReplyOutput(
                content="중복 키/순서 답변",
                model_name="model-id",
                prompt_version="chat-prompt-v6",
                citations=bad_citations,
            )
        )
        service, chat_repo, _, chat_session = _service_fixture(engine=engine)

        with pytest.raises(ApiError) as captured:
            await service.send_message(
                user=SimpleNamespace(id=uuid4()),  # type: ignore[arg-type]
                session_id=chat_session.id,
                request=SendChatMessageRequest(content="질문"),
            )

        assert captured.value.status_code == 500
        assert chat_repo.mark_completed_calls == 0
        assert chat_repo.persisted_citations == []
        assert chat_repo.commits == 1


@pytest.mark.parametrize(
    ("engine_error", "expected_code"),
    [
        (ChatTimeoutError("timeout"), "GATEWAY_TIMEOUT"),
        (ChatServiceUnavailableError("unavailable"), "SERVICE_UNAVAILABLE"),
        (ChatGenerationFailedError("failed"), "AI_RESPONSE_FAILED"),
        (RuntimeError("unexpected"), "AI_RESPONSE_FAILED"),
    ],
)
async def test_send_message_generator_failure_persists_zero_citations(
    engine_error: Exception,
    expected_code: str,
) -> None:
    service, chat_repo, _, chat_session = _service_fixture(engine=RecordingEngine(error=engine_error))

    with pytest.raises(ApiError) as captured:
        await service.send_message(
            user=SimpleNamespace(id=uuid4()),  # type: ignore[arg-type]
            session_id=chat_session.id,
            request=SendChatMessageRequest(content="실패 질문"),
        )

    assert captured.value.code == expected_code
    assert chat_repo.create_citations_calls == 0
    assert chat_repo.mark_completed_calls == 0
    assert chat_repo.persisted_citations == []
    assert chat_repo.commits == 1
    assert chat_repo.messages[1].generation_status == ChatGenerationStatus.FAILED
    assert chat_repo.messages[1].content is None


async def test_send_message_stale_version_or_repository_failure_prevents_mark_completed() -> None:
    # 1. Stale prescription version prevents citation creation and completion
    engine = RecordingEngine(
        result=ChatReplyOutput(
            content="구버전 답변",
            model_name="model-id",
            prompt_version="chat-prompt-v6",
            citations=[_supported_citation()],
        )
    )
    stale_service, stale_repo, _, stale_session = _service_fixture(
        engine=engine,
        current_version_at_completion=False,
    )
    with pytest.raises(ApiError) as stale_exc:
        await stale_service.send_message(
            user=SimpleNamespace(id=uuid4()),  # type: ignore[arg-type]
            session_id=stale_session.id,
            request=SendChatMessageRequest(content="경합 질문"),
        )
    assert stale_exc.value.code == "PRESCRIPTION_VERSION_CONFLICT"
    assert stale_repo.create_citations_calls == 0
    assert stale_repo.mark_completed_calls == 0
    assert stale_repo.persisted_citations == []

    # 2. Repository create_citations failure aborts before mark_completed
    repo_error = RuntimeError("db flush failed")
    failing_service, failing_repo, _, failing_session = _service_fixture(
        engine=engine,
        create_citations_error=repo_error,
    )
    with pytest.raises(RuntimeError) as caught:
        await failing_service.send_message(
            user=SimpleNamespace(id=uuid4()),  # type: ignore[arg-type]
            session_id=failing_session.id,
            request=SendChatMessageRequest(content="저장 실패 질문"),
        )
    assert caught.value is repo_error
    assert failing_repo.mark_completed_calls == 0
    assert failing_repo.persisted_citations == []


async def test_chat_repository_create_citations_orders_rows_and_list_messages_eager_loads_citations() -> None:
    class FakeScalarResult:
        def scalars(self) -> "FakeScalarResult":
            return self

        def all(self) -> list[ChatMessage]:
            return []

    class CapturingAsyncSession:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.flushed = 0
            self.statements: list[object] = []

        def add_all(self, items: Sequence[object]) -> None:
            self.added.extend(items)

        async def flush(self) -> None:
            self.flushed += 1

        async def execute(self, statement: object) -> FakeScalarResult:
            self.statements.append(statement)
            return FakeScalarResult()

    session = CapturingAsyncSession()
    repo = ChatRepository(session)  # type: ignore[arg-type]
    message_id = uuid4()
    c2 = _needs_verification_citation(display_order=2, claim_key="claim-2")
    c1 = _supported_citation(display_order=1, claim_key="claim-1")

    created = await repo.create_citations(message_id=message_id, citations=[c2, c1])
    assert repo.create_citations is not None
    assert session.flushed == 1
    assert [row.display_order for row in created] == [1, 2]
    assert [row.claim_key for row in created] == ["claim-1", "claim-2"]
    assert all(isinstance(row, ChatCitation) for row in created)

    chat_session = ChatSession(
        id=uuid4(),
        prescription_id=uuid4(),
        prescription_version_id=uuid4(),
        profile_id=uuid4(),
    )
    await repo.list_messages(session=chat_session)
    assert len(session.statements) == 1
    compiled = str(
        cast(ClauseElement, session.statements[0]).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "FROM chat_message" in compiled
    assert "ORDER BY chat_message.message_seq" in compiled
