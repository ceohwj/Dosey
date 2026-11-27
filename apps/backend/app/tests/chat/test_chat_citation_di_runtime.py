"""Tests for local opt-in chat citation DI wiring, app-DB retriever, warmup, and corpus loader."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from openai import AsyncOpenAI
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.dependencies.services as services_dep
from app.core.chat_citation_retrieval import (
    CACHE_SCHEMA_VERSION,
    AppDbChatEvidenceRetriever,
    ChatCitationRetrievalError,
)
from app.core.config import Config, Env
from app.dependencies.security import get_request_user
from app.dependencies.services import (
    get_chat_citation_retriever,
    get_chat_citation_validator,
    get_chat_engine,
)
from app.main import app, fastapi_app
from app.models.chat import ChatGenerationStatus, ChatSession
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.models.medical_documents import MedicalDocument
from app.models.ocr import OcrJob
from app.models.prescriptions import Medication, Prescription, PrescriptionVersion, PrescriptionVersionMedication
from app.models.profiles import Profile, ProfileType
from app.models.user_consents import ConsentPurpose, ConsentStatus, UserConsent
from app.models.users import Gender, User
from app.services.chat_ai.prompt import CLAIM_CITATION_PROMPT_VERSION, PROMPT_VERSION
from app.services.chat_ai.schemas import ProviderChatResponse
from app.services.chat_generator_engine import ChatGeneratorEngine
from app.tests.fixtures.prescription_fingerprint import fingerprint_values
from rag_runtime.chat_claim_citation_validator import NliTriple, RulePlusNliClaimValidator
from scripts.load_local_chat_eval_corpus import load_corpus_into_session

TEST_ORIGIN = "http://localhost:5173"


class _NonClosingSessionContext:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *args: object) -> None:
        return None


def _shared_session_factory(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], lambda: _NonClosingSessionContext(session))


def _unit_vector(dim: int, hot_index: int = 0) -> list[float]:
    vec = [0.0] * dim
    vec[hot_index] = 1.0
    return vec


def _two_axis_vector(dim: int, x: float, y: float) -> list[float]:
    vec = [0.0] * dim
    vec[0] = x
    vec[1] = y
    return vec


def _cache_key(text: str, *, model: str = "text-embedding-3-large", dimensions: int = 1536) -> str:
    return hashlib.sha256(f"{model}:{dimensions}:{text}".encode()).hexdigest()


def _local_config_overrides(**extra: object) -> dict[str, object]:
    return {
        "ENV": Env.LOCAL,
        "DB_HOST": "127.0.0.1",
        "DB_USER": "postgres",
        "DB_PASSWORD": "postgres-local-test-password",
        "DB_NAME": "dosey_test",
        "PUBLIC_TRACK_F_ENABLED": False,
        "CHAT_CLOSED_DEMO_RAG_ENABLED": False,
        **extra,
    }


def _staging_safe_overrides(**extra: object) -> dict[str, object]:
    return {
        "ENV": Env.STAGING,
        "DB_HOST": "10.0.0.10",
        "DB_USER": "postgres",
        "DB_PASSWORD": "postgres-staging-test-password",
        "DB_NAME": "dosey_staging",
        "CORS_ORIGINS": ["https://staging.example.com"],
        "COOKIE_SECURE": True,
        "JWT_SECRET_KEY": "s" * 64,
        "ADMIN_SECRET_KEY": "a" * 64,
        "IDEMPOTENCY_HMAC_KEY": "i" * 64,
        "IDEMPOTENCY_SNAPSHOT_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "PUBLIC_TRACK_F_ENABLED": False,
        "CHAT_CLOSED_DEMO_RAG_ENABLED": False,
        **extra,
    }


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


def test_chat_citation_rag_config_guards() -> None:
    local_cfg = Config.model_validate(
        _local_config_overrides(
            CHAT_CITATION_RAG_ENABLED=True,
            CHAT_CITATION_RAG_TOP_K=5,
            CHAT_CITATION_RAG_MIN_DENSE_SCORE=0.50,
        )
    )
    assert local_cfg.CHAT_CITATION_RAG_ENABLED is True

    with pytest.raises(ValidationError, match="local"):
        Config.model_validate(_staging_safe_overrides(CHAT_CITATION_RAG_ENABLED=True))

    with pytest.raises(ValidationError, match="PUBLIC_TRACK_F_ENABLED"):
        Config.model_validate(_local_config_overrides(PUBLIC_TRACK_F_ENABLED=True, CHAT_CITATION_RAG_ENABLED=True))

    with pytest.raises(ValidationError, match="CHAT_CLOSED_DEMO_RAG_ENABLED"):
        Config.model_validate(
            _local_config_overrides(CHAT_CLOSED_DEMO_RAG_ENABLED=True, CHAT_CITATION_RAG_ENABLED=True)
        )

    with pytest.raises(ValidationError, match="CHAT_CITATION_RAG_TOP_K"):
        Config.model_validate(_local_config_overrides(CHAT_CITATION_RAG_TOP_K=6))

    with pytest.raises(ValidationError, match="CHAT_CITATION_RAG_MIN_DENSE_SCORE"):
        Config.model_validate(_local_config_overrides(CHAT_CITATION_RAG_MIN_DENSE_SCORE=1.2))


def test_rule_plus_nli_validator_warmup_loads_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def _fake_loader() -> Any:
        nonlocal calls
        calls += 1

        def _scorer(premise: str, hypothesis: str) -> NliTriple:
            del premise, hypothesis
            return (0.90, 0.05, 0.05)

        return _scorer

    monkeypatch.setattr("rag_runtime.chat_claim_citation_validator.load_mdeberta_nli_scorer", _fake_loader)
    validator = RulePlusNliClaimValidator()
    assert calls == 0
    validator.warmup()
    assert calls == 1
    validator.warmup()
    assert calls == 1


def test_get_chat_engine_di_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    dummy_client = AsyncOpenAI(api_key="sk-test-dummy")
    context = SimpleNamespace(client_ip="127.0.0.1")

    monkeypatch.setattr(services_dep.config, "CHAT_CLOSED_DEMO_RAG_ENABLED", False)
    monkeypatch.setattr(services_dep.config, "CHAT_CITATION_RAG_ENABLED", False)
    fake_request_missing = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    assert get_chat_citation_retriever(fake_request_missing) is None  # type: ignore[arg-type]
    assert get_chat_citation_validator(fake_request_missing) is None  # type: ignore[arg-type]

    default_engine = get_chat_engine(
        client=dummy_client,
        context=context,  # type: ignore[arg-type]
        closed_demo_retriever=None,
        chat_citation_retriever=None,
        chat_citation_validator=None,
    )
    assert isinstance(default_engine, ChatGeneratorEngine)
    assert default_engine._closed_demo_retriever is None
    assert default_engine._citation_validator is None
    assert PROMPT_VERSION == "chat-prompt-v6"

    monkeypatch.setattr(services_dep.config, "CHAT_CITATION_RAG_ENABLED", True)
    with pytest.raises(RuntimeError, match="startup"):
        get_chat_citation_retriever(fake_request_missing)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="startup"):
        get_chat_citation_validator(fake_request_missing)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="CHAT_CITATION_RAG_ENABLED"):
        get_chat_engine(
            client=dummy_client,
            context=context,  # type: ignore[arg-type]
            closed_demo_retriever=None,
            chat_citation_retriever=None,
            chat_citation_validator=None,
        )

    fake_retriever = SimpleNamespace()
    fake_validator = RulePlusNliClaimValidator(scorer=lambda _p, _h: (0.9, 0.05, 0.05))
    fake_request_ready = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                chat_citation_retriever=fake_retriever,
                chat_citation_validator=fake_validator,
            )
        )
    )
    assert get_chat_citation_retriever(fake_request_ready) is fake_retriever  # type: ignore[arg-type]
    assert get_chat_citation_validator(fake_request_ready) is fake_validator  # type: ignore[arg-type]

    enabled_engine = get_chat_engine(
        client=dummy_client,
        context=context,  # type: ignore[arg-type]
        closed_demo_retriever=None,
        chat_citation_retriever=fake_retriever,  # type: ignore[arg-type]
        chat_citation_validator=fake_validator,
    )
    assert isinstance(enabled_engine, ChatGeneratorEngine)
    assert enabled_engine._closed_demo_retriever is fake_retriever
    assert enabled_engine._citation_validator is fake_validator


async def test_app_db_retriever_hybrid_rrf_and_abstention_with_postgres(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    corpus_path = tmp_path / "corpus_chunks.jsonl"
    dosage_content = "성인은 1회 1~2정(500~1000mg)을 1일 3~4회 필요 시 복용하며, 1일 최대 4000mg을 초과하지 않습니다."
    storage_content = "기밀용기에 담아 실온(1~30도)에 보관하고 직사광선을 피하십시오."
    unrelated_content = "칼슘 흡수 보조를 위해 충분한 수분과 함께 섭취하는 것이 권장됩니다."
    rows = [
        {
            "chunk_id": "195900034:dosage:0",
            "item_seq": "195900034",
            "item_name": "타이레놀정500밀리그람",
            "section": "용법용량",
            "source_code": "MFDS_DATA_GO_KR_15095677",
            "source_version": "local-eval@1",
            "content": dosage_content,
        },
        {
            "chunk_id": "195900034:storage:0",
            "item_seq": "195900034",
            "item_name": "타이레놀정500밀리그람",
            "section": "저장방법",
            "source_code": "MFDS_DATA_GO_KR_15095677",
            "source_version": "local-eval@1",
            "content": storage_content,
        },
        {
            "chunk_id": "200001234:precautions:0",
            "item_seq": "200001234",
            "item_name": "칼슘보충정",
            "section": "주의사항",
            "source_code": "MFDS_DATA_GO_KR_15095677",
            "source_version": "local-eval@1",
            "content": unrelated_content,
        },
    ]
    corpus_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    # Idempotent loader verification
    assert await load_corpus_into_session(db_session, corpus_path=corpus_path) == (2, 3)
    assert await load_corpus_into_session(db_session, corpus_path=corpus_path) == (0, 0)

    docs = (await db_session.scalars(select(KnowledgeDocument))).all()
    chunks = (await db_session.scalars(select(KnowledgeChunk))).all()
    assert len(docs) >= 2
    assert len(chunks) >= 3

    dosage_indexed = f"타이레놀정500밀리그람 [용법용량] {dosage_content}"
    storage_indexed = f"타이레놀정500밀리그람 [저장방법] {storage_content}"
    unrelated_indexed = f"칼슘보충정 [주의사항] {unrelated_content}"
    cache_path = tmp_path / "embeddings_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": CACHE_SCHEMA_VERSION,
                "model": "text-embedding-3-large",
                "dimensions": 1536,
                "entries": {
                    _cache_key(dosage_indexed): _unit_vector(1536, 0),
                    _cache_key(storage_indexed): _two_axis_vector(1536, 0.8, 0.6),
                    _cache_key(unrelated_indexed): _unit_vector(1536, 2),
                },
            }
        ),
        encoding="utf-8",
    )

    async def _hit_embedder(question: str) -> tuple[float, ...]:
        del question
        return tuple(_unit_vector(1536, 0))

    retriever = AppDbChatEvidenceRetriever(
        session_factory=_shared_session_factory(db_session),
        embedding_cache_path=cache_path,
        query_embedder=_hit_embedder,
        top_k=2,
        min_dense_score=0.50,
    )
    results = await retriever.retrieve("타이레놀 하루 최대 복용량은 얼마인가요?")
    assert len(results) == 2
    assert results[0].display_order == 1
    assert "4000mg" in results[0].content.reveal()
    assert results[0].source_code == "MFDS_DATA_GO_KR_15095677"

    async def _miss_embedder(question: str) -> tuple[float, ...]:
        del question
        return tuple(_unit_vector(1536, 10))

    abstaining_retriever = AppDbChatEvidenceRetriever(
        session_factory=_shared_session_factory(db_session),
        embedding_cache_path=cache_path,
        query_embedder=_miss_embedder,
        top_k=2,
        min_dense_score=0.50,
    )
    with pytest.raises(ChatCitationRetrievalError, match="threshold"):
        await abstaining_retriever.retrieve("오늘 서울 날씨가 어떤가요?")


async def test_di_enabled_end_to_end_post_and_get_chat_citations(
    client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = uuid4().hex[:10]
    owner = User(
        email=f"cite-di-{token}@example.com",
        hashed_password="synthetic-hash",
        name="합성 DI 사용자",
        gender=Gender.FEMALE,
        birthday=date(1992, 3, 15),
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
        original_file_name=f"di-{token}.jpg",
        object_key=f"synthetic/chat-di/{token}.jpg",
        file_mime_type="image/jpeg",
        file_size_bytes=128,
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
        prescribed_date=date(2026, 11, 20),
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
            "duration_days": 5,
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
            duration_days=5,
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
            duration_days=5,
            display_order=1,
        )
    )

    dosage_content = f"성인은 1일 1회 1정을 식후에 충분한 물과 함께 복용하십시오 ({token})."
    corpus_path = tmp_path / "di_e2e_corpus.jsonl"
    corpus_path.write_text(
        json.dumps(
            {
                "chunk_id": f"{token}:dosage:0",
                "item_seq": f"99{token[:6]}",
                "item_name": f"합성 해열진통제-{token}",
                "section": "용법용량",
                "source_code": "MFDS_EVAL",
                "source_version": "local-eval@1",
                "content": dosage_content,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    assert await load_corpus_into_session(db_session, corpus_path=corpus_path) == (1, 1)

    seeded_chunk = (
        await db_session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.vector_store_key == f"{token}:dosage:0"))
    ).one()

    cache_path = tmp_path / "di_e2e_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": CACHE_SCHEMA_VERSION,
                "model": "text-embedding-3-large",
                "dimensions": 1536,
                "entries": {_cache_key(seeded_chunk.chunk_text): _unit_vector(1536, 0)},
            }
        ),
        encoding="utf-8",
    )

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

    class _FakeOpenAIChatProvider:
        def __init__(self, client: AsyncOpenAI, **kwargs: object) -> None:
            del client, kwargs

        async def generate(self, **kwargs: object) -> ProviderChatResponse:
            del kwargs
            return ProviderChatResponse(
                content=json.dumps(
                    {
                        "answer": "성인은 1일 1회 1정을 식후에 복용하세요.",
                        "claims": [
                            {
                                "claim_key": "c-dosage",
                                "claim_text": "성인은 1일 1회 1정을 식후에 복용하세요.",
                                "claim_kind": "critical",
                                "evidence_chunk_ids": [str(seeded_chunk.id)],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                model_name="synthetic-chat-v7",
            )

    async def _embed_hit(question: str) -> tuple[float, ...]:
        del question
        return tuple(_unit_vector(1536, 0))

    retriever = AppDbChatEvidenceRetriever(
        session_factory=_shared_session_factory(db_session),
        embedding_cache_path=cache_path,
        query_embedder=_embed_hit,
        top_k=3,
        min_dense_score=0.50,
    )
    validator = RulePlusNliClaimValidator(scorer=lambda _p, _h: (0.94, 0.04, 0.02))

    monkeypatch.setattr(services_dep.config, "CHAT_CLOSED_DEMO_RAG_ENABLED", False)
    monkeypatch.setattr(services_dep.config, "CHAT_CITATION_RAG_ENABLED", True)
    monkeypatch.setattr(services_dep, "ChatOpenAIResponsesClient", _FakeOpenAIChatProvider)

    engine = get_chat_engine(
        client=AsyncOpenAI(api_key="sk-test-dummy"),
        context=SimpleNamespace(client_ip="127.0.0.1"),  # type: ignore[arg-type]
        closed_demo_retriever=None,
        chat_citation_retriever=retriever,
        chat_citation_validator=validator,
    )

    fastapi_app.dependency_overrides[get_request_user] = lambda: SimpleNamespace(
        id=owner.id,
        account_status="ACTIVE",
        is_active=True,
    )
    fastapi_app.dependency_overrides[get_chat_engine] = lambda: engine

    post_resp = await client.post(
        f"/api/v1/chat-sessions/{chat_session.id}/messages",
        json={"content": "언제 복용하나요?"},
        headers={"Origin": TEST_ORIGIN},
    )
    assert post_resp.status_code == 201
    post_data = post_resp.json()["data"]
    assert post_data["generation_status"] == ChatGenerationStatus.COMPLETED.value
    assert post_data["prompt_version"] == CLAIM_CITATION_PROMPT_VERSION
    assert len(post_data["citations"]) == 1
    assert post_data["citations"][0]["verdict"] == "SUPPORTED"
    assert post_data["citations"][0]["source_code"] == "MFDS_EVAL"

    get_resp = await client.get(
        f"/api/v1/chat-sessions/{chat_session.id}/messages",
        headers={"Origin": TEST_ORIGIN},
    )
    assert get_resp.status_code == 200
    assistant_msgs = [m for m in get_resp.json()["data"]["messages"] if m["role"] == "ASSISTANT"]
    assert len(assistant_msgs) == 1
    assert len(assistant_msgs[0]["citations"]) == 1
