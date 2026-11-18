"""Synthetic persisted authorities for the CR-A Sync Guide integration tests.

The canonical Bundle and retrieval binding are built through the production
builders. Only the policy evaluator and executor are replaced in the tests.
"""

import os
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401
from app.core import config
from app.core.db.databases import Base
from app.models.knowledge import RagKnowledgeIndexMember
from app.models.prescriptions import Prescription, PrescriptionVersionMedication
from app.models.rag_runtime import (
    GuideRetrievalBindingManifest,
    RagRuntimeBundleStatus,
    RagRuntimeEnvironment,
    RagRuntimeReleaseBundle,
)
from app.models.rag_source import RagSourceSnapshot, RagSourceSnapshotMember
from app.models.users import User
from app.services.guide_retrieval_binding import (
    GuideRetrievalBindingBuildRequest,
    create_guide_retrieval_binding_manifest,
)
from app.tests.rag.test_ai_job_preflight_context_repository import (
    _create_canonical_runtime_graph,
    _create_identification,
    _create_prescription,
    _create_user,
    _retrieval_configuration_projection,
)


@pytest_asyncio.fixture
async def cra_session_factory(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Own a unique schema in the explicit CR-A PostgreSQL test container only."""

    if os.environ.get("DOSEY_CRA_TEST_ALLOWED") != "1":
        pytest.skip("CR-A real PostgreSQL tests require DOSEY_CRA_TEST_ALLOWED=1 and the dedicated test DB")
    if os.environ.get("CRA_TEST_DB_PORT") != "55217":
        raise RuntimeError("CR-A integration requires the dedicated opt-in database port")
    if config.DB_USER != "cra_test" or config.DB_NAME != "test" or config.DB_EXPOSE_PORT != 55217:
        raise RuntimeError("CR-A integration refused a non-dedicated PostgreSQL configuration")

    # Fail closed on accidental Provider, telemetry, SMTP, or other outbound TCP.
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded_connect(sock: socket.socket, address: object) -> None:
        if not isinstance(address, tuple) or address[:2] != ("127.0.0.1", 55217):
            raise RuntimeError("CR-A fixture permits only its dedicated localhost PostgreSQL socket")
        original_connect(sock, address)

    def guarded_connect_ex(sock: socket.socket, address: object) -> int:
        if not isinstance(address, tuple) or address[:2] != ("127.0.0.1", 55217):
            raise RuntimeError("CR-A fixture permits only its dedicated localhost PostgreSQL socket")
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    schema = f"cra_{uuid4().hex}"
    url = URL.create(
        "postgresql+asyncpg",
        username=config.DB_USER,
        password=config.DB_PASSWORD,
        host="127.0.0.1",
        port=55217,
        database="test",
    )
    admin = create_async_engine(url, poolclass=NullPool)
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},test_extensions,public"}},
    )
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


@pytest_asyncio.fixture
async def db_session(cra_session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Opt-in alias for the pre-CR-A wiring test under ``--noconftest``."""

    from app.core.db.databases import get_db_session
    from app.main import fastapi_app

    async with cra_session_factory() as session:

        async def override_get_db_session() -> AsyncIterator[AsyncSession]:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        fastapi_app.dependency_overrides[get_db_session] = override_get_db_session
        try:
            yield session
        finally:
            fastapi_app.dependency_overrides.pop(get_db_session, None)


@dataclass(frozen=True, slots=True)
class CraRuntimeFixture:
    user: User
    prescription: Prescription
    environment: RagRuntimeEnvironment
    bundle: RagRuntimeReleaseBundle
    retrieval_binding: GuideRetrievalBindingManifest
    knowledge_snapshot: RagSourceSnapshot
    knowledge_member: RagSourceSnapshotMember
    knowledge_source_code: str
    medication_id: UUID


async def seed_cra_runtime(session: AsyncSession) -> CraRuntimeFixture:
    """Persist one exact, canonical LOCAL authority graph and matched medication."""

    user, profile = await _create_user(session)
    prescription = await _create_prescription(session, user=user, profile=profile)
    medication = await session.scalar(
        select(PrescriptionVersionMedication).where(
            PrescriptionVersionMedication.prescription_version_id == prescription.active_version_id
        )
    )
    assert medication is not None
    await _create_identification(session, medication=medication)
    manifest, bundle, environment, _, _, index, member_binding = await _create_canonical_runtime_graph(
        session, actor=user
    )
    # The builder deliberately leaves a candidate BUILDING. This isolated test
    # seeds an already approved synthetic runtime, with its canonical rows intact.
    bundle.bundle_status = RagRuntimeBundleStatus.READY
    await session.flush()
    binding = await create_guide_retrieval_binding_manifest(
        session,
        GuideRetrievalBindingBuildRequest(
            runtime_release_bundle_id=bundle.id,
            runtime_execution_manifest_id=manifest.id,
            knowledge_index_id=index.id,
            member_bindings=(member_binding,),
            retrieval_configuration=_retrieval_configuration_projection(),
        ),
    )
    snapshot = await session.get(RagSourceSnapshot, member_binding.source_snapshot_id)
    member = await session.get(RagSourceSnapshotMember, member_binding.source_snapshot_member_id)
    assert snapshot is not None and member is not None
    # Source code is the historical code pinned in the exact index member.
    pinned_member = await session.scalar(
        select(RagKnowledgeIndexMember).where(RagKnowledgeIndexMember.knowledge_index_id == index.id)
    )
    assert pinned_member is not None
    return CraRuntimeFixture(
        user=user,
        prescription=prescription,
        environment=environment,
        bundle=bundle,
        retrieval_binding=binding,
        knowledge_snapshot=snapshot,
        knowledge_member=member,
        knowledge_source_code=pinned_member.source_code,
        medication_id=medication.id,
    )
