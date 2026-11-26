"""Chat citation claim-verdict migration (`926a1b2c3d4e`) in an isolated PostgreSQL schema."""

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
from app.models.chat import ChatCitation

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "apps/backend/alembic/versions/926a1b2c3d4e_extend_chat_citation_for_claim_verdicts.py"


def _module():
    spec = importlib.util.spec_from_file_location("chat_citation_migration", PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_single_forward_head() -> None:
    settings = Config(str(ROOT / "apps/backend/alembic.ini"))
    settings.set_main_option("script_location", str(ROOT / "apps/backend/alembic"))
    assert ScriptDirectory.from_config(settings).get_heads() == ["926a1b2c3d4e"]
    assert _module().down_revision == "925a1b2c3d4e"


async def _apply(connection, operation: str) -> None:
    def apply(sync) -> None:
        module = _module()
        module.op = Operations(MigrationContext.configure(sync))
        getattr(module, operation)()

    await connection.run_sync(apply)


@pytest_asyncio.fixture
async def schema_connection():
    assert config.DB_HOST in ("127.0.0.1", "localhost", "::1") and "test" in config.DB_NAME.lower()
    schema = "chat_citation_mig_" + uuid4().hex
    engine = create_async_engine(config.database_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            await connection.execute(text("CREATE TABLE chat_message (id CHAR(36) PRIMARY KEY)"))
            await connection.execute(text("CREATE TABLE knowledge_chunk (id CHAR(36) PRIMARY KEY)"))
            await connection.execute(
                text(
                    "CREATE TABLE chat_citation ("
                    "id CHAR(36) PRIMARY KEY, "
                    "message_id CHAR(36) NOT NULL REFERENCES chat_message(id), "
                    "knowledge_chunk_id CHAR(36) NOT NULL REFERENCES knowledge_chunk(id), "
                    "claim_text VARCHAR(1000) NOT NULL, "
                    "cited_text VARCHAR(2000) NOT NULL, "
                    "display_order INTEGER NOT NULL, "
                    "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                    "CONSTRAINT uq_chat_citation_order UNIQUE (message_id, display_order), "
                    "CONSTRAINT chk_chat_citation_order CHECK (display_order > 0)"
                    ")"
                )
            )
            yield connection
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


@pytest.mark.asyncio
async def test_chat_citation_upgrade_matches_model_and_downgrade_restores_legacy(schema_connection) -> None:
    connection = schema_connection
    await _apply(connection, "upgrade")

    def inspect_upgraded(sync) -> None:
        inspector = inspect(sync)
        table = ChatCitation.__table__
        actual = {item["name"]: item for item in inspector.get_columns(table.name)}
        assert set(actual) == set(table.columns.keys())
        assert {name: item["nullable"] for name, item in actual.items()} == {
            column.name: column.nullable for column in table.columns
        }
        assert {item["name"] for item in inspector.get_check_constraints(table.name)} == {
            constraint.name for constraint in table.constraints if constraint.__class__.__name__ == "CheckConstraint"
        }
        assert {item["name"] for item in inspector.get_unique_constraints(table.name)} == {
            constraint.name for constraint in table.constraints if constraint.__class__.__name__ == "UniqueConstraint"
        }

    await connection.run_sync(inspect_upgraded)
    await _apply(connection, "downgrade")

    def inspect_downgraded(sync) -> None:
        inspector = inspect(sync)
        cols = {item["name"]: item for item in inspector.get_columns("chat_citation")}
        assert set(cols) == {
            "id",
            "message_id",
            "knowledge_chunk_id",
            "claim_text",
            "cited_text",
            "display_order",
            "created_at",
        }
        assert cols["knowledge_chunk_id"]["nullable"] is False
        assert cols["cited_text"]["nullable"] is False

    await connection.run_sync(inspect_downgraded)


@pytest.mark.asyncio
async def test_chat_citation_constraints_and_nullable_downgrade_guard(schema_connection) -> None:
    connection = schema_connection
    await _apply(connection, "upgrade")

    msg_id, chunk_id = str(uuid4()), str(uuid4())
    await connection.execute(text("INSERT INTO chat_message(id) VALUES (:id)"), {"id": msg_id})
    await connection.execute(text("INSERT INTO knowledge_chunk(id) VALUES (:id)"), {"id": chunk_id})

    insert_sql = text(
        "INSERT INTO chat_citation ("
        "id, message_id, knowledge_chunk_id, claim_key, claim_kind, verdict, "
        "claim_text, cited_text, source_code, source_version, locator, content_sha256, display_order"
        ") VALUES ("
        ":id, :msg_id, :chunk_id, :claim_key, :claim_kind, :verdict, "
        ":claim_text, :cited_text, :source_code, :source_version, :locator, :content_sha256, :display_order"
        ")"
    )
    base_supported = {
        "id": str(uuid4()),
        "msg_id": msg_id,
        "chunk_id": chunk_id,
        "claim_key": "c1",
        "claim_kind": "CRITICAL",
        "verdict": "SUPPORTED",
        "claim_text": "성인은 1일 1회 1정을 식후에 복용합니다.",
        "cited_text": "용법·용량 원문",
        "source_code": "MFDS_EDB",
        "source_version": "2026.11.01",
        "locator": "UD:p1",
        "content_sha256": "a" * 64,
        "display_order": 1,
    }

    for invalid_patch in (
        {"claim_kind": "CRITICAL", "verdict": "NEEDS_VERIFICATION", "chunk_id": None, "content_sha256": None},
        {"verdict": "SUPPORTED", "chunk_id": None},
        {"verdict": "SUPPORTED", "content_sha256": "not-a-hex-digest"},
        {"claim_text": "   "},
    ):
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(insert_sql, base_supported | invalid_patch | {"id": str(uuid4())})

    await connection.execute(insert_sql, base_supported)
    needs_verif_id = str(uuid4())
    await connection.execute(
        insert_sql,
        base_supported
        | {
            "id": needs_verif_id,
            "claim_key": "c2",
            "claim_kind": "NON_CRITICAL",
            "verdict": "NEEDS_VERIFICATION",
            "chunk_id": None,
            "cited_text": None,
            "content_sha256": None,
            "display_order": 2,
        },
    )

    with pytest.raises(RuntimeError, match="downgrade cannot restore NOT NULL"):
        await _apply(connection, "downgrade")

    await connection.execute(text("DELETE FROM chat_citation"))
    await _apply(connection, "downgrade")
