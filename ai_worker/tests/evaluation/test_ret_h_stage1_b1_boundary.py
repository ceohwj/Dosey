"""Legacy RET-H Stage 1 stays closed until its B1 migration is approved."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai_worker.tasks.evaluation import ret_h_bootstrap_stages as stages


@pytest.mark.parametrize("credentials", ["none", "writer", "publisher", "both"])
def test_stage1_exits_before_credentials_database_fixture_or_receipt_io(
    monkeypatch, tmp_path, capsys, caplog, credentials
):
    secret = "synthetic-stage1-secret"
    environment = {}
    for role in ("WRITER", "PUBLISHER"):
        if credentials in (role.lower(), "both"):
            environment[f"SOURCE_{role}_USER"] = "synthetic-user"
            environment[f"SOURCE_{role}_PASSWORD"] = secret
    env = Mock(wraps=environment)
    monkeypatch.setattr(stages, "os", SimpleNamespace(environ=env))
    calls = []
    for name in (
        "_build_database_url_for_role",
        "create_async_engine",
        "async_sessionmaker",
        "load_ret_h_smoke_synthetic_fixture",
        "bootstrap_ret_h_smoke_stage1_source",
        "stage1_receipt_to_dict",
    ):
        call = Mock(side_effect=AssertionError(f"Stage 1 reached {name}"))
        monkeypatch.setattr(stages, name, call)
        calls.append(call)
    destination = tmp_path / "receipt.json"
    with monkeypatch.context() as io_patch:
        for name in ("read_text", "write_text", "mkdir", "replace", "resolve"):
            call = Mock(side_effect=AssertionError(f"Stage 1 reached Path.{name}"))
            io_patch.setattr(Path, name, call)
            calls.append(call)
        with pytest.raises(SystemExit) as failure:
            stages.main(["stage1", "--output-receipt", str(destination)])
    assert failure.value.code == 1
    assert not env.mock_calls
    for call in calls:
        call.assert_not_called()
    assert not destination.exists()
    captured = capsys.readouterr()
    assert "Stage 1 is disabled pending B1 publication/selection migration" in captured.err + caplog.text
    assert secret not in captured.out + captured.err + caplog.text


@pytest.mark.parametrize("arguments", [["--help"], ["stage1", "--help"]])
def test_stage1_help_marks_migration_pending(arguments, capsys):
    with pytest.raises(SystemExit) as failure:
        stages.main(arguments)
    assert failure.value.code == 0
    assert "disabled pending B1 migration" in capsys.readouterr().out
