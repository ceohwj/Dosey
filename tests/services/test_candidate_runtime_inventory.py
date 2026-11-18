from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts.audit_candidate_runtime import BINDING_COLUMNS, audit, local_database_url


@pytest.mark.asyncio
async def test_missing_configuration_is_unknown_without_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = MagicMock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr("scripts.audit_candidate_runtime.create_async_engine", engine)
    result = await audit({})
    assert result["status"] == "UNAVAILABLE"
    assert result["unexpired_candidate_keys"] is None
    assert result["legacy_retry_support_required"] is None
    engine.assert_not_called()


@pytest.mark.parametrize("scope,host", [("production", "127.0.0.1"), ("local", "db.example.com")])
@pytest.mark.asyncio
async def test_remote_or_nonlocal_configuration_never_connects(
    monkeypatch: pytest.MonkeyPatch, scope: str, host: str
) -> None:
    engine = MagicMock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr("scripts.audit_candidate_runtime.create_async_engine", engine)
    result = await audit(
        {"ENV": scope, "DB_HOST": host, "DB_USER": "synthetic", "DB_PASSWORD": "secret", "DB_NAME": "test"}
    )
    assert result["status"] == "UNAVAILABLE"
    assert "secret" not in str(result)
    engine.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_port_remains_unknown() -> None:
    result = await audit(
        {
            "DB_HOST": "127.0.0.1",
            "DB_USER": "synthetic",
            "DB_PASSWORD": "synthetic",
            "DB_NAME": "test",
            "DB_PORT": "not-a-port",
        }
    )
    assert result["reason"] == "INVALID_LOCAL_CONFIGURATION"
    assert result["unexpired_candidate_keys"] is None


def test_compose_local_host_uses_exposed_port_and_preserves_escaped_password() -> None:
    url = local_database_url(
        {
            "DB_HOST": "postgres",
            "DB_EXPOSE_PORT": "15432",
            "DB_USER": "synthetic",
            "DB_PASSWORD": "p@ss/word",
            "DB_NAME": "test",
        }
    )
    assert url is not None
    assert (url.host, url.port, url.password) == ("127.0.0.1", 15432, "p@ss/word")


@pytest.mark.asyncio
async def test_connection_error_is_redacted_and_does_not_become_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = MagicMock()
    engine.connect.side_effect = RuntimeError("postgresql://synthetic:secret@localhost/patient_database")
    engine.dispose = AsyncMock()
    monkeypatch.setattr("scripts.audit_candidate_runtime.create_async_engine", MagicMock(return_value=engine))
    result = await audit({"DB_HOST": "127.0.0.1", "DB_USER": "synthetic", "DB_PASSWORD": "secret", "DB_NAME": "test"})
    assert result["status"] == "UNAVAILABLE"
    assert result["reason"] == "DATABASE_AUDIT_FAILED"
    assert result["unexpired_candidate_keys"] is None
    assert "secret" not in str(result) and "patient_database" not in str(result)
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_success_requires_read_only_before_aggregate_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = AsyncMock()
    connection.begin = MagicMock()
    counts = MagicMock()
    counts.mappings.return_value.one.return_value = {
        "unexpired": 3,
        "latest_expiry": datetime(2026, 10, 30, tzinfo=UTC),
    }
    connection.execute.side_effect = [MagicMock(), MagicMock(), counts]
    connection.scalar.side_effect = ["on", "idempotency_record", "medication_candidate_search", "alembic_version", 2, 1]
    connection.scalars.side_effect = [["924a1b2c3d4e"], list(BINDING_COLUMNS)]
    engine = MagicMock()
    engine.connect.return_value.__aenter__.return_value = connection
    engine.dispose = AsyncMock()
    factory = MagicMock(return_value=engine)
    monkeypatch.setattr("scripts.audit_candidate_runtime.create_async_engine", factory)
    result = await audit({"DB_HOST": "127.0.0.1", "DB_USER": "synthetic", "DB_PASSWORD": "secret", "DB_NAME": "test"})
    assert result["status"] == "OBSERVED"
    assert result["unexpired_candidate_keys"] == 3
    assert result["runtime_bound_search_rows"] == 1
    assert result["legacy_retry_support_required"] is True
    statements = [str(call.args[0]) for call in connection.execute.await_args_list]
    assert statements[0] == "SET TRANSACTION READ ONLY"
    assert statements[1] == "SET LOCAL statement_timeout = '5s'"
    assert statements[2].startswith("SELECT count(*)")
    assert factory.call_args.kwargs["hide_parameters"] is True
    assert "secret" not in str(result)
