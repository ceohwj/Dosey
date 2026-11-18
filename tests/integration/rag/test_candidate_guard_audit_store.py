"""Candidate audit PostgreSQL table and fail-closed repository smoke in an isolated schema."""

from uuid import uuid4

import pytest
from sqlalchemy import Table, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401
from app.core import config
from app.core.db.databases import Base
from app.models.rag_candidate_guard_audit import RagCandidateRequestGuardAudit
from app.repositories.rag_candidate_guard_audit_repository import (
    CandidateGuardAuditCorruptError,
    RagCandidateGuardAuditRepository,
)
from rag_runtime.candidate_guard_audit import CandidateGuardAuditValidationError
from tests.integration.rag.test_candidate_guard_audit_transaction import sessions as audit_sessions  # noqa: F401


def _tables() -> list[Table]:
    seen: dict[str, Table] = {}
    stack = [RagCandidateRequestGuardAudit.__table__]
    while stack:
        table = stack.pop()
        if table.name in seen:
            continue
        seen[table.name] = table
        stack.extend(key.column.table for key in table.foreign_keys)
    return [seen[name] for name in sorted(seen)]


@pytest.mark.asyncio
async def test_postgres_schema_and_exact_missing_read():
    if "test" not in config.DB_NAME.lower():
        pytest.fail("requires disposable test database")
    schema = "candidate_guard_audit_" + uuid4().hex
    admin = create_async_engine(config.database_url, poolclass=NullPool, hide_parameters=True)
    engine = create_async_engine(
        config.database_url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=_tables()))
        async with AsyncSession(engine) as session:
            repo = RagCandidateGuardAuditRepository(session)
            assert await repo.get_exact(uuid4(), "a" * 64) is None
            assert await repo.delete_for_account_withdrawal(uuid4()) == 0
            with pytest.raises(CandidateGuardAuditValidationError):
                await repo.get_exact(uuid4(), "NOT_A_HASH")
            assert await session.scalar(text("SELECT count(*) FROM rag_candidate_request_guard_audit")) == 0
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True])
async def test_record_retry_tamper_and_withdrawal_cleanup(audit_sessions, shared):  # noqa: F811
    from dataclasses import replace

    from sqlalchemy import func, select

    from app.models.rag_citation_authorization import (
        RagCitationAuthorizationReceipt,
        RagCitationAuthorizationSourceDecision,
    )
    from app.models.rag_request_authority import (
        RagRequestGuardAuthority,
        RagRequestMemberDecision,
        RagRequestSourceDecision,
    )
    from app.models.rag_request_guard_runtime_binding import RagRequestGuardRuntimeBinding
    from app.models.rag_runtime import AiJobExecutionContext, RagRuntimeEnvironment
    from app.repositories.rag_request_guard_runtime_binding_repository import RagRequestGuardRuntimeBindingRepository
    from app.services.candidate_guard_audit import CandidateGuardAuditCoordinator, CandidateGuardPolicyDenied
    from rag_runtime.candidate_guard_audit import compute_candidate_guard_audit_hash
    from tests.integration.rag.test_candidate_guard_audit_transaction import (
        _envelope,
        _seed_synthetic_capture,
    )
    from tests.integration.rag.test_candidate_search_synthetic_flow import _seed_flow

    # The transaction fixture omits Guide/Citation consumers; cleanup must query them.
    seen = {}
    stack = [
        RagCitationAuthorizationSourceDecision.__table__,
        RagCitationAuthorizationReceipt.__table__,
        AiJobExecutionContext.__table__,
        RagRequestMemberDecision.__table__,
    ]
    while stack:
        table = stack.pop()
        if table.name in seen:
            continue
        seen[table.name] = table
        stack.extend(key.column.table for key in table.foreign_keys)
    async with audit_sessions.kw["bind"].begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=list(seen.values()), checkfirst=True)
        )

    fixture = await _seed_flow(audit_sessions)
    capture = await _seed_synthetic_capture(audit_sessions, fixture)
    async with audit_sessions.begin() as session:
        envelope = await _envelope(session, fixture, capture)

        async def deny():
            raise CandidateGuardPolicyDenied(envelope)

        await CandidateGuardAuditCoordinator(session).execute_mutation(deny)
        digest = compute_candidate_guard_audit_hash(envelope.observation)
        assert (
            await RagCandidateGuardAuditRepository(session).record(envelope.observation, envelope.proof_manifest)
            == digest
        )
        if shared:
            # A separate #806 consumer of the same semantic #713 Guard retains the parent.
            other_binding = replace(envelope.binding, request_guard_decision_id=uuid4())
            await RagRequestGuardRuntimeBindingRepository(session).record(other_binding)
    async with audit_sessions.begin() as session:
        environment = await session.get(RagRuntimeEnvironment, fixture.environment_id)
        environment.environment_revision += 1
    async with audit_sessions() as session:
        assert (
            await RagCandidateGuardAuditRepository(session).get_exact(
                envelope.observation.request_guard_decision_id, digest
            )
            is not None
        )
        schema = await session.scalar(text("SELECT current_schema()"))
    async with audit_sessions.begin() as session:
        row = await session.get(RagCandidateRequestGuardAudit, envelope.observation.request_guard_decision_id)
        original_projection = row.projection
        row.projection = {**original_projection, "query_digest": "f" * 64}
    async with audit_sessions() as session:
        with pytest.raises(CandidateGuardAuditCorruptError):
            await RagCandidateGuardAuditRepository(session).get_exact(
                envelope.observation.request_guard_decision_id, digest
            )
    async with audit_sessions.begin() as session:
        row = await session.get(RagCandidateRequestGuardAudit, envelope.observation.request_guard_decision_id)
        row.projection = original_projection
    async with audit_sessions.begin() as session:
        binding = await session.get(RagRequestGuardRuntimeBinding, envelope.observation.request_guard_decision_id)
        original_binding_hash = binding.artifact_content_sha256
        binding.artifact_content_sha256 = "f" * 64
    async with audit_sessions() as session:
        with pytest.raises(CandidateGuardAuditValidationError):
            await RagCandidateGuardAuditRepository(session).record(envelope.observation, envelope.proof_manifest)
    async with audit_sessions.begin() as session:
        binding = await session.get(RagRequestGuardRuntimeBinding, envelope.observation.request_guard_decision_id)
        binding.artifact_content_sha256 = original_binding_hash
    assert schema.startswith("candidate_audit_")
    role = "candidate_cleanup_" + uuid4().hex
    readable = (
        "rag_candidate_request_guard_audit",
        "rag_request_guard_runtime_binding",
        "rag_request_guard_authority",
        "rag_request_source_decision",
        "rag_request_member_decision",
        "rag_citation_authorization_source_decision",
        "rag_citation_authorization_receipt",
        "ai_job_execution_context",
    )
    deletable = readable[:5]
    engine = audit_sessions.kw["bind"]
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOINHERIT'))
        await connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"'))
        for name in readable:
            await connection.execute(text(f'GRANT SELECT ON TABLE "{schema}"."{name}" TO "{role}"'))
        for name in deletable:
            await connection.execute(text(f'GRANT DELETE ON TABLE "{schema}"."{name}" TO "{role}"'))
    try:
        async with audit_sessions.begin() as session:
            await session.execute(text(f'SET LOCAL ROLE "{role}"'))
            assert await RagCandidateGuardAuditRepository(session).delete_for_account_withdrawal(fixture.owner_id) == 1
    finally:
        async with engine.begin() as connection:
            for name in readable:
                await connection.execute(text(f'REVOKE ALL ON TABLE "{schema}"."{name}" FROM "{role}"'))
            await connection.execute(text(f'REVOKE USAGE ON SCHEMA "{schema}" FROM "{role}"'))
            await connection.execute(text(f'DROP ROLE "{role}"'))
    async with audit_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RagCandidateRequestGuardAudit)) == 0
        expected = 1 if shared else 0
        assert await session.scalar(select(func.count()).select_from(RagRequestGuardRuntimeBinding)) == expected
        assert await session.scalar(select(func.count()).select_from(RagRequestGuardAuthority)) == expected
        assert await session.scalar(select(func.count()).select_from(RagRequestSourceDecision)) == expected
