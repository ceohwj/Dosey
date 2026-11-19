"""Complete Source attempts are written atomically in isolated synthetic PostgreSQL."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401
from ai_worker.adapters.sqlalchemy_source_snapshot_repository import SqlAlchemySourceSnapshotRepository
from ai_worker.tasks.rag.source_ingestion.artifacts import RawArtifactMetadata, StoredRawArtifact
from ai_worker.tasks.rag.source_ingestion.snapshot_lifecycle import SnapshotRunRecord
from app.core import config
from app.core.db.databases import Base
from app.models.rag_source import RagSourceIngestionArtifact, RagSourceIngestionRun
from app.repositories.rag_source_catalog_repository import (
    RagSourceCatalogRepository,
    RagSourceCreate,
    RagSourceEndpointCreate,
    RagSourceOperationCreate,
)
from app.tests.db_extensions import ensure_vector_extension

pytestmark = pytest.mark.asyncio

SCHEMA = f"rag_complete_{uuid4().hex}"
EXTENSIONS = "test_extensions"
URL_ = URL.create(
    "postgresql+asyncpg",
    username=config.DB_USER,
    password=config.DB_PASSWORD,
    host="127.0.0.1",
    port=config.DB_EXPOSE_PORT,
    database=config.DB_NAME,
)
engine = create_async_engine(
    URL_,
    poolclass=NullPool,
    connect_args={"server_settings": {"search_path": f"{SCHEMA},{EXTENSIONS}"}},
)
factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
NOW = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture(scope="module", autouse=True)
async def isolated_schema() -> AsyncIterator[None]:
    if config.DB_NAME != "test" or config.DB_HOST not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("Source integration tests require the isolated loopback test database.")
    admin = create_async_engine(URL_, poolclass=NullPool)
    async with admin.begin() as connection:
        await connection.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        await connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {EXTENSIONS}"))
        await connection.execute(text(f"CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA {EXTENSIONS}"))
        await connection.execute(text(f"ALTER EXTENSION pg_trgm SET SCHEMA {EXTENSIONS}"))
        await ensure_vector_extension(connection, EXTENSIONS)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        await admin.dispose()


async def _operation(suffix: str) -> UUID:
    async with factory.begin() as session:
        catalog = RagSourceCatalogRepository(session)
        source = await catalog.create_source(
            RagSourceCreate(
                source_code=f"SYNTHETIC_COMPLETE_ATTEMPT_{suffix}",
                display_name="Synthetic Source",
                max_rejected_records=1,
                max_rejection_rate=Decimal("0.5"),
            )
        )
        endpoint = await catalog.create_endpoint(
            RagSourceEndpointCreate(
                source_id=source.id,
                endpoint_code=f"SYNTHETIC_COMPLETE_ENDPOINT_{suffix}",
                display_name="Synthetic Endpoint",
            )
        )
        operation = await catalog.create_operation(
            RagSourceOperationCreate(
                endpoint_id=endpoint.id,
                operation_code=f"SYNTHETIC_COMPLETE_OPERATION_{suffix}",
                display_name="Synthetic Operation",
            )
        )
        return operation.id


def _record(operation_id: UUID, key: str) -> SnapshotRunRecord:
    return SnapshotRunRecord(
        operation_id=operation_id,
        snapshot_id=None,
        run_group_key=key,
        attempt_number=1,
        run_status="FAILED",
        started_at=NOW,
        finished_at=NOW,
        duration_ms=0,
        failure_code="SOURCE_VERSION_CONFLICT",
    )


def _raw(key: str) -> StoredRawArtifact:
    return StoredRawArtifact(
        page_number=1,
        metadata=RawArtifactMetadata(key, "a" * 64, 8, "application/json"),
        storage_backend="LOCAL_PRIVATE",
        object_key=f"synthetic/{key}",
    )


async def _counts(key: str) -> tuple[int, int]:
    async with factory() as session:
        runs = await session.scalar(
            select(func.count()).select_from(RagSourceIngestionRun).where(RagSourceIngestionRun.run_group_key == key)
        )
        artifacts = await session.scalar(
            select(func.count())
            .select_from(RagSourceIngestionArtifact)
            .join(RagSourceIngestionRun)
            .where(RagSourceIngestionRun.run_group_key == key)
        )
        return int(runs or 0), int(artifacts or 0)


async def test_complete_attempt_commits_all_rows_and_zero_artifact_failure() -> None:
    operation_id = await _operation("success")
    async with factory.begin() as session:
        repository = SqlAlchemySourceSnapshotRepository(session)
        await repository.persist_complete_attempt(_record(operation_id, "complete-two"), (_raw("raw-two"),))
        await repository.persist_complete_attempt(_record(operation_id, "complete-zero"), ())
        assert not hasattr(repository, "create_run")
        assert not hasattr(repository, "create_artifacts")
    assert await _counts("complete-two") == (1, 1)
    assert await _counts("complete-zero") == (1, 0)


async def test_database_artifact_failure_rolls_back_savepoint_even_when_caught() -> None:
    operation_id = await _operation("db_failure")
    async with factory.begin() as session:
        repository = SqlAlchemySourceSnapshotRepository(session)
        await repository.persist_complete_attempt(_record(operation_id, "old-terminal"), (_raw("old"),))
    invalid = _raw("invalid")
    object.__setattr__(invalid.metadata, "content_type", "")  # Bypass immutable DTO to exercise PostgreSQL CHECK.
    async with factory.begin() as session:
        repository = SqlAlchemySourceSnapshotRepository(session)
        with pytest.raises(IntegrityError):
            await repository.persist_complete_attempt(_record(operation_id, "db-rejected"), (invalid,))
        # The outer transaction intentionally commits after catching the inner error.
    assert await _counts("db-rejected") == (0, 0)
    assert await _counts("old-terminal") == (1, 1)


async def test_cancellation_and_outer_rollback_leave_no_partial_attempt() -> None:
    operation_id = await _operation("cancel")
    async with factory.begin() as session:
        repository = SqlAlchemySourceSnapshotRepository(session)
        execute = session.execute

        async def cancel_artifact(statement, *args, **kwargs):
            if "INSERT INTO rag_source_ingestion_artifact" in str(statement):
                raise asyncio.CancelledError()
            return await execute(statement, *args, **kwargs)

        with patch.object(session, "execute", new=cancel_artifact), pytest.raises(asyncio.CancelledError):
            await repository.persist_complete_attempt(_record(operation_id, "cancelled"), (_raw("cancel"),))
    assert await _counts("cancelled") == (0, 0)

    async with factory() as session:
        await session.begin()
        await SqlAlchemySourceSnapshotRepository(session).persist_complete_attempt(
            _record(operation_id, "outer-rollback"), (_raw("rollback"),)
        )
        await session.rollback()
    assert await _counts("outer-rollback") == (0, 0)
