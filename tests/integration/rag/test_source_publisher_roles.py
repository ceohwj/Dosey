"""Synthetic login ACL and independently committed Writer denial barrier."""

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import config
from infra.python.source_role_policy import (
    SOURCE_PUBLISHER_LOCK_COLUMNS,
    SOURCE_TABLES,
    WRITER_PUBLICATION_TABLES,
    apply_source_role_policy,
    revoke_source_writer_publication,
    verify_source_role_policy,
)


@pytest_asyncio.fixture
async def publisher_roles():
    database = create_async_engine(
        URL.create(
            "postgresql+asyncpg",
            username=config.DB_USER,
            password=config.DB_PASSWORD,
            host=config.DB_HOST,
            port=config.DB_EXPOSE_PORT,
            database=config.DB_NAME,
        )
    )
    suffix = uuid4().hex[:12]
    schema, runtime, writer, publisher = (
        f"pubb1_{part}_{suffix}" for part in ("schema", "runtime", "writer", "publisher")
    )
    password = f"synthetic-{suffix}-only"
    engines = {
        name: create_async_engine(database.url.set(username=role, password=password))
        for name, role in (("runtime", runtime), ("writer", writer), ("publisher", publisher))
    }
    try:
        async with database.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            for role in (runtime, writer, publisher):
                await connection.execute(text(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}'"))
            for table in SOURCE_TABLES + ("rag_source_use_approval", "catalog_business", "knowledge_business"):
                await connection.execute(text(f'CREATE TABLE "{schema}"."{table}" (id integer PRIMARY KEY)'))
            for table, column in SOURCE_PUBLISHER_LOCK_COLUMNS.items():
                await connection.execute(
                    text(
                        f'ALTER TABLE "{schema}"."{table}" ADD COLUMN "{column}" integer NOT NULL DEFAULT 0 CHECK ("{column}"=0)'
                    )
                )
            await connection.execute(
                text(
                    f'ALTER TABLE "{schema}".rag_source_snapshot ADD COLUMN verification_status text, '
                    "ADD COLUMN verified_at timestamptz, ADD COLUMN effective_at timestamptz, ADD COLUMN verification_seal_id text"
                )
            )
            await connection.execute(
                text(
                    f'ALTER TABLE "{schema}".rag_source_use_approval ADD COLUMN revoked_at timestamptz, '
                    "ADD COLUMN revoked_by integer, ADD COLUMN revoked_reason text"
                )
            )
            await connection.execute(
                text(
                    f'ALTER TABLE "{schema}".rag_source_ingestion_run ADD COLUMN snapshot_id integer, '
                    "ADD COLUMN run_status text, ADD COLUMN failure_code text, ADD COLUMN failure_message text, "
                    "ADD COLUMN duration_ms integer, ADD COLUMN finished_at timestamptz"
                )
            )
            for role in (runtime, writer, publisher):
                await connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"'))
        yield database, schema, runtime, writer, publisher, engines
    finally:
        for engine in engines.values():
            await engine.dispose()
        async with database.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            for role in (publisher, writer, runtime):
                await connection.execute(text(f'DROP OWNED BY "{role}"'))
                await connection.execute(text(f'DROP ROLE "{role}"'))
        await database.dispose()


async def _denied(engine, statement, sqlstate="42501"):
    with pytest.raises(DBAPIError) as error:
        async with engine.begin() as connection:
            await connection.execute(text(statement))
    assert error.value.orig.sqlstate == sqlstate


async def test_publisher_complete_graph_stale_grants_and_repeat_policy(publisher_roles):
    admin, schema, runtime, writer, publisher, engines = publisher_roles
    policy = dict(schema=schema, owner=config.DB_USER, runtime=runtime, writer=writer, publisher=publisher)
    async with admin.begin() as connection:
        for role in (writer, publisher):
            await connection.execute(text(f'GRANT ALL ON ALL TABLES IN SCHEMA "{schema}" TO "{role}"'))
            await connection.execute(
                text(f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" GRANT ALL ON TABLES TO "{role}"')
            )
        await connection.execute(text(f'GRANT ALL ON ALL TABLES IN SCHEMA "{schema}" TO PUBLIC'))
        for table in SOURCE_TABLES + ("catalog_business", "knowledge_business"):
            await connection.execute(
                text(f'GRANT INSERT (id), UPDATE (id) ON "{schema}"."{table}" TO PUBLIC, "{publisher}", "{writer}"')
            )
        for _ in range(2):
            await apply_source_role_policy(connection, **policy)
        await connection.execute(text(f'CREATE TABLE "{schema}".future_table (id integer)'))
    async with engines["publisher"].begin() as connection:
        for table in SOURCE_TABLES:
            await connection.execute(text(f'INSERT INTO "{schema}"."{table}" (id) VALUES (1)'))
        for table, column in SOURCE_PUBLISHER_LOCK_COLUMNS.items():
            await connection.execute(text(f'UPDATE "{schema}"."{table}" SET "{column}"=0 WHERE id=1'))
            await connection.execute(text(f'SELECT id FROM "{schema}"."{table}" FOR UPDATE'))
    for table in WRITER_PUBLICATION_TABLES:
        await _denied(engines["writer"], f'INSERT INTO "{schema}"."{table}" (id) VALUES (2)')
    await _assert_publisher_denials(engines["publisher"], schema)


async def _assert_publisher_denials(engine, schema):
    for column in ("snapshot_id", "run_status", "failure_code", "failure_message", "duration_ms", "finished_at"):
        for assignment in (f"{column}={column}", f"knowledge_index_lock_marker=0, {column}={column}"):
            await _denied(engine, f'UPDATE "{schema}".rag_source_ingestion_run SET {assignment}')
    for table in SOURCE_TABLES:
        for statement in (
            f'UPDATE "{schema}"."{table}" SET id=2',
            f'DELETE FROM "{schema}"."{table}"',
            f'TRUNCATE "{schema}"."{table}"',
        ):
            await _denied(engine, statement)
    for table, column in SOURCE_PUBLISHER_LOCK_COLUMNS.items():
        await _denied(engine, f'UPDATE "{schema}"."{table}" SET "{column}"=1', "23514")
    for table in ("rag_source_use_approval", "catalog_business", "knowledge_business", "future_table"):
        for statement in (
            f'INSERT INTO "{schema}"."{table}" (id) VALUES (1)',
            f'UPDATE "{schema}"."{table}" SET id=2',
            f'DELETE FROM "{schema}"."{table}"',
            f'TRUNCATE "{schema}"."{table}"',
        ):
            await _denied(engine, statement)


async def test_publisher_rejects_membership_admin_owner_and_global_defaults(publisher_roles):
    admin, schema, runtime, writer, publisher, _ = publisher_roles
    policy = dict(schema=schema, owner=config.DB_USER, runtime=runtime, writer=writer, publisher=publisher)
    async with admin.begin() as connection:
        for statement, message in (
            (f'GRANT "{writer}" TO "{publisher}"', "inherit or SET ROLE"),
            (f'ALTER ROLE "{publisher}" REPLICATION', "administrative privileges"),
            (f'ALTER TABLE "{schema}".rag_source OWNER TO "{publisher}"', "own schema objects"),
            (f'ALTER DEFAULT PRIVILEGES GRANT INSERT ON TABLES TO "{publisher}"', "global table default grants"),
        ):
            savepoint = await connection.begin_nested()
            try:
                await connection.execute(text(statement))
                with pytest.raises(ValueError, match=message):
                    await apply_source_role_policy(connection, **policy)
            finally:
                await savepoint.rollback()
        with pytest.raises(ValueError, match="must differ"):
            await apply_source_role_policy(connection, **{**policy, "publisher": writer})


async def test_t1_commit_survives_t2_rollback_and_pre_t1_failure_preserves_old_state(publisher_roles):
    admin, schema, runtime, writer, publisher, engines = publisher_roles
    boundary = dict(schema=schema, owner=config.DB_USER, runtime=runtime, writer=writer)
    async with admin.begin() as connection:
        for table in WRITER_PUBLICATION_TABLES:
            await connection.execute(text(f'GRANT INSERT ON "{schema}"."{table}" TO "{writer}", PUBLIC'))
            await connection.execute(text(f'GRANT INSERT (id) ON "{schema}"."{table}" TO "{writer}", PUBLIC'))
        await connection.execute(text(f'ALTER DEFAULT PRIVILEGES GRANT INSERT ON TABLES TO "{writer}"'))
        await connection.execute(text(f'GRANT "{runtime}" TO "{writer}"'))
    with pytest.raises(ValueError, match="inherit or SET ROLE"):
        async with admin.begin() as connection:
            await revoke_source_writer_publication(connection, **boundary)
    # T1 never committed: B denial is not established.
    async with engines["writer"].begin() as connection:
        await connection.execute(text(f'INSERT INTO "{schema}".rag_source_ingestion_run (id) VALUES (1)'))
    async with admin.begin() as connection:
        await connection.execute(text(f'REVOKE "{runtime}" FROM "{writer}"'))
        await revoke_source_writer_publication(connection, **boundary)
    with pytest.raises(RuntimeError, match="synthetic T2"):
        async with admin.begin() as connection:
            await apply_source_role_policy(connection, **boundary, publisher=publisher)
            raise RuntimeError("synthetic T2 failure")
    for table in WRITER_PUBLICATION_TABLES:
        await _denied(engines["writer"], f'INSERT INTO "{schema}"."{table}" (id) VALUES (2)')
    await _denied(engines["publisher"], f'INSERT INTO "{schema}".rag_source_ingestion_run (id) VALUES (2)')


async def test_full_provisioning_requires_publisher_before_connecting(monkeypatch):
    from infra.python import provision_database_roles as provisioning

    environment = {
        "DB_HOST": "synthetic.invalid",
        "DB_PORT": "5432",
        "DB_NAME": "synthetic",
        "DB_ADMIN_USER": "admin",
        "DB_ADMIN_PASSWORD": "synthetic-only",
        "DB_MIGRATION_USER": "owner",
        "DB_APP_USER": "runtime",
        "SOURCE_WRITER_USER": "writer",
    }

    def unexpected_connection(*args, **kwargs):
        pytest.fail("Missing Publisher must fail before a connection is attempted")

    monkeypatch.setattr(provisioning, "create_async_engine", unexpected_connection)
    with pytest.raises(ValueError, match="Missing database provisioning"):
        await provisioning.run_provisioning(environment)
    environment["SOURCE_PUBLISHER_USER"] = "writer"
    with pytest.raises(ValueError, match="must be distinct"):
        await provisioning.run_provisioning(environment)


@pytest.mark.parametrize("deny_only", (True, False))
@pytest.mark.parametrize("readback_fails", (False, True))
async def test_postcommit_readback_uses_fresh_connection_after_t1_or_t2(monkeypatch, deny_only, readback_fails):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from infra.python import provision_database_roles as provisioning

    environment = {
        "DB_HOST": "synthetic.invalid",
        "DB_PORT": "5432",
        "DB_NAME": "synthetic",
        "DB_ADMIN_USER": "admin",
        "DB_ADMIN_PASSWORD": "synthetic-only",
        "DB_MIGRATION_USER": "owner",
        "DB_APP_USER": "runtime",
        "SOURCE_WRITER_USER": "writer",
    }
    if not deny_only:
        environment["SOURCE_PUBLISHER_USER"] = "publisher"
    transaction = []
    connection = object()
    fresh_connection = AsyncMock()

    class Engine:
        @asynccontextmanager
        async def begin(self):
            transaction.append("begin")
            yield connection
            transaction.append("commit")

        @asynccontextmanager
        async def connect(self):
            assert transaction == ["begin", "commit"]
            transaction.append("fresh connect")
            yield fresh_connection

        async def dispose(self):
            transaction.append("dispose")

    deny = AsyncMock()
    full = AsyncMock()
    readback = AsyncMock(side_effect=ValueError("synthetic readback failure") if readback_fails else None)

    def create_engine(*args, **kwargs):
        assert kwargs["poolclass"] is provisioning.NullPool
        return Engine()

    monkeypatch.setattr(provisioning, "create_async_engine", create_engine)
    monkeypatch.setattr(provisioning, "revoke_source_writer_publication", deny)
    monkeypatch.setattr(provisioning, "provision_roles", full)
    monkeypatch.setattr(provisioning, "verify_source_role_policy", readback)
    if readback_fails:
        with pytest.raises(ValueError, match="synthetic readback failure"):
            await provisioning.run_provisioning(environment, deny_only=deny_only)
    else:
        await provisioning.run_provisioning(environment, deny_only=deny_only)
    if deny_only:
        deny.assert_awaited_once_with(connection, schema="public", owner="owner", runtime="runtime", writer="writer")
        full.assert_not_awaited()
    else:
        full.assert_awaited_once()
        assert full.await_args.kwargs["publisher"] == "publisher"
        deny.assert_not_awaited()
    readback.assert_awaited_once_with(
        fresh_connection,
        schema="public",
        owner="owner",
        runtime="runtime",
        writer="writer",
        publisher=None if deny_only else "publisher",
        deny_only=deny_only,
    )
    fresh_connection.execute.assert_awaited_once()
    assert str(fresh_connection.execute.await_args.args[0]) == "SET TRANSACTION READ ONLY"
    assert transaction == ["begin", "commit", "fresh connect", "dispose"]


@pytest.mark.parametrize(("table", "marker"), SOURCE_PUBLISHER_LOCK_COLUMNS.items())
async def test_publisher_rejects_unconstrained_marker_with_coordinate(publisher_roles, table, marker):
    admin, schema, runtime, writer, publisher, _ = publisher_roles
    async with admin.begin() as connection:
        await connection.execute(text(f'ALTER TABLE "{schema}"."{table}" ALTER COLUMN "{marker}" DROP NOT NULL'))
        with pytest.raises(ValueError, match=rf"{table}\.{marker}.*validated CHECK equal to zero"):
            await apply_source_role_policy(
                connection, schema=schema, owner=config.DB_USER, runtime=runtime, writer=writer, publisher=publisher
            )


async def test_postcommit_readback_failure_preserves_committed_writer_denial(publisher_roles):
    admin, schema, runtime, writer, publisher, engines = publisher_roles
    boundary = dict(schema=schema, owner=config.DB_USER, runtime=runtime, writer=writer)
    async with admin.begin() as connection:
        await revoke_source_writer_publication(connection, **boundary)
    async with admin.begin() as connection:
        await apply_source_role_policy(connection, **boundary, publisher=publisher)
    async with admin.connect() as connection:
        await verify_source_role_policy(connection, **boundary, publisher=publisher)
    # A post-commit drift must stop restart without undoing the T1 barrier.
    async with admin.begin() as connection:
        await connection.execute(
            text(f'GRANT UPDATE (run_status) ON "{schema}".rag_source_ingestion_run TO "{publisher}"')
        )
    async with admin.connect() as connection:
        with pytest.raises(ValueError, match="Publisher.*column"):
            await verify_source_role_policy(connection, **boundary, publisher=publisher)
    for table in WRITER_PUBLICATION_TABLES:
        await _denied(engines["writer"], f'INSERT INTO "{schema}"."{table}" (id) VALUES (2)')


@pytest.mark.parametrize(
    "drift",
    ("table", "column", "sequence", "schema_create", "membership", "default", "missing_insert", "foreign_schema"),
)
async def test_readback_rejects_effective_publisher_acl_drift(publisher_roles, drift):
    admin, schema, runtime, writer, publisher, _ = publisher_roles
    boundary = dict(schema=schema, owner=config.DB_USER, runtime=runtime, writer=writer, publisher=publisher)
    async with admin.begin() as connection:
        await apply_source_role_policy(connection, **boundary)
    statements = {
        "table": f'GRANT INSERT ON "{schema}".catalog_business TO "{publisher}"',
        "column": f'GRANT UPDATE (id) ON "{schema}".rag_source TO "{publisher}"',
        "sequence": f'CREATE SEQUENCE "{schema}".stale_sequence; GRANT USAGE ON "{schema}".stale_sequence TO "{publisher}"',
        "schema_create": f'GRANT CREATE ON SCHEMA "{schema}" TO "{publisher}"',
        "membership": f'GRANT "{writer}" TO "{publisher}"',
        "default": f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" GRANT INSERT ON TABLES TO "{publisher}"',
        "missing_insert": f'REVOKE INSERT ON "{schema}".rag_source FROM "{publisher}"',
        "foreign_schema": f'CREATE SCHEMA "{schema}_other"; CREATE TABLE "{schema}_other".rag_source (id integer); GRANT INSERT ON "{schema}_other".rag_source TO "{publisher}"',
    }
    # Roll back corrupt grants and extra objects after the read-only verifier rejects them.
    async with admin.connect() as connection:
        transaction = await connection.begin()
        try:
            for statement in statements[drift].split("; "):
                await connection.execute(text(statement))
            with pytest.raises(ValueError):
                await verify_source_role_policy(connection, **boundary)
        finally:
            await transaction.rollback()
