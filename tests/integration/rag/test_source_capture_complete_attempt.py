"""Synthetic complete writer attempt followed by a separate capture issuer session."""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import Table, delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401
from ai_worker.adapters.sqlalchemy_source_snapshot_repository import SqlAlchemySourceSnapshotRepository
from ai_worker.tasks.rag.source_ingestion.artifacts import RawArtifactMetadata, StoredRawArtifact
from ai_worker.tasks.rag.source_ingestion.checksums import raw_manifest_checksum
from ai_worker.tasks.rag.source_ingestion.snapshot_lifecycle import SnapshotRunRecord
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
from app.models.rag_source_capture_observation import RagSourceCaptureObservation as CaptureRow
from app.repositories.rag_source_capture_observation_repository import (
    RagSourceCaptureObservationRepository,
    SourceCaptureConflictError,
)
from app.services.source_capture_admission import SyntheticPrivateAcquisition, SyntheticSourceCaptureAdmission
from rag_runtime.source_capture_observation import SourceCaptureIssuerRef, SourceCaptureObservation

pytestmark = pytest.mark.asyncio
_SCHEMA = "source_capture_complete_" + uuid4().hex[:12]
_NOW = datetime(2026, 11, 4, tzinfo=UTC)
_ISSUER = SourceCaptureIssuerRef("source_live_capture_issuer", "2.0", "c" * 64)


def _required_tables() -> list[Table]:
    seen: dict[str, Table] = {}
    stack = [CaptureRow.__table__, RagSourceIngestionArtifact.__table__]
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
        pytest.fail("capture test requires a loopback synthetic test database")
    engine = create_async_engine(
        config.database_url,
        poolclass=NullPool,
        isolation_level="REPEATABLE READ",
        connect_args={"server_settings": {"search_path": _SCHEMA}},
    )
    admin = create_async_engine(config.database_url, poolclass=NullPool)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{_SCHEMA}"'))
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=_required_tables()))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{_SCHEMA}" CASCADE'))
        await admin.dispose()


async def test_complete_writer_run_to_v2_issuer_and_historical_v1(db):
    metadata = RawArtifactMetadata("p1", "a" * 64, 4, "application/json")
    manifest = raw_manifest_checksum([metadata])
    artifact = StoredRawArtifact(1, metadata, "synthetic", "synthetic-key")
    started = _NOW
    completed = started + timedelta(seconds=1)
    recorded = started + timedelta(seconds=2)

    async with db.begin() as session:
        source = RagSource(source_code="SYN_" + uuid4().hex, display_name="synthetic")
        session.add(source)
        await session.flush()
        endpoint = RagSourceEndpoint(source_id=source.id, endpoint_code="EP", display_name="synthetic")
        session.add(endpoint)
        await session.flush()
        operation = RagSourceOperation(endpoint_id=endpoint.id, operation_code="LIST", display_name="synthetic")
        session.add(operation)
        await session.flush()
        snapshot = RagSourceSnapshot(
            operation_id=operation.id,
            source_version="v-" + uuid4().hex,
            raw_manifest_checksum=manifest,
            canonical_checksum="b" * 64,
            schema_version="v1",
            parser_version="v1",
            normalization_version="v1",
            canonicalization_spec_version="v1",
            endpoint_receipt_hash="e" * 64,
            record_count=1,
            rejected_record_count=0,
            verification_status="PENDING",
            collected_at=started,
        )
        session.add(snapshot)
        await session.flush()
        old_run = RagSourceIngestionRun(
            operation_id=operation.id,
            run_group_key=uuid4().hex,
            snapshot_id=snapshot.id,
            run_status="SUCCEEDED",
            attempt_number=1,
            started_at=started,
            finished_at=completed,
        )
        session.add(old_run)
        await session.flush()
        session.add(
            RagSourceIngestionArtifact(
                ingestion_run_id=old_run.id,
                page_number=1,
                artifact_kind="RAW_RESPONSE",
                artifact_key="p1",
                storage_backend="synthetic",
                object_key="synthetic-key-old",
                raw_checksum=metadata.raw_checksum,
                byte_size=metadata.byte_size,
                content_type=metadata.content_type,
            )
        )
        await session.flush()
        old_obs = SourceCaptureObservation(
            capture_observation_id=uuid4(),
            ingestion_run_id=old_run.id,
            source_snapshot_id=snapshot.id,
            source_version=snapshot.source_version,
            canonical_checksum=snapshot.canonical_checksum,
            source_code=source.source_code,
            endpoint_code=endpoint.endpoint_code,
            operation_code=operation.operation_code,
            capture_protocol_version="v1",
            issuer_ref=SourceCaptureIssuerRef("source_live_capture_issuer", "1.0", "c" * 64),
            request_profile_sha256="d" * 64,
            capture_origin="LIVE_PROVIDER",
            capture_started_at=started,
            capture_completed_at=completed,
            recorded_at=recorded,
            raw_manifest_sha256=manifest,
            endpoint_contract_receipt_sha256="e" * 64,
            full_scan_completed=True,
            primary_key_validation_passed=True,
            page_count=1,
            record_count=1,
        )
        old_hash = await RagSourceCaptureObservationRepository(session).record(old_obs)

    record = SnapshotRunRecord(
        operation_id=operation.id,
        snapshot_id=snapshot.id,
        run_group_key=uuid4().hex,
        attempt_number=1,
        run_status="SUCCEEDED",
        started_at=started,
        finished_at=completed,
        duration_ms=1000,
    )
    async with db.begin() as writer_session:
        new_run_id = await SqlAlchemySourceSnapshotRepository(writer_session).persist_complete_attempt(
            record, (artifact,)
        )
        assert new_run_id != old_run.id

    # Server-held private facts are fixed from the synthetic acquisition and
    # Writer result before a capture observation is projected from them.
    context = SyntheticPrivateAcquisition(
        new_run_id=new_run_id,
        capture_observation_id=uuid4(),
        source_snapshot_id=snapshot.id,
        source_code=source.source_code,
        endpoint_code=endpoint.endpoint_code,
        operation_code=operation.operation_code,
        issuer_ref=_ISSUER,
        request_profile_sha256="d" * 64,
        capture_started_at=started,
        capture_completed_at=completed,
        recorded_at=recorded,
        origin="SIMULATED_LIVE",
    )
    new_obs = SourceCaptureObservation(
        capture_observation_id=context.capture_observation_id,
        ingestion_run_id=context.new_run_id,
        source_snapshot_id=context.source_snapshot_id,
        source_version=snapshot.source_version,
        canonical_checksum=snapshot.canonical_checksum,
        source_code=context.source_code,
        endpoint_code=context.endpoint_code,
        operation_code=context.operation_code,
        capture_protocol_version="v2",
        issuer_ref=context.issuer_ref,
        request_profile_sha256=context.request_profile_sha256,
        capture_origin="LIVE_PROVIDER",
        capture_started_at=context.capture_started_at,
        capture_completed_at=context.capture_completed_at,
        recorded_at=context.recorded_at,
        raw_manifest_sha256=manifest,
        endpoint_contract_receipt_sha256=snapshot.endpoint_receipt_hash,
        full_scan_completed=True,
        primary_key_validation_passed=True,
        page_count=1,
        record_count=1,
    )
    async with db.begin() as issuer_session:
        repo = RagSourceCaptureObservationRepository(issuer_session)
        admission = SyntheticSourceCaptureAdmission(repo, issuer_ref=_ISSUER)
        digest = await admission.record(new_obs, context)
        assert await admission.record(new_obs, context) == digest
        for changed_observation, changed_context in (
            (replace(new_obs, ingestion_run_id=old_run.id), context),
            (new_obs, replace(context, new_run_id=old_run.id)),
            (new_obs, replace(context, origin="RAW_REPLAY")),
            (replace(new_obs, issuer_ref=old_obs.issuer_ref), context),
            (replace(new_obs, request_profile_sha256="0" * 64), context),
            (replace(new_obs, recorded_at=recorded + timedelta(seconds=1)), context),
        ):
            with pytest.raises(SourceCaptureConflictError):
                await admission.record(changed_observation, changed_context)
        assert await repo.get_exact(old_obs.capture_observation_id, old_hash) == old_obs

    async with db.begin() as verify_session:
        repo = RagSourceCaptureObservationRepository(verify_session)
        assert await repo.get_exact(new_obs.capture_observation_id, digest) == new_obs
        assert await repo.get_exact(old_obs.capture_observation_id, old_hash) == old_obs
        assert (
            len(
                (
                    await verify_session.scalars(
                        select(RagSourceIngestionArtifact).where(
                            RagSourceIngestionArtifact.ingestion_run_id == new_run_id
                        )
                    )
                ).all()
            )
            == 1
        )
        with pytest.raises(IntegrityError):
            async with verify_session.begin_nested():
                await verify_session.execute(
                    delete(RagSourceIngestionRun).where(RagSourceIngestionRun.id == new_run_id)
                )
        assert (
            len(
                (
                    await verify_session.scalars(
                        select(RagSourceIngestionArtifact).where(
                            RagSourceIngestionArtifact.ingestion_run_id == new_run_id
                        )
                    )
                ).all()
            )
            == 1
        )
