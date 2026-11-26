"""Real PostgreSQL + FastAPI E2E tests for runtime `rule_plus_nli` chat citation validation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.security import get_request_user
from app.dependencies.services import get_chat_engine
from app.main import app, fastapi_app
from app.models.chat import (
    ChatCitation,
    ChatCitationClaimKind,
    ChatCitationVerdict,
    ChatGenerationStatus,
    ChatMessage,
    ChatRole,
    ChatSession,
)
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.models.medical_documents import MedicalDocument
from app.models.ocr import OcrJob
from app.models.prescriptions import Medication, Prescription, PrescriptionVersion, PrescriptionVersionMedication
from app.models.profiles import Profile, ProfileType
from app.models.user_consents import ConsentPurpose, ConsentStatus, UserConsent
from app.models.users import Gender, User
from app.services.chat_ai.schemas import ProviderChatResponse
from app.services.chat_generator_engine import ChatGeneratorEngine
from app.tests.fixtures.prescription_fingerprint import fingerprint_values
from rag_runtime.chat_claim_citation_validator import NliTriple, RulePlusNliClaimValidator

TEST_ORIGIN = "http://localhost:5173"


class _FakeSensitiveText:
    def __init__(self, text: str) -> None:
        self._text = text

    def reveal(self) -> str:
        return self._text


class _FakeRetrievedEvidence:
    def __init__(
        self,
        *,
        display_order: int,
        knowledge_chunk_id: UUID,
        source_code: str,
        source_version: str,
        locator: str,
        content: str,
    ) -> None:
        self.display_order = display_order
        self.knowledge_chunk_id = knowledge_chunk_id
        self.source_code = source_code
        self.source_version = source_version
        self.locator = locator
        self.content = _FakeSensitiveText(content)


class _FakeClosedDemoRetriever:
    def __init__(self, items: tuple[_FakeRetrievedEvidence, ...]) -> None:
        self._items = items

    async def retrieve(self, question: str) -> tuple[_FakeRetrievedEvidence, ...]:
        del question
        return self._items


class _FakeStructuredChatProvider:
    def __init__(self, payload: dict[str, object]) -> None:
        self._json = json.dumps(payload, ensure_ascii=False)

    async def generate(self, **kwargs: object) -> ProviderChatResponse:
        del kwargs
        return ProviderChatResponse(content=self._json, model_name="synthetic-chat-v7")


@dataclass(frozen=True)
class _RuntimeE2EFixture:
    owner_id: UUID
    session_id: UUID
    chunk_dosage_id: UUID
    chunk_dosage_text: str
    chunk_storage_id: UUID
    chunk_storage_text: str


@pytest.fixture(autouse=True)
def _clear_dependency_overrides() -> Iterator[None]:
    yield
    fastapi_app.dependency_overrides.pop(get_request_user, None)
    fastapi_app.dependency_overrides.pop(get_chat_engine, None)


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    del db_session
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as test_client:
        yield test_client


@pytest_asyncio.fixture
async def runtime_e2e_fixture(db_session: AsyncSession) -> _RuntimeE2EFixture:
    token = uuid4().hex[:12]
    owner = User(
        email=f"cite-e2e-{token}@example.com",
        hashed_password="synthetic-hash",
        name="합성 검증 사용자",
        gender=Gender.FEMALE,
        birthday=date(1990, 5, 10),
        phone_number=f"010{uuid4().int % 100_000_000:08d}",
    )
    db_session.add(owner)
    await db_session.flush()

    profile = Profile(user_id=owner.id, profile_type=ProfileType.SELF, display_name=owner.name)
    db_session.add(profile)
    await db_session.flush()

    document = MedicalDocument(
        uploaded_by=owner.id,
        profile_id=profile.id,
        original_file_name=f"synthetic-{token}.jpg",
        object_key=f"synthetic/chat-citation-e2e/{token}.jpg",
        file_mime_type="image/jpeg",
        file_size_bytes=256,
    )
    db_session.add(document)
    await db_session.flush()

    ocr_job = OcrJob(document_id=document.id)
    db_session.add(ocr_job)
    await db_session.flush()

    version_id = uuid4()
    prescription = Prescription(
        active_version_id=version_id,
        document_id=document.id,
        source_ocr_job_id=ocr_job.id,
        profile_id=profile.id,
        prescribed_date=date(2026, 11, 1),
        confirmed_at=datetime.now(UTC),
    )
    db_session.add(prescription)
    await db_session.flush()

    med_payload = [
        {
            "medication_name": f"합성 해열진통제-{token}",
            "dose_value": Decimal("1.000"),
            "dose_unit": "정",
            "frequency_per_day": 1,
            "timing_text": "식후",
            "duration_days": 7,
            "display_order": 1,
        }
    ]
    db_session.add(
        PrescriptionVersion(
            **fingerprint_values(prescription.prescribed_date, med_payload),
            id=version_id,
            prescription_id=prescription.id,
            version_number=1,
            prescribed_date=prescription.prescribed_date,
            confirmed_at=prescription.confirmed_at,
        )
    )
    await db_session.flush()

    db_session.add(
        Medication(
            prescription_id=prescription.id,
            medication_name=f"합성 해열진통제-{token}",
            dose_value=Decimal("1.000"),
            dose_unit="정",
            frequency_per_day=1,
            timing_text="식후",
            duration_days=7,
            display_order=1,
        )
    )
    db_session.add(
        PrescriptionVersionMedication(
            medication_count=1,
            prescription_version_id=version_id,
            medication_name=f"합성 해열진통제-{token}",
            dose_value=Decimal("1.000"),
            dose_unit="정",
            frequency_per_day=1,
            timing_text="식후",
            duration_days=7,
            display_order=1,
        )
    )

    knowledge_doc = KnowledgeDocument(
        title=f"합성 MFDS 라벨-{token}",
        publisher="식품의약품안전처",
        source_url=f"https://example.invalid/mfds/{token}",
        document_version="2026.11.01",
    )
    db_session.add(knowledge_doc)
    await db_session.flush()

    chunk_dosage_text = "성인은 1일 1회 1정을 식후 30분에 충분한 물과 함께 복용하십시오."
    chunk_storage_text = "직사광선을 피하여 실온의 건조한 장소에 밀폐 보관하십시오."
    chunk_dosage = KnowledgeChunk(
        knowledge_document_id=knowledge_doc.id,
        chunk_index=0,
        chunk_text=chunk_dosage_text,
        content_hash=hashlib.sha256(chunk_dosage_text.encode("utf-8")).hexdigest(),
        normalization_version="norm-v1",
    )
    chunk_storage = KnowledgeChunk(
        knowledge_document_id=knowledge_doc.id,
        chunk_index=1,
        chunk_text=chunk_storage_text,
        content_hash=hashlib.sha256(chunk_storage_text.encode("utf-8")).hexdigest(),
        normalization_version="norm-v1",
    )
    db_session.add_all([chunk_dosage, chunk_storage])
    await db_session.flush()

    chat_session = ChatSession(
        prescription_id=prescription.id,
        prescription_version_id=version_id,
        profile_id=profile.id,
    )
    db_session.add_all(
        [
            chat_session,
            UserConsent(
                user_id=owner.id,
                purpose=ConsentPurpose.CHAT,
                status=ConsentStatus.GRANTED,
                policy_version="chat-consent.v1",
                granted_at=datetime.now(UTC),
            ),
        ]
    )
    await db_session.flush()
    await db_session.commit()

    return _RuntimeE2EFixture(
        owner_id=owner.id,
        session_id=chat_session.id,
        chunk_dosage_id=chunk_dosage.id,
        chunk_dosage_text=chunk_dosage_text,
        chunk_storage_id=chunk_storage.id,
        chunk_storage_text=chunk_storage_text,
    )


def _wire_owner_and_engine(owner_id: UUID, engine: ChatGeneratorEngine) -> None:
    fastapi_app.dependency_overrides[get_request_user] = lambda: SimpleNamespace(
        id=owner_id,
        account_status="ACTIVE",
        is_active=True,
    )
    fastapi_app.dependency_overrides[get_chat_engine] = lambda: engine


async def test_e2e_post_and_get_persist_validated_citations_without_internal_fields(
    client: AsyncClient,
    db_session: AsyncSession,
    runtime_e2e_fixture: _RuntimeE2EFixture,
) -> None:
    retriever = _FakeClosedDemoRetriever(
        (
            _FakeRetrievedEvidence(
                display_order=1,
                knowledge_chunk_id=runtime_e2e_fixture.chunk_dosage_id,
                source_code="MFDS_EDB",
                source_version="2026.11.01",
                locator="용법·용량 제1항",
                content=runtime_e2e_fixture.chunk_dosage_text,
            ),
            _FakeRetrievedEvidence(
                display_order=2,
                knowledge_chunk_id=runtime_e2e_fixture.chunk_storage_id,
                source_code="MFDS_EDB",
                source_version="2026.11.01",
                locator="보관방법 제2항",
                content=runtime_e2e_fixture.chunk_storage_text,
            ),
        )
    )
    provider = _FakeStructuredChatProvider(
        {
            "answer": "성인은 1일 1회 1정을 식후 30분에 복용하세요. 종이 외포장은 분리배출이 가능합니다.",
            "claims": [
                {
                    "claim_key": "c-dosage",
                    "claim_text": "성인은 1일 1회 1정을 식후 30분에 복용하세요.",
                    "claim_kind": "critical",
                    "evidence_chunk_ids": [str(runtime_e2e_fixture.chunk_dosage_id)],
                },
                {
                    "claim_key": "c-packaging",
                    "claim_text": "종이 외포장은 분리배출이 가능합니다.",
                    "claim_kind": "non_critical",
                    "evidence_chunk_ids": [str(runtime_e2e_fixture.chunk_storage_id)],
                },
            ],
        }
    )

    def _scorer(premise: str, hypothesis: str) -> NliTriple:
        del premise
        if "식후 30분" in hypothesis:
            return (0.93, 0.05, 0.02)
        return (0.12, 0.80, 0.08)

    engine = ChatGeneratorEngine(
        provider=provider,
        model="synthetic-model",
        timeout_seconds=2.0,
        closed_demo_retriever=retriever,  # type: ignore[arg-type]
        citation_validator=RulePlusNliClaimValidator(scorer=_scorer),
    )
    _wire_owner_and_engine(runtime_e2e_fixture.owner_id, engine)

    post_response = await client.post(
        f"/api/v1/chat-sessions/{runtime_e2e_fixture.session_id}/messages",
        json={"content": "이 약은 하루에 몇 번 먹나요?"},
        headers={"Origin": TEST_ORIGIN},
    )

    assert post_response.status_code == 201
    post_data = post_response.json()["data"]
    assert post_data["generation_status"] == "COMPLETED"
    assert post_data["prompt_version"] == "chat-prompt-v7-claim-citation"

    expected_public_citations = [
        {
            "claim_key": "c-dosage",
            "claim_text": "성인은 1일 1회 1정을 식후 30분에 복용하세요.",
            "verdict": "SUPPORTED",
            "source_code": "MFDS_EDB",
            "source_version": "2026.11.01",
            "locator": "용법·용량 제1항",
            "display_order": 1,
        },
        {
            "claim_key": "c-packaging",
            "claim_text": "종이 외포장은 분리배출이 가능합니다.",
            "verdict": "NEEDS_VERIFICATION",
            "source_code": "MFDS_EDB",
            "source_version": "2026.11.01",
            "locator": "보관방법 제2항",
            "display_order": 2,
        },
    ]
    assert post_data["citations"] == expected_public_citations
    forbidden_internal_fields = {"knowledge_chunk_id", "content_sha256", "claim_kind", "cited_text"}
    for item in post_data["citations"]:
        assert forbidden_internal_fields.isdisjoint(item)

    assistant_message_id = UUID(post_data["assistant_message_id"])
    rows = (
        await db_session.scalars(
            select(ChatCitation)
            .where(ChatCitation.message_id == assistant_message_id)
            .order_by(ChatCitation.display_order)
        )
    ).all()
    assert len(rows) == 2
    assert rows[0].claim_kind == ChatCitationClaimKind.CRITICAL
    assert rows[0].verdict == ChatCitationVerdict.SUPPORTED
    assert rows[0].knowledge_chunk_id == runtime_e2e_fixture.chunk_dosage_id
    assert rows[0].content_sha256 == hashlib.sha256(runtime_e2e_fixture.chunk_dosage_text.encode("utf-8")).hexdigest()
    assert rows[0].cited_text == runtime_e2e_fixture.chunk_dosage_text

    assert rows[1].claim_kind == ChatCitationClaimKind.NON_CRITICAL
    assert rows[1].verdict == ChatCitationVerdict.NEEDS_VERIFICATION
    assert rows[1].knowledge_chunk_id is None
    assert rows[1].content_sha256 is None
    assert rows[1].cited_text is None

    get_response = await client.get(f"/api/v1/chat-sessions/{runtime_e2e_fixture.session_id}/messages")
    assert get_response.status_code == 200
    messages = get_response.json()["data"]["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "USER"
    assert messages[0]["citations"] is None
    assert messages[1]["role"] == "ASSISTANT"
    assert messages[1]["citations"] == expected_public_citations


async def test_e2e_critical_contradict_returns_500_and_persists_failed_message_with_zero_citations(
    client: AsyncClient,
    db_session: AsyncSession,
    runtime_e2e_fixture: _RuntimeE2EFixture,
) -> None:
    retriever = _FakeClosedDemoRetriever(
        (
            _FakeRetrievedEvidence(
                display_order=1,
                knowledge_chunk_id=runtime_e2e_fixture.chunk_dosage_id,
                source_code="MFDS_EDB",
                source_version="2026.11.01",
                locator="용법·용량 제1항",
                content=runtime_e2e_fixture.chunk_dosage_text,
            ),
        )
    )
    provider = _FakeStructuredChatProvider(
        {
            "answer": "하루에 2회 복용하세요.",
            "claims": [
                {
                    "claim_key": "c-bad-freq",
                    "claim_text": "성인은 1일 2회 1정씩 식후에 복용합니다.",
                    "claim_kind": "critical",
                    "evidence_chunk_ids": [str(runtime_e2e_fixture.chunk_dosage_id)],
                }
            ],
        }
    )

    engine = ChatGeneratorEngine(
        provider=provider,
        model="synthetic-model",
        timeout_seconds=2.0,
        closed_demo_retriever=retriever,  # type: ignore[arg-type]
        citation_validator=RulePlusNliClaimValidator(scorer=lambda _p, _h: (0.95, 0.03, 0.02)),
    )
    _wire_owner_and_engine(runtime_e2e_fixture.owner_id, engine)

    response = await client.post(
        f"/api/v1/chat-sessions/{runtime_e2e_fixture.session_id}/messages",
        json={"content": "하루에 두 번 먹어도 되나요?"},
        headers={"Origin": TEST_ORIGIN},
    )

    assert response.status_code == 500
    assert response.json()["code"] == "AI_RESPONSE_FAILED"

    persisted_messages = (
        await db_session.scalars(
            select(ChatMessage)
            .where(ChatMessage.session_id == runtime_e2e_fixture.session_id)
            .order_by(ChatMessage.message_seq)
        )
    ).all()
    assert len(persisted_messages) == 2
    user_msg, assistant_msg = persisted_messages
    assert user_msg.role == ChatRole.USER
    assert assistant_msg.role == ChatRole.ASSISTANT
    assert assistant_msg.generation_status == ChatGenerationStatus.FAILED
    assert assistant_msg.content is None
    assert assistant_msg.model_name is None
    assert assistant_msg.prompt_version is None

    citation_rows = (
        await db_session.scalars(select(ChatCitation).where(ChatCitation.message_id == assistant_msg.id))
    ).all()
    assert citation_rows == []


async def test_postgresql_check_constraints_reject_invalid_chat_citation_rows(
    db_session: AsyncSession,
    runtime_e2e_fixture: _RuntimeE2EFixture,
) -> None:
    assistant_msg = ChatMessage(
        session_id=runtime_e2e_fixture.session_id,
        message_seq=1,
        role=ChatRole.ASSISTANT,
        content="제약 검증용 메시지",
        generation_status=ChatGenerationStatus.COMPLETED,
    )
    db_session.add(assistant_msg)
    await db_session.flush()

    # 1. chk_chat_citation_fail_closed_verdict: CRITICAL + NEEDS_VERIFICATION forbidden
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                ChatCitation(
                    message_id=assistant_msg.id,
                    knowledge_chunk_id=None,
                    claim_key="bad-fail-closed",
                    claim_text="중요 주장 미검증",
                    claim_kind=ChatCitationClaimKind.CRITICAL,
                    verdict=ChatCitationVerdict.NEEDS_VERIFICATION,
                    source_code="MFDS_EDB",
                    source_version="2026.11.01",
                    locator="용법·용량 제1항",
                    content_sha256=None,
                    cited_text=None,
                    display_order=1,
                )
            )
            await db_session.flush()

    # 2. chk_chat_citation_supported_shape: SUPPORTED missing knowledge_chunk_id/content_sha256 forbidden
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                ChatCitation(
                    message_id=assistant_msg.id,
                    knowledge_chunk_id=None,
                    claim_key="bad-supported-shape",
                    claim_text="근거 누락 SUPPORTED",
                    claim_kind=ChatCitationClaimKind.NON_CRITICAL,
                    verdict=ChatCitationVerdict.SUPPORTED,
                    source_code="MFDS_EDB",
                    source_version="2026.11.01",
                    locator="보관방법 제2항",
                    content_sha256=None,
                    cited_text=None,
                    display_order=2,
                )
            )
            await db_session.flush()
