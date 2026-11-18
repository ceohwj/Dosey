"""PostgreSQL checks for the additive Candidate Search Runtime binding migration.

Run only against an isolated test database account with CREATE SCHEMA privilege.
Each test owns one random schema and drops only that exact schema.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from app.core import config

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "backend/alembic/versions/924a1b2c3d4e_candidate_search_runtime_binding.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("candidate_runtime_binding_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_single_forward_head() -> None:
    alembic_config = Config(str(ROOT / "backend/alembic.ini"))
    alembic_config.set_main_option("script_location", str(ROOT / "backend/alembic"))
    assert ScriptDirectory.from_config(alembic_config).get_revision("924a1b2c3d4e") is not None
    assert _module().down_revision == "923a1b2c3d4e"


@pytest_asyncio.fixture
async def isolated_schema():
    if "test" not in config.DB_NAME.lower():
        pytest.fail("Candidate migration test requires a dedicated test database")
    schema = "candidate_binding_" + uuid4().hex
    engine = create_async_engine(config.database_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            await connection.execute(
                text(
                    "CREATE TABLE rag_runtime_release_bundle "
                    "(id CHAR(36) PRIMARY KEY, bundle_manifest_hash VARCHAR(64) NOT NULL, "
                    "CONSTRAINT uq_bundle_id_hash UNIQUE (id, bundle_manifest_hash))"
                )
            )
            await connection.execute(text("CREATE TABLE rag_runtime_environment (id CHAR(36) PRIMARY KEY)"))
            await connection.execute(
                text(
                    "CREATE TABLE medication_candidate_search "
                    "(id CHAR(36) PRIMARY KEY, runtime_release_bundle_id CHAR(36), "
                    "candidate_index_version_id CHAR(36))"
                )
            )
        yield engine, schema
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def _migration(connection: AsyncConnection, schema: str, function: str) -> None:
    module = _module()
    await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))

    def execute(sync_connection):
        module.op = Operations(MigrationContext.configure(sync_connection))
        getattr(module, function)()

    await connection.run_sync(execute)


async def _insert_search(connection: AsyncConnection, **values: object) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join(f":{key}" for key in values)
    await connection.execute(
        text(f"INSERT INTO medication_candidate_search ({columns}) VALUES ({placeholders})"), values
    )


async def _must_reject(connection: AsyncConnection, **values: object) -> None:
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await _insert_search(connection, **values)


@pytest.mark.asyncio
async def test_legacy_rows_constraints_and_guarded_downgrade(isolated_schema) -> None:
    engine, schema = isolated_schema
    bundle_id, environment_id, index_id = (str(uuid4()) for _ in range(3))
    manifest_hash = "a" * 64
    async with engine.begin() as connection:
        await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        await connection.execute(
            text("INSERT INTO rag_runtime_release_bundle (id, bundle_manifest_hash) VALUES (:id, :hash)"),
            {"id": bundle_id, "hash": manifest_hash},
        )
        await connection.execute(text("INSERT INTO rag_runtime_environment (id) VALUES (:id)"), {"id": environment_id})
        legacy_null_id, legacy_bundle_id = str(uuid4()), str(uuid4())
        await _insert_search(connection, id=legacy_null_id)
        await _insert_search(connection, id=legacy_bundle_id, runtime_release_bundle_id=str(uuid4()))
        await _migration(connection, schema, "upgrade")
        rows = (
            await connection.execute(
                text(
                    "SELECT id, runtime_release_bundle_manifest_hash, runtime_environment_id, "
                    "runtime_environment_revision FROM medication_candidate_search ORDER BY id"
                )
            )
        ).all()
        assert len(rows) == 2
        assert all(row[1:] == (None, None, None) for row in rows)

        valid = dict(
            id=str(uuid4()),
            runtime_release_bundle_id=bundle_id,
            runtime_release_bundle_manifest_hash=manifest_hash,
            runtime_environment_id=environment_id,
            runtime_environment_revision=1,
            candidate_index_version_id=index_id,
        )
        for patch in (
            {"runtime_environment_revision": None},
            {"runtime_release_bundle_manifest_hash": "A" * 64},
            {"runtime_environment_revision": 0},
            {"runtime_release_bundle_id": str(uuid4())},
            {"runtime_release_bundle_manifest_hash": "b" * 64},
            {"runtime_environment_id": str(uuid4())},
            {"candidate_index_version_id": None},
        ):
            await _must_reject(connection, **(valid | patch | {"id": str(uuid4())}))
        await _insert_search(connection, **valid)
        with pytest.raises(RuntimeError, match="lose authoritative Candidate Runtime bindings"):
            await _migration(connection, schema, "downgrade")
        assert (
            await connection.scalar(
                text("SELECT runtime_environment_revision FROM medication_candidate_search WHERE id = :id"),
                {"id": valid["id"]},
            )
        ) == 1
