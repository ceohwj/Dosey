"""Real restricted-role Source publication/cleanup/capture interleavings.

Each test owns a random schema and roles in an explicitly selected loopback test
database. No Docker, actual Source, application database or external store is used.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import Table, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401
from ai_worker.adapters.local_private_source_artifact_store import LocalPrivateSourceArtifactStore
from ai_worker.adapters.local_private_source_cleanup import (
    LocalPrivateArtifactCleanupExecutor,
    LocalPrivateCleanupExecutorJournal,
    LocalPrivateCleanupRequestJournal,
)
from ai_worker.adapters.sqlalchemy_orphan_artifact_references import SqlAlchemyOrphanArtifactReferenceInspector
from ai_worker.adapters.sqlalchemy_source_snapshot_repository import SqlAlchemySourceSnapshotRepository
from ai_worker.admin.source_artifact_cleanup import validate_cleanup_executor_session
from ai_worker.tasks.rag.source_cleanup.orphan_artifact import (
    CleanupRequest,
    CleanupResult,
    CleanupTarget,
    execute_cleanup_request,
)
from ai_worker.tasks.rag.source_client.contracts import SourceOperationIdentity
from ai_worker.tasks.rag.source_ingestion.artifacts import RawArtifactMetadata, StoredRawArtifact
from ai_worker.tasks.rag.source_ingestion.checksums import raw_manifest_checksum
from ai_worker.tasks.rag.source_ingestion.service import SourceAcquisitionInProgressError
from ai_worker.tasks.rag.source_ingestion.snapshot_lifecycle import SnapshotRunRecord
from app.core import config
from app.core.db.databases import Base
from app.models.rag_source import (
    RagSource,
    RagSourceEndpoint,
    RagSourceIngestionArtifact,
    RagSourceOperation,
    RagSourceSnapshot,
    RagSourceSnapshotMember,
)
from app.models.rag_source_capture_observation import RagSourceCaptureObservation as CaptureRow
from app.models.rag_source_use_approval import RagSourceUseApproval
from app.repositories.rag_source_capture_observation_repository import (
    RagSourceCaptureObservationRepository,
    SourceCaptureConflictError,
)
from app.services.source_capture_admission import SyntheticPrivateAcquisition, SyntheticSourceCaptureAdmission
from infra.python.source_capture_role_policy import apply_source_capture_role_policy
from infra.python.source_role_policy import apply_source_role_policy
from rag_runtime.source_capture_observation import SourceCaptureIssuerRef, SourceCaptureObservation

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 11, 4, tzinfo=UTC)
DATA = b"synthetic-publication-page"
ISSUER = SourceCaptureIssuerRef("source_live_capture_issuer", "2.0", "c" * 64)


def _tables() -> list[Table]:
    found: dict[str, Table] = {}
    pending = [CaptureRow.__table__, RagSourceSnapshotMember.__table__, RagSourceUseApproval.__table__]
    while pending:
        table = pending.pop()
        if table.name not in found:
            found[table.name] = table
            pending.extend(key.column.table for key in table.foreign_keys)
    return list(found.values())


@dataclass
class Database:
    engine: AsyncEngine
    rr_engine: AsyncEngine
    schema: str
    publisher: str
    writer: str
    reader: str
    issuer: str
    cleanup: str

    @asynccontextmanager
    async def session(self, role: str | None = None, *, repeatable: bool = False) -> AsyncIterator[AsyncSession]:
        factory = async_sessionmaker(self.rr_engine if repeatable else self.engine, expire_on_commit=False)
        async with factory.begin() as session:
            if role:
                await session.execute(text(f'SET LOCAL ROLE "{role}"'))
            yield session


@pytest_asyncio.fixture
async def database() -> AsyncIterator[Database]:
    if config.DB_HOST not in {"127.0.0.1", "localhost", "::1"} or config.DB_NAME != "test":
        pytest.fail("publication tests require an explicitly selected loopback test database")
    suffix = uuid4().hex[:14]
    schema = f"source_publication_{suffix}"
    roles = [f"srcpub_{name}_{suffix}" for name in ("publisher", "writer", "reader", "issuer", "cleanup")]
    options = dict(poolclass=NullPool, hide_parameters=True, connect_args={"server_settings": {"search_path": schema}})
    engine = create_async_engine(config.database_url, **options)
    rr_engine = create_async_engine(config.database_url, isolation_level="REPEATABLE READ", **options)
    admin = create_async_engine(config.database_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            for role in roles:
                await connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOINHERIT'))
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=_tables()))
            await apply_source_role_policy(
                connection, schema=schema, owner=config.DB_USER, runtime=roles[2], writer=roles[1], publisher=roles[0]
            )
            await apply_source_capture_role_policy(connection, schema=schema, reader=roles[2], issuer=roles[3])
            await connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{roles[4]}"'))
            for table in ("rag_source_ingestion_artifact", "rag_source_ingestion_run", "rag_source_snapshot_member"):
                await connection.execute(text(f'GRANT SELECT ON TABLE "{schema}"."{table}" TO "{roles[4]}"'))
            for role, table, privilege, expected in (
                (roles[0], "rag_source_ingestion_artifact", "INSERT", True),
                (roles[0], "rag_source_ingestion_artifact", "UPDATE", False),
                (roles[0], "rag_source_ingestion_artifact", "DELETE", False),
                (roles[0], "rag_source_capture_observation", "INSERT", False),
                (roles[3], "rag_source_ingestion_run", "INSERT", False),
                (roles[3], "rag_source_capture_observation", "INSERT", True),
                (roles[4], "rag_source_ingestion_artifact", "DELETE", False),
            ):
                assert (
                    await connection.scalar(
                        text("SELECT has_table_privilege(:role, :table, :privilege)"),
                        {"role": role, "table": f"{schema}.{table}", "privilege": privilege},
                    )
                    is expected
                )
        yield Database(engine, rr_engine, schema, *roles)
    finally:
        await rr_engine.dispose()
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            for role in roles:
                # All grants were confined to the now-dropped, owned schema.
                await connection.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        await admin.dispose()


async def _seed(database: Database, metadata: RawArtifactMetadata | None = None):
    async with database.session() as session:
        source = RagSource(source_code="SYN_" + uuid4().hex, display_name="synthetic")
        session.add(source)
        await session.flush()
        endpoint = RagSourceEndpoint(source_id=source.id, endpoint_code="EP", display_name="synthetic")
        session.add(endpoint)
        await session.flush()
        operation = RagSourceOperation(endpoint_id=endpoint.id, operation_code="LIST", display_name="synthetic")
        session.add(operation)
        await session.flush()
        snapshot = None
        if metadata:
            snapshot = RagSourceSnapshot(
                operation_id=operation.id,
                source_version="v-" + uuid4().hex,
                raw_manifest_checksum=raw_manifest_checksum([metadata]),
                canonical_checksum="b" * 64,
                schema_version="v1",
                parser_version="v1",
                normalization_version="v1",
                canonicalization_spec_version="v1",
                endpoint_receipt_hash="e" * 64,
                record_count=1,
                rejected_record_count=0,
                verification_status="PENDING",
                collected_at=NOW,
            )
            session.add(snapshot)
            await session.flush()
        return operation.id, SourceOperationIdentity(source.source_code, "EP", "LIST"), snapshot


def _record(operation_id: UUID, *, snapshot_id: UUID | None = None) -> SnapshotRunRecord:
    return SnapshotRunRecord(
        operation_id=operation_id,
        snapshot_id=snapshot_id,
        run_group_key=uuid4().hex,
        attempt_number=1,
        run_status="SUCCEEDED" if snapshot_id else "FAILED",
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        duration_ms=1000,
        failure_code=None if snapshot_id else "SOURCE_VERSION_CONFLICT",
    )


async def _waiting(database: Database, pid: int) -> None:
    """Synchronize on an actual advisory wait, not elapsed wall-clock time."""
    async with database.session() as observer:
        async with asyncio.timeout(10):
            while not await observer.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=:pid AND locktype='advisory' AND NOT granted)"),
                {"pid": pid},
            ):
                await asyncio.sleep(0)


async def _reference_count(database: Database, run_id: UUID) -> int:
    async with database.session(database.reader) as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(RagSourceIngestionArtifact)
                .where(RagSourceIngestionArtifact.ingestion_run_id == run_id)
            )
            or 0
        )


@pytest.mark.parametrize("role_name", ["writer", "reader", "issuer", "cleanup"])
@pytest.mark.parametrize(
    "table",
    ["rag_source_ingestion_run", "rag_source_ingestion_artifact", "rag_source_snapshot", "rag_source_snapshot_member"],
)
async def test_non_publisher_roles_cannot_insert_source_aggregate(
    database: Database, role_name: str, table: str
) -> None:
    role = getattr(database, role_name)
    with pytest.raises(DBAPIError) as error:
        async with database.session(role) as session:
            assert await session.scalar(text("SELECT current_user")) == role
            await session.execute(text(f"INSERT INTO {table} DEFAULT VALUES"))
    assert error.value.orig.sqlstate == "42501"


async def test_acquisition_first_and_global_first_writers_preserve_fail_fast_without_a_cycle(database):
    operation_id, identity, _ = await _seed(database)
    raw = StoredRawArtifact(1, RawArtifactMetadata("p1", "a" * 64, 4, "application/json"), "synthetic", "p1")
    async with database.session(database.publisher) as generic:
        generic_repo = SqlAlchemySourceSnapshotRepository(generic)
        assert await generic.scalar(text("SELECT current_user")) == database.publisher
        await generic_repo.try_lock_acquisition(identity)
        async with database.session(database.publisher) as product:
            product_repo = SqlAlchemySourceSnapshotRepository(product)
            await product_repo.lock_artifact_publication()
            pid = await generic.scalar(text("SELECT pg_backend_pid()"))
            waiting = asyncio.create_task(generic_repo.lock_artifact_publication())
            try:
                await _waiting(database, pid)
                with pytest.raises(SourceAcquisitionInProgressError):
                    await asyncio.wait_for(product_repo.try_lock_acquisition(identity), timeout=10)
            finally:
                # Product ends, releasing global while generic still owns Source acquisition.
                await product.rollback()
                await asyncio.wait_for(waiting, timeout=10)
        await generic_repo.lock_operation(identity)
        run_id = await generic_repo.persist_complete_attempt(_record(operation_id), (raw,))
    assert await _reference_count(database, run_id) == 1
    async with database.session(database.publisher) as later:
        assert await SqlAlchemySourceSnapshotRepository(later).try_lock_acquisition(identity) == operation_id


def _physical_artifact(tmp_path: Path):
    incoming = tmp_path / "incoming"
    incoming.write_bytes(DATA)
    incoming.chmod(0o600)
    root = tmp_path / "objects"
    store = LocalPrivateSourceArtifactStore(root)
    metadata = RawArtifactMetadata("p1", hashlib.sha256(DATA).hexdigest(), len(DATA), "application/json")
    return incoming, root, store, metadata


def _request(stored: StoredRawArtifact) -> CleanupRequest:
    return CleanupRequest(
        uuid4(),
        uuid4().hex,
        None,
        (CleanupTarget(stored.metadata.artifact_key, stored.object_key, stored.metadata.raw_checksum),),
        "MFDS_LABEL_TRANSACTION_FAILED",
        NOW,
    )


async def _cleanup(session: AsyncSession, request: CleanupRequest, root: Path, journal: Path):
    journal.mkdir(mode=0o700)
    (journal / "requests").mkdir(mode=0o700)
    (journal / "receipts").mkdir(mode=0o700)
    LocalPrivateCleanupRequestJournal(journal).append_request(request)
    receipts = LocalPrivateCleanupExecutorJournal(journal)
    return await execute_cleanup_request(
        request=receipts.read_request(request.request_id),
        executor="synthetic-cleanup",
        executed_at=NOW,
        references=SqlAlchemyOrphanArtifactReferenceInspector(session),
        artifacts=LocalPrivateArtifactCleanupExecutor(root),
        receipts=receipts,
    )


async def test_writer_commit_makes_waiting_cleanup_protect_the_physical_object(database, tmp_path):
    operation_id, _, _ = await _seed(database)
    incoming, root, store, metadata = _physical_artifact(tmp_path)
    cleanup_pid: asyncio.Future[int] = asyncio.Future()

    async def cleanup(request):
        async with database.session(database.cleanup) as session:
            await validate_cleanup_executor_session(session)
            cleanup_pid.set_result(await session.scalar(text("SELECT pg_backend_pid()")))
            await SqlAlchemySourceSnapshotRepository(session).lock_artifact_publication()
            return await _cleanup(session, request, root, tmp_path / "journal")

    async with database.session(database.publisher) as writer:
        repository = SqlAlchemySourceSnapshotRepository(writer)
        await repository.lock_artifact_publication()
        stored = store.put_verified(page_number=1, file_path=incoming, metadata=metadata)
        run_id = await repository.persist_complete_attempt(_record(operation_id), (stored,))
        task = asyncio.create_task(cleanup(_request(stored)))
        await _waiting(database, await asyncio.wait_for(cleanup_pid, timeout=10))
    receipt = await asyncio.wait_for(task, timeout=10)
    assert receipt.items[0].result is CleanupResult.BLOCKED
    assert receipt.items[0].references.artifact_receipts == 1
    assert store.read_verified(object_key=stored.object_key, metadata=metadata) == DATA
    assert await _reference_count(database, run_id) == 1


async def test_cleanup_first_requires_writer_to_recreate_verified_bytes_before_reference_commit(database, tmp_path):
    operation_id, _, _ = await _seed(database)
    incoming, root, store, metadata = _physical_artifact(tmp_path)
    orphan = store.put_verified(page_number=1, file_path=incoming, metadata=metadata)
    writer_pid: asyncio.Future[int] = asyncio.Future()

    async def publish():
        async with database.session(database.publisher) as session:
            repository = SqlAlchemySourceSnapshotRepository(session)
            writer_pid.set_result(await session.scalar(text("SELECT pg_backend_pid()")))
            await repository.lock_artifact_publication()
            # The same real content-addressed put must recreate/verify the deleted orphan.
            stored = store.put_verified(page_number=1, file_path=incoming, metadata=metadata)
            assert store.read_verified(object_key=stored.object_key, metadata=metadata) == DATA
            return await repository.persist_complete_attempt(_record(operation_id), (stored,))

    async with database.session(database.cleanup) as cleanup:
        await validate_cleanup_executor_session(cleanup)
        await SqlAlchemySourceSnapshotRepository(cleanup).lock_artifact_publication()
        task = asyncio.create_task(publish())
        await _waiting(database, await asyncio.wait_for(writer_pid, timeout=10))
        receipt = await _cleanup(cleanup, _request(orphan), root, tmp_path / "journal")
        assert receipt.items[0].result is CleanupResult.DELETED
        assert not (root / orphan.object_key).exists()
    run_id = await asyncio.wait_for(task, timeout=10)
    assert await _reference_count(database, run_id) == 1
    assert store.read_verified(object_key=orphan.object_key, metadata=metadata) == DATA


async def test_rr_issuer_cannot_publish_a_partial_attempt_and_needs_a_fresh_snapshot_after_commit(database):
    metadata = RawArtifactMetadata("p1", "a" * 64, 4, "application/json")
    operation_id, identity, snapshot = await _seed(database, metadata)
    assert snapshot is not None
    raw = StoredRawArtifact(1, metadata, "synthetic", "p1")
    between = asyncio.Event()
    finish = asyncio.Event()
    new_run: dict[str, UUID] = {}

    async with database.session(database.publisher) as writer:
        execute = writer.execute

        async def paused(statement, *args, **kwargs):
            sql = str(statement)
            if sql.startswith("INSERT INTO rag_source_ingestion_run "):
                new_run["id"] = UUID(statement.compile().params["id"])
            if sql.startswith("INSERT INTO rag_source_ingestion_artifact "):
                between.set()
                await asyncio.wait_for(finish.wait(), timeout=10)
            return await execute(statement, *args, **kwargs)

        with patch.object(writer, "execute", new=paused):
            task = asyncio.create_task(
                SqlAlchemySourceSnapshotRepository(writer).persist_complete_attempt(
                    _record(operation_id, snapshot_id=snapshot.id), (raw,)
                )
            )
            await asyncio.wait_for(between.wait(), timeout=10)
            observation = SourceCaptureObservation(
                capture_observation_id=uuid4(),
                ingestion_run_id=new_run["id"],
                source_snapshot_id=snapshot.id,
                source_version=snapshot.source_version,
                canonical_checksum=snapshot.canonical_checksum,
                source_code=identity.source_code,
                endpoint_code=identity.endpoint_code,
                operation_code=identity.operation_code,
                capture_protocol_version="v2",
                issuer_ref=ISSUER,
                request_profile_sha256="d" * 64,
                capture_origin="LIVE_PROVIDER",
                capture_started_at=NOW,
                capture_completed_at=NOW + timedelta(seconds=1),
                recorded_at=NOW + timedelta(seconds=2),
                raw_manifest_sha256=raw_manifest_checksum([metadata]),
                endpoint_contract_receipt_sha256="e" * 64,
                full_scan_completed=True,
                primary_key_validation_passed=True,
                page_count=1,
                record_count=1,
            )
            acquisition = SyntheticPrivateAcquisition(
                new_run_id=new_run["id"],
                capture_observation_id=observation.capture_observation_id,
                source_snapshot_id=snapshot.id,
                source_code=identity.source_code,
                endpoint_code=identity.endpoint_code,
                operation_code=identity.operation_code,
                issuer_ref=ISSUER,
                request_profile_sha256="d" * 64,
                capture_started_at=NOW,
                capture_completed_at=observation.capture_completed_at,
                recorded_at=observation.recorded_at,
                origin="SIMULATED_LIVE",
            )
            async with database.session(database.issuer, repeatable=True) as old_reader:
                admission = SyntheticSourceCaptureAdmission(
                    RagSourceCaptureObservationRepository(old_reader), issuer_ref=ISSUER
                )
                with pytest.raises(SourceCaptureConflictError, match="run and snapshot binding unavailable"):
                    await admission.record(observation, acquisition)
                assert await old_reader.scalar(select(func.count()).select_from(CaptureRow)) == 0
                finish.set()
                assert await asyncio.wait_for(task, timeout=10) == new_run["id"]
                await writer.commit()
                # Waiting for the writer does not refresh an existing RR snapshot.
                with pytest.raises(SourceCaptureConflictError, match="run and snapshot binding unavailable"):
                    await admission.record(observation, acquisition)
    async with database.session(database.issuer, repeatable=True) as fresh_reader:
        repository = RagSourceCaptureObservationRepository(fresh_reader)
        digest = await SyntheticSourceCaptureAdmission(repository, issuer_ref=ISSUER).record(observation, acquisition)
        assert await repository.get_exact(observation.capture_observation_id, digest) == observation
