"""Isolated synthetic PostgreSQL capture store round trip."""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import Table, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401
from ai_worker.tasks.rag.source_ingestion.artifacts import RawArtifactMetadata
from ai_worker.tasks.rag.source_ingestion.checksums import raw_manifest_checksum
from app.core import config
from app.core.db.databases import Base
from app.models.rag_source import (
    RagSource,
    RagSourceEndpoint,
    RagSourceIngestionArtifact,
    RagSourceIngestionRun,
    RagSourceOperation,
    RagSourceSnapshot,
)
from app.models.rag_source_capture_observation import RagSourceCaptureObservation as Row
from app.repositories.rag_source_capture_observation_repository import (
    RagSourceCaptureObservationRepository,
    SourceCaptureConflictError,
    SourceCaptureIsolationError,
    raw_manifest_hash,
)
from infra.python.source_capture_role_policy import apply_source_capture_role_policy
from rag_runtime.source_capture_observation import SourceCaptureIssuerRef, SourceCaptureObservation

pytestmark = pytest.mark.asyncio
_SCHEMA = "source_capture_test_" + uuid4().hex[:12]
_NOW = datetime(2026, 11, 4, tzinfo=UTC)


def _required_tables() -> list[Table]:
    seen: dict[str, Table] = {}
    stack = [Row.__table__, RagSourceIngestionArtifact.__table__]
    while stack:
        table = stack.pop()
        if table.name in seen:
            continue
        seen[table.name] = table
        stack.extend(key.column.table for key in table.foreign_keys)
    return [seen[key] for key in sorted(seen)]


@pytest_asyncio.fixture(scope="module")
async def db() -> AsyncIterator[async_sessionmaker]:
    if (
        config.DB_HOST not in {"127.0.0.1", "localhost"}
        or config.DB_NAME != "test"
        or config.DB_PORT != config.DB_EXPOSE_PORT
    ):
        pytest.fail("capture store tests require a loopback synthetic test database")
    engine = create_async_engine(
        config.database_url,
        poolclass=NullPool,
        isolation_level="REPEATABLE READ",
        connect_args={"server_settings": {"search_path": _SCHEMA}},
    )
    admin = create_async_engine(config.database_url, poolclass=NullPool)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{_SCHEMA}"'))
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=_required_tables()))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{_SCHEMA}" CASCADE'))
        await admin.dispose()


async def _seed(session, *, status="SUCCEEDED", raw_char="a"):
    source = RagSource(source_code=f"SYN_{uuid4().hex}", display_name="synthetic")
    session.add(source)
    await session.flush()
    endpoint = RagSourceEndpoint(source_id=source.id, endpoint_code="EP", display_name="synthetic")
    session.add(endpoint)
    await session.flush()
    op = RagSourceOperation(endpoint_id=endpoint.id, operation_code="LIST", display_name="synthetic")
    session.add(op)
    await session.flush()
    artifact = RagSourceIngestionArtifact(
        ingestion_run_id=uuid4(),
        page_number=1,
        artifact_kind="RAW_RESPONSE",
        artifact_key="p1",
        storage_backend="synthetic",
        object_key="synthetic-key",
        raw_checksum=raw_char * 64,
        byte_size=4,
        content_type="application/json",
    )
    manifest = raw_manifest_hash([artifact])
    assert manifest == raw_manifest_checksum(
        [
            RawArtifactMetadata(
                artifact_key="p1", raw_checksum=raw_char * 64, byte_size=4, content_type="application/json"
            )
        ]
    )
    snapshot = RagSourceSnapshot(
        operation_id=op.id,
        source_version=f"v-{uuid4().hex}",
        raw_manifest_checksum=manifest if status == "SUCCEEDED" else "f" * 64,
        canonical_checksum="b" * 64,
        schema_version="v1",
        parser_version="v1",
        normalization_version="v1",
        canonicalization_spec_version="v1",
        endpoint_receipt_hash="e" * 64,
        record_count=1,
        rejected_record_count=0,
        verification_status="PENDING",
        collected_at=_NOW,
    )
    session.add(snapshot)
    await session.flush()
    run = RagSourceIngestionRun(
        id=artifact.ingestion_run_id,
        operation_id=op.id,
        run_group_key=uuid4().hex,
        snapshot_id=snapshot.id,
        run_status=status,
        attempt_number=1,
        started_at=_NOW,
        finished_at=_NOW + timedelta(seconds=1),
    )
    session.add(run)
    session.add(artifact)
    await session.flush()
    obs = SourceCaptureObservation(
        capture_observation_id=uuid4(),
        ingestion_run_id=run.id,
        source_snapshot_id=snapshot.id,
        source_version=snapshot.source_version,
        canonical_checksum=snapshot.canonical_checksum,
        source_code=source.source_code,
        endpoint_code=endpoint.endpoint_code,
        operation_code=op.operation_code,
        capture_protocol_version="v1",
        issuer_ref=SourceCaptureIssuerRef("source_live_capture_issuer", "1", "c" * 64),
        request_profile_sha256="d" * 64,
        capture_origin="LIVE_PROVIDER",
        capture_started_at=_NOW,
        capture_completed_at=_NOW + timedelta(seconds=1),
        recorded_at=_NOW + timedelta(seconds=2),
        raw_manifest_sha256=manifest,
        endpoint_contract_receipt_sha256="e" * 64,
        full_scan_completed=True,
        primary_key_validation_passed=True,
        page_count=1,
        record_count=1,
    )
    return obs, artifact


async def test_record_retry_exact_read_and_conflicts(db):
    async with db.begin() as session:
        obs, artifact = await _seed(session)
        repo = RagSourceCaptureObservationRepository(session)
        digest = await repo.record(obs)
        assert await repo.record(obs) == digest
        assert await repo.get_exact(obs.capture_observation_id, digest) == obs
        assert await repo.get_exact(obs.capture_observation_id, "0" * 64) is None
        with pytest.raises(SourceCaptureConflictError):
            await repo.record(replace(obs, raw_manifest_sha256="f" * 64))
        with pytest.raises(SourceCaptureConflictError):
            await repo.record(replace(obs, capture_observation_id=uuid4()))
    async with db.begin() as session:
        repo = RagSourceCaptureObservationRepository(session)
        assert await repo.get_exact(obs.capture_observation_id, digest) == obs
        await session.execute(
            update(Row)
            .where(Row.capture_observation_id == obs.capture_observation_id)
            .values(canonical_checksum="f" * 64)
        )
        session.expire_all()
        assert await repo.get_exact(obs.capture_observation_id, digest) is None


async def test_no_change_uses_its_own_raw_artifacts_and_rollback(db):
    async with db() as session:
        async with session.begin():
            obs, artifact = await _seed(session, status="NO_CHANGE", raw_char="a")
            repo = RagSourceCaptureObservationRepository(session)
            digest = await repo.record(obs)
            assert await repo.get_exact(obs.capture_observation_id, digest) == obs
        await session.rollback()
    async with db.begin() as session:
        obs, artifact = await _seed(session, status="NO_CHANGE", raw_char="b")
        repo = RagSourceCaptureObservationRepository(session)
        with pytest.raises(SourceCaptureConflictError):
            await repo.record(replace(obs, raw_manifest_sha256="f" * 64))
        digest = await repo.record(obs)
        assert await repo.get_exact(obs.capture_observation_id, digest) == obs
    async with db.begin() as session:
        obs, artifact = await _seed(session)
        repo = RagSourceCaptureObservationRepository(session)
        digest = await repo.record(obs)
        await session.rollback()
    async with db.begin() as session:
        assert (
            await RagSourceCaptureObservationRepository(session).get_exact(obs.capture_observation_id, digest) is None
        )


async def test_capture_role_policy_denies_reader_insert(db):
    admin = create_async_engine(config.database_url, poolclass=NullPool)
    reader = "capture_reader_" + uuid4().hex[:10]
    issuer = "capture_issuer_" + uuid4().hex[:10]
    async with db.begin() as session:
        obs, _ = await _seed(session)
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE ROLE "{reader}" NOLOGIN NOINHERIT'))
            await conn.execute(text(f'CREATE ROLE "{issuer}" NOLOGIN NOINHERIT'))
            await conn.execute(text(f'GRANT INSERT ON TABLE "{_SCHEMA}".rag_source_ingestion_run TO "{issuer}"'))
            with pytest.raises(ValueError, match="metadata write grants"):
                await apply_source_capture_role_policy(conn, schema=_SCHEMA, reader=reader, issuer=issuer)
            await conn.execute(text(f'REVOKE INSERT ON TABLE "{_SCHEMA}".rag_source_ingestion_run FROM "{issuer}"'))
            await conn.execute(
                text(f'GRANT UPDATE (raw_checksum) ON TABLE "{_SCHEMA}".rag_source_ingestion_artifact TO "{issuer}"')
            )
            with pytest.raises(ValueError, match="metadata write grants"):
                await apply_source_capture_role_policy(conn, schema=_SCHEMA, reader=reader, issuer=issuer)
            await conn.execute(
                text(f'REVOKE UPDATE (raw_checksum) ON TABLE "{_SCHEMA}".rag_source_ingestion_artifact FROM "{issuer}"')
            )
            await apply_source_capture_role_policy(conn, schema=_SCHEMA, reader=reader, issuer=issuer)
            assert await conn.scalar(
                text("SELECT has_table_privilege(:role, :table, 'SELECT')"),
                {"role": reader, "table": f"{_SCHEMA}.rag_source_capture_observation"},
            )
            assert not await conn.scalar(
                text("SELECT has_table_privilege(:role, :table, 'INSERT')"),
                {"role": reader, "table": f"{_SCHEMA}.rag_source_capture_observation"},
            )
            assert await conn.scalar(
                text("SELECT has_table_privilege(:role, :table, 'INSERT')"),
                {"role": issuer, "table": f"{_SCHEMA}.rag_source_capture_observation"},
            )
            assert not await conn.scalar(
                text("SELECT has_table_privilege(:role, :table, 'UPDATE')"),
                {"role": issuer, "table": f"{_SCHEMA}.rag_source_capture_observation"},
            )
            async with conn.begin_nested() as savepoint:
                await conn.execute(text(f'SET LOCAL ROLE "{reader}"'))
                with pytest.raises(DBAPIError, match="permission denied"):
                    await conn.execute(text(f'INSERT INTO "{_SCHEMA}".rag_source_capture_observation DEFAULT VALUES'))
                await savepoint.rollback()
        role_engine = create_async_engine(
            config.database_url,
            poolclass=NullPool,
            isolation_level="REPEATABLE READ",
            connect_args={"server_settings": {"search_path": _SCHEMA}},
        )
        try:
            async with async_sessionmaker(role_engine, expire_on_commit=False).begin() as session:
                await session.execute(text(f'SET LOCAL ROLE "{issuer}"'))
                repo = RagSourceCaptureObservationRepository(session)
                digest = await repo.record(obs)
                assert await repo.record(obs) == digest
            async with async_sessionmaker(role_engine, expire_on_commit=False).begin() as session:
                await session.execute(text(f'SET LOCAL ROLE "{reader}"'))
                assert (
                    await RagSourceCaptureObservationRepository(session).get_exact(obs.capture_observation_id, digest)
                    == obs
                )
        finally:
            await role_engine.dispose()
    finally:
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP OWNED BY "{reader}", "{issuer}"'))
            await conn.execute(text(f'DROP ROLE IF EXISTS "{reader}"'))
            await conn.execute(text(f'DROP ROLE IF EXISTS "{issuer}"'))
        await admin.dispose()


async def test_read_committed_refuses_capture_record_without_writing(db):
    async with db.begin() as session:
        obs, _ = await _seed(session)
    rc_engine = create_async_engine(
        config.database_url,
        poolclass=NullPool,
        isolation_level="READ COMMITTED",
        connect_args={"server_settings": {"search_path": _SCHEMA}},
    )
    try:
        async with async_sessionmaker(rc_engine, expire_on_commit=False).begin() as session:
            with pytest.raises(SourceCaptureIsolationError):
                await RagSourceCaptureObservationRepository(session).record(obs)
            assert (
                await session.scalar(
                    select(Row.capture_observation_id).where(Row.capture_observation_id == obs.capture_observation_id)
                )
                is None
            )
    finally:
        await rc_engine.dispose()
