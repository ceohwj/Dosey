"""Additive audit DDL in an owned, isolated synthetic PostgreSQL schema."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core import config
from app.models.rag_candidate_guard_audit import RagCandidateRequestGuardAudit
from app.models.rag_source_capture_observation import RagSourceCaptureObservation

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "apps/backend/alembic/versions/925a1b2c3d4e_candidate_guard_audit_capture.py"


def _module():
    spec = importlib.util.spec_from_file_location("candidate_guard_audit_migration", PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_single_forward_head() -> None:
    settings = Config(str(ROOT / "apps/backend/alembic.ini"))
    settings.set_main_option("script_location", str(ROOT / "apps/backend/alembic"))
    script = ScriptDirectory.from_config(settings)
    assert len(script.get_heads()) == 1
    assert script.get_revision("925a1b2c3d4e") is not None
    assert _module().down_revision == "924a1b2c3d4e"


async def _apply(connection, operation: str) -> None:
    def apply(sync):
        module = _module()
        module.op = Operations(MigrationContext.configure(sync))
        getattr(module, operation)()

    await connection.run_sync(apply)


@pytest_asyncio.fixture
async def schema_connection():
    assert config.DB_HOST in ("127.0.0.1", "localhost", "::1") and config.DB_NAME == "test"
    schema = "audit_migration_" + uuid4().hex
    engine = create_async_engine(config.database_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            for name in (
                "user",
                "prescription_version",
                "medication_candidate_search",
                "medication_candidate_search_result",
                "rag_runtime_environment",
                "rag_candidate_index_version",
                "rag_source_ingestion_run",
            ):
                await connection.execute(text(f'CREATE TABLE "{name}" (id CHAR(36) PRIMARY KEY)'))
            await connection.execute(
                text(
                    "CREATE TABLE prescription_version_medication (id CHAR(36) PRIMARY KEY, "
                    "prescription_version_id CHAR(36), UNIQUE(id, prescription_version_id))"
                )
            )
            await connection.execute(
                text("CREATE TABLE rag_request_guard_runtime_binding (request_guard_decision_id CHAR(36) PRIMARY KEY)")
            )
            await connection.execute(
                text(
                    "CREATE TABLE rag_source_snapshot (id CHAR(36) PRIMARY KEY, source_version VARCHAR(200), "
                    "UNIQUE(id, source_version))"
                )
            )
            legacy = str(uuid4())
            await connection.execute(text("INSERT INTO medication_candidate_search(id) VALUES (:id)"), {"id": legacy})
            await _apply(connection, "upgrade")
            assert await connection.scalar(text("SELECT id FROM medication_candidate_search")) == legacy
            yield connection
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_matches_models_and_empty_downgrade(schema_connection) -> None:
    connection = schema_connection

    def compare(sync):
        inspector = inspect(sync)
        for model in (RagCandidateRequestGuardAudit, RagSourceCaptureObservation):
            table = model.__table__
            actual = {item["name"]: item for item in inspector.get_columns(table.name)}
            assert set(actual) == set(table.columns.keys())
            assert {name: item["nullable"] for name, item in actual.items()} == {
                column.name: column.nullable for column in table.columns
            }
            assert {item["name"] for item in inspector.get_check_constraints(table.name)} == {
                constraint.name
                for constraint in table.constraints
                if constraint.__class__.__name__ == "CheckConstraint"
            }
            assert len(inspector.get_foreign_keys(table.name)) == len(table.foreign_key_constraints)

    await connection.run_sync(compare)
    await _apply(connection, "downgrade")
    assert await connection.scalar(text("SELECT count(*) FROM medication_candidate_search")) == 1


@pytest.mark.asyncio
async def test_capture_constraints_and_downgrade_preserve_observations(schema_connection) -> None:
    connection = schema_connection
    snapshot_id, run_id = str(uuid4()), str(uuid4())
    await connection.execute(text("INSERT INTO rag_source_snapshot VALUES (:id, 'synthetic-v1')"), {"id": snapshot_id})
    await connection.execute(text("INSERT INTO rag_source_ingestion_run VALUES (:id)"), {"id": run_id})
    sql = text(
        "INSERT INTO rag_source_capture_observation (capture_observation_id, ingestion_run_id, "
        "source_snapshot_id, source_version, canonical_checksum, source_code, endpoint_code, operation_code, "
        "capture_protocol_version, issuer_ref, request_profile_sha256, capture_origin, capture_started_at, "
        "capture_completed_at, recorded_at, raw_manifest_sha256, endpoint_contract_receipt_sha256, "
        "full_scan_completed, primary_key_validation_passed, page_count, record_count, observation_content_sha256) "
        "VALUES (:id, :run, :snapshot, 'synthetic-v1', :hash, 'SYNTHETIC', 'SYNTHETIC', 'SYNTHETIC', "
        "'1.0', '{}'::jsonb, :hash, :origin, '2026-11-04T00:00:00Z', '2026-11-04T00:01:00Z', "
        "'2026-11-04T00:02:00Z', :hash, :hash, :full, true, :pages, 1, :hash)"
    )
    values = dict(
        id=str(uuid4()), run=run_id, snapshot=snapshot_id, hash="a" * 64, origin="LIVE_PROVIDER", full=True, pages=1
    )
    for patch in ({"origin": "RAW_REPLAY"}, {"full": False}, {"pages": 0}, {"snapshot": str(uuid4())}):
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(sql, values | patch)
    await connection.execute(sql, values)
    with pytest.raises(RuntimeError, match="lose immutable"):
        await _apply(connection, "downgrade")
    assert await connection.scalar(text("SELECT count(*) FROM rag_source_capture_observation")) == 1
    await connection.execute(text("DELETE FROM rag_source_capture_observation"))
    await _apply(connection, "downgrade")
