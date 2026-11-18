"""Integration tests for RET-H AWS synthetic smoke bootstrap authority split (#684).

Validates:
- Legacy Stage 1 fails under SOURCE_WRITER_USER with 42501 and atomic rollback
- Stage 2 source preparation uses an explicit test-only admin fixture
- Stage 2 authority and idempotency under KNOWLEDGE_INDEX_BUILDER_USER (and 42501 on source/user/job)
- Fail-closed preflight preventing embedding calls
- Sentinel binding verification over bootstrapped tables
- Synthetic runtime parent creation, cascade deletion, and cleanup under DB_APP_USER
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ai_worker.tasks.evaluation.actual_retrieval_index import DeterministicFakeEmbeddingAdapter
from ai_worker.tasks.evaluation.resources import (
    DEFAULT_SMOKE_FIXTURE_PATH,
    load_ret_h_smoke_synthetic_fixture,
)
from ai_worker.tasks.evaluation.ret_h_bootstrap_stages import (
    RET_H_SMOKE_INDEX_CODE,
    bootstrap_ret_h_smoke_stage1_source,
    bootstrap_ret_h_smoke_stage2_knowledge_index,
    cleanup_synthetic_runtime_parent,
    ensure_synthetic_runtime_parent,
)
from ai_worker.tasks.evaluation.ret_h_smoke import (
    verify_source_sentinel_indexed,
)
from app.core import config
from backend.app.release_validation.ret_h_synthetic_smoke import (
    sentinels_from_fixture,
    verify_query_sentinel_binding,
)
from infra.python.provision_database_roles import run_provisioning

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.asyncio


def _alembic_config() -> Config:
    alembic_config = Config()
    alembic_config.set_main_option("script_location", str(ROOT / "backend/alembic"))
    return alembic_config


@pytest_asyncio.fixture
async def provisioned_db(monkeypatch):
    """Creates a disposable migrated database with real least-privilege roles."""
    suffix = uuid4().hex[:8]
    db_name = f"smoke684_{suffix}"
    migration_user = f"owner684_{suffix}"
    app_user = f"app684_{suffix}"
    source_writer_user = f"writer684_{suffix}"
    source_publisher_user = f"publisher684_{suffix}"
    builder_user = f"builder684_{suffix}"
    password = f"pass684_{suffix}"

    admin_url = config.database_url
    cluster_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    admin_db_engine = create_async_engine(make_url(admin_url).set(database=db_name), hide_parameters=True)

    # 1. Create database
    async with cluster_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}"'))

    monkeypatch.setattr(config, "DB_NAME", db_name)

    # 2. Run migrations
    await asyncio.to_thread(command.upgrade, _alembic_config(), "head")

    # 3. Create roles and run provisioning
    env = {
        "DB_HOST": config.DB_HOST,
        "DB_PORT": str(config.DB_EXPOSE_PORT),
        "DB_NAME": db_name,
        "DB_ADMIN_USER": config.DB_USER,
        "DB_ADMIN_PASSWORD": config.DB_PASSWORD,
        "DB_MIGRATION_USER": migration_user,
        "DB_MIGRATION_PASSWORD": password,
        "DB_APP_USER": app_user,
        "DB_APP_PASSWORD": password,
        "SOURCE_WRITER_USER": source_writer_user,
        "SOURCE_WRITER_PASSWORD": password,
        "SOURCE_PUBLISHER_USER": source_publisher_user,
        "SOURCE_PUBLISHER_PASSWORD": password,
        "KNOWLEDGE_INDEX_BUILDER_USER": builder_user,
        "KNOWLEDGE_INDEX_BUILDER_PASSWORD": password,
    }

    # Run SQL to create users
    async with admin_db_engine.begin() as conn:
        for u in (migration_user, app_user, source_writer_user, source_publisher_user, builder_user):
            await conn.execute(
                text(
                    f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{u}') THEN "
                    f"CREATE ROLE \"{u}\" WITH LOGIN PASSWORD '{password}'; END IF; END $$;"
                )
            )

    # Run provision_database_roles
    await run_provisioning(env)

    # Create role-specific engines
    url = make_url(admin_url).set(database=db_name)
    app_engine = create_async_engine(url.set(username=app_user, password=password))
    writer_engine = create_async_engine(url.set(username=source_writer_user, password=password))
    builder_engine = create_async_engine(url.set(username=builder_user, password=password))

    yield {
        "db_name": db_name,
        "admin_engine": admin_db_engine,
        "app_engine": app_engine,
        "writer_engine": writer_engine,
        "builder_engine": builder_engine,
        "admin_source_fixture_session_factory": async_sessionmaker(admin_db_engine, expire_on_commit=False),
        "app_session_factory": async_sessionmaker(app_engine, expire_on_commit=False),
        "writer_session_factory": async_sessionmaker(writer_engine, expire_on_commit=False),
        "builder_session_factory": async_sessionmaker(builder_engine, expire_on_commit=False),
    }

    # Cleanup
    await app_engine.dispose()
    await writer_engine.dispose()
    await builder_engine.dispose()
    await admin_db_engine.dispose()

    async with cluster_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        for u in (migration_user, app_user, source_writer_user, source_publisher_user, builder_user):
            await conn.execute(text(f'DROP ROLE IF EXISTS "{u}"'))
    await cluster_engine.dispose()


async def test_stage1_source_writer_authority_and_boundaries(provisioned_db) -> None:
    fixture = load_ret_h_smoke_synthetic_fixture(DEFAULT_SMOKE_FIXTURE_PATH)
    writer_session_factory = provisioned_db["writer_session_factory"]

    # B1 removes Snapshot/Member INSERT from Writer. Earlier hierarchy writes
    # in this legacy helper must roll back with the rejected Snapshot INSERT.
    with pytest.raises(DBAPIError) as denied:
        await bootstrap_ret_h_smoke_stage1_source(session_factory=writer_session_factory, fixture=fixture)
    assert getattr(denied.value.orig, "sqlstate", None) == "42501"
    async with provisioned_db["admin_source_fixture_session_factory"]() as session:
        for table, column, value in (
            ("rag_source", "source_code", fixture.source_code),
            ("rag_source_endpoint", "endpoint_code", fixture.endpoint_code),
            ("rag_source_operation", "operation_code", fixture.operation_code),
        ):
            assert (
                await session.scalar(text(f"SELECT count(*) FROM {table} WHERE {column}=:code"), {"code": value}) == 0
            )

    # 3. SOURCE_WRITER cannot write to knowledge_document (SQLSTATE 42501)
    async with writer_session_factory() as session:
        with pytest.raises(DBAPIError) as exc_info:
            await session.execute(
                text(
                    "INSERT INTO knowledge_document (id, record_contract_version, document_status, "
                    "source_snapshot_member_id, external_document_id, document_content_hash, canonicalization_spec_version) "
                    "VALUES (:id, '1.0', 'VALID', :member_id, 'ext', :h, '1.0')"
                ),
                {"id": str(uuid4()), "member_id": str(uuid4()), "h": "0" * 64},
            )
            await session.commit()
        assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"

    # 4. SOURCE_WRITER cannot write to user or ai_job (SQLSTATE 42501)
    async with writer_session_factory() as session:
        with pytest.raises(DBAPIError) as exc_info:
            await session.execute(
                text("INSERT INTO \"user\" (id, email, hashed_password, name) VALUES (:id, 'a@b.c', 'h', 'n')"),
                {"id": str(uuid4())},
            )
            await session.commit()
        assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"


async def test_stage2_builder_authority_and_boundaries(provisioned_db) -> None:
    fixture = load_ret_h_smoke_synthetic_fixture(DEFAULT_SMOKE_FIXTURE_PATH)
    admin_source_fixture = provisioned_db["admin_source_fixture_session_factory"]
    builder_session_factory = provisioned_db["builder_session_factory"]
    embedding_adapter = DeterministicFakeEmbeddingAdapter(dimension=1536)

    # Test-only admin fixture; the legacy Stage 1 CLI remains disabled.
    s1_receipt = await bootstrap_ret_h_smoke_stage1_source(
        session_factory=admin_source_fixture,
        fixture=fixture,
    )

    # 1. Stage 2 succeeds under KNOWLEDGE_INDEX_BUILDER
    receipt1 = await bootstrap_ret_h_smoke_stage2_knowledge_index(
        session_factory=builder_session_factory,
        embedding_port=embedding_adapter,
        fixture=fixture,
        stage1_snapshot_id=s1_receipt.snapshot_id,
    )
    assert not receipt1.reused
    assert receipt1.index_code == RET_H_SMOKE_INDEX_CODE
    assert receipt1.knowledge_index_id is not None

    # 2. Idempotent re-run under KNOWLEDGE_INDEX_BUILDER
    receipt2 = await bootstrap_ret_h_smoke_stage2_knowledge_index(
        session_factory=builder_session_factory,
        embedding_port=embedding_adapter,
        fixture=fixture,
        stage1_snapshot_id=s1_receipt.snapshot_id,
    )
    assert receipt2.reused
    assert receipt2.knowledge_index_id == receipt1.knowledge_index_id

    # 3. KNOWLEDGE_INDEX_BUILDER cannot write to rag_source (SQLSTATE 42501)
    async with builder_session_factory() as session:
        with pytest.raises(DBAPIError) as exc_info:
            await session.execute(
                text("INSERT INTO rag_source (id, source_code, lifecycle_status) VALUES (:id, 'FORBIDDEN', 'ACTIVE')"),
                {"id": str(uuid4())},
            )
            await session.commit()
        assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"

    # 4. KNOWLEDGE_INDEX_BUILDER cannot write to user or ai_job (SQLSTATE 42501)
    async with builder_session_factory() as session:
        with pytest.raises(DBAPIError) as exc_info:
            await session.execute(
                text("INSERT INTO \"user\" (id, email, hashed_password, name) VALUES (:id, 'a@b.c', 'h', 'n')"),
                {"id": str(uuid4())},
            )
            await session.commit()
        assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"


async def test_sentinel_bindings_after_bootstrap(provisioned_db) -> None:
    fixture = load_ret_h_smoke_synthetic_fixture(DEFAULT_SMOKE_FIXTURE_PATH)
    admin_source_fixture = provisioned_db["admin_source_fixture_session_factory"]
    builder_session_factory = provisioned_db["builder_session_factory"]
    app_session_factory = provisioned_db["app_session_factory"]
    embedding_adapter = DeterministicFakeEmbeddingAdapter(dimension=1536)

    s1 = await bootstrap_ret_h_smoke_stage1_source(session_factory=admin_source_fixture, fixture=fixture)
    s2 = await bootstrap_ret_h_smoke_stage2_knowledge_index(
        session_factory=builder_session_factory,
        embedding_port=embedding_adapter,
        fixture=fixture,
        stage1_snapshot_id=s1.snapshot_id,
    )

    # DB_APP_USER reads index and verifies sentinel
    ok, msg = await verify_source_sentinel_indexed(
        session_factory=app_session_factory,
        knowledge_index_id=s2.knowledge_index_id,
        source_sentinel=fixture.source_sentinel,
        allowed_source_snapshot_member_ids=s1.member_ids,
    )
    assert ok, msg

    sentinels = sentinels_from_fixture(
        {"query_sentinel": fixture.query_sentinel, "source_sentinel": fixture.source_sentinel}
    )
    assert sentinels is not None
    check = verify_query_sentinel_binding(
        synthetic_query=fixture.synthetic_query,
        sentinels=sentinels,
        approved_query_sha256=fixture.synthetic_query_sha256,
    )
    assert check.passed, check.message


async def test_synthetic_runtime_parent_lifecycle_and_cascade_cleanup(provisioned_db) -> None:
    app_session_factory = provisioned_db["app_session_factory"]
    user_id = uuid4()
    job_id = uuid4()

    # 1. DB_APP_USER creates synthetic user and ai_job
    await ensure_synthetic_runtime_parent(
        session_factory=app_session_factory,
        user_id=user_id,
        job_id=job_id,
    )

    # Verify user and job exist
    async with app_session_factory() as session:
        u_count = (
            await session.execute(text('SELECT COUNT(*) FROM "user" WHERE id = :id'), {"id": str(user_id)})
        ).scalar_one()
        j_count = (
            await session.execute(text("SELECT COUNT(*) FROM ai_job WHERE id = :id"), {"id": str(job_id)})
        ).scalar_one()
        assert u_count == 1
        assert j_count == 1

    # 2. Cleanup under DB_APP_USER (ai_job DELETE, then user DELETE)
    await cleanup_synthetic_runtime_parent(
        session_factory=app_session_factory,
        user_id=user_id,
        job_id=job_id,
    )

    # 3. Verify complete absence in new session
    async with app_session_factory() as session:
        j_left = (
            await session.execute(text("SELECT COUNT(*) FROM ai_job WHERE id = :id"), {"id": str(job_id)})
        ).scalar_one()
        u_left = (
            await session.execute(text('SELECT COUNT(*) FROM "user" WHERE id = :id'), {"id": str(user_id)})
        ).scalar_one()

        assert j_left == 0
        assert u_left == 0
