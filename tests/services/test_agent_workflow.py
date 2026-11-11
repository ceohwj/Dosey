import copy
import importlib.util
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/agent_workflow.py"


def test_discover_empty_store_without_creating_it(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "discover", "--root", str(tmp_path)], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"tasks": []}
    assert not (tmp_path / ".omx").exists()


def record(task_id="one"):
    return {
        "schema_version": 1,
        "task_id": task_id,
        "goal": "goal",
        "base_revision": "abc",
        "allowed_files": ["source.py"],
        "forbidden_files": [],
        "dependencies": [],
        "authoritative_contract": [],
        "acceptance_criteria": [],
        "required_tests": [],
        "non_goals": [],
        "phase": "planned",
        "revision": 0,
        "completed": [],
        "known_gaps": [],
        "next_action": "continue",
        "knowledge_refs": [],
        "budget": {"max_cycles": 3, "max_active_seconds": 1200, "cycles_used": 0, "active_seconds": 0},
        "operation_in_flight": False,
    }


def cli(root, *args):
    return subprocess.run([sys.executable, str(SCRIPT), *args, "--root", str(root)], capture_output=True, text=True)


def test_claim_blocks_different_task_before_source_write(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("original")
    first = cli(tmp_path, "claim", "--task", "one")
    assert first.returncode == 0
    second = cli(tmp_path, "claim", "--task", "two")
    if second.returncode == 0:
        source.write_text("modified")
    assert second.returncode != 0
    assert json.loads(second.stderr)["code"] == "conflict"
    assert source.read_text() == "original"


def test_discover_and_restore_canonical_without_cache(tmp_path):
    folder = tmp_path / ".omx/plans/dosey-agent-tasks/one"
    folder.mkdir(parents=True)
    folder.joinpath("task.json").write_text(json.dumps(record()))
    found = cli(tmp_path, "discover")
    assert json.loads(found.stdout)["tasks"][0]["task_id"] == "one"
    restored = json.loads(cli(tmp_path, "restore", "--task", "one").stdout)
    assert restored["task"]["next_action"] == "continue"
    assert restored["verification"] == "UNKNOWN"


def test_checkpoint_cas_and_release(tmp_path):
    token = json.loads(cli(tmp_path, "claim", "--task", "one").stdout)["claim"]
    input_file = tmp_path / "input.json"
    input_file.write_text(json.dumps(record()))
    args = ("checkpoint", "--task", "one", "--claim", token, "--expected-revision", "0", "--record", str(input_file))
    saved = cli(tmp_path, *args)
    assert json.loads(saved.stdout)["revision"] == 1
    assert cli(tmp_path, *args).returncode != 0
    assert cli(tmp_path, "release", "--task", "one", "--claim", "bad").returncode != 0
    assert json.loads(cli(tmp_path, "release", "--task", "one", "--claim", token).stdout)["released"]


def test_unknown_budget_prevents_claim(tmp_path):
    folder = tmp_path / ".omx/plans/dosey-agent-tasks/one"
    folder.mkdir(parents=True)
    item = record()
    item["budget"]["cycles_used"] = None
    folder.joinpath("task.json").write_text(json.dumps(item))
    result = cli(tmp_path, "claim", "--task", "one")
    assert result.returncode != 0
    assert json.loads(result.stderr)["code"] == "reconciliation_required"


def module():
    assert SCRIPT.exists(), "workflow implementation is missing"
    spec = importlib.util.spec_from_file_location("agent_workflow", SCRIPT)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def canonical(root, item=None):
    item = item or record()
    folder = root / ".omx/plans/dosey-agent-tasks" / item["task_id"]
    folder.mkdir(parents=True, exist_ok=True)
    folder.joinpath("task.json").write_text(json.dumps(item))
    return folder


def initialized(root):
    api = module()
    token = api.claim_writer(root, "one")["claim"]
    api.checkpoint_task(root, "one", token, 0, record())
    return api, token


@pytest.mark.parametrize("different", [False, True])
def test_racing_claims_admit_one_before_fixture_write(tmp_path, different):
    fixture = tmp_path / "fixture"
    fixture.write_text("original")
    runner = """import json, subprocess, sys, time
from pathlib import Path
script, root, task, gate, marker = sys.argv[1:]
while not Path(gate).exists(): time.sleep(.005)
p = subprocess.run([sys.executable, script, 'claim', '--task', task, '--root', root], capture_output=True, text=True)
if p.returncode == 0: Path(marker).write_text(task)
print(json.dumps({'returncode':p.returncode, 'stdout':p.stdout, 'stderr':p.stderr}))
"""
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                runner,
                str(SCRIPT),
                str(tmp_path),
                task,
                str(tmp_path / "go"),
                str(tmp_path / f"winner{i}"),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        for i, task in enumerate(["one", "two" if different else "one"])
    ]
    (tmp_path / "go").touch()
    results = [json.loads(p.communicate(timeout=10)[0]) for p in processes]
    assert sorted(r["returncode"] for r in results) == [0, 1]
    assert len(list(tmp_path.glob("winner*"))) == 1
    assert fixture.read_text() == "original"


def test_restore_retains_canonical_and_detects_changes(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=synthetic@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "synthetic",
        ],
        check=True,
    )
    api, token = initialized(tmp_path)
    item = record()
    item.update(phase="running", completed=["done"], known_gaps=["gap"], next_action="next")
    item["authoritative_contract"] = ["policy.md"]
    item["knowledge_refs"] = ["notes.md"]
    item["budget"]["active_seconds"] = 0.5
    api.checkpoint_task(tmp_path, "one", token, 1, item)
    assert api.restore_task(tmp_path, "one")["verification"] == "MATCHED_SNAPSHOT"
    for filename in ["source.py", "policy.md", "notes.md"]:
        (tmp_path / filename).write_text("changed")
        assert api.restore_task(tmp_path, "one")["verification"] == "STALE"
        api.checkpoint_task(tmp_path, "one", token, api.restore_task(tmp_path, "one")["task"]["revision"], item)
    folder = tmp_path / ".omx/plans/dosey-agent-tasks/one"
    for contents in [None, "no json", "{}", "[]"]:
        cache = folder / "checkpoint.json"
        cache.unlink()
        if contents is not None:
            cache.write_text(contents)
        restored = api.restore_task(tmp_path, "one")
        assert restored["verification"] == "UNKNOWN"
        assert restored["task"]["completed"] == ["done"]
        assert restored["task"]["next_action"] == "next"
        assert restored["task"]["budget"]["active_seconds"] == 0.5
        if not cache.exists():
            cache.write_text("{}")


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("revision", True),
        ("revision", -1),
        ("phase", "other"),
        ("operation_in_flight", 1),
        ("completed", "done"),
        ("goal", None),
    ],
)
def test_invalid_record_fields(field, value):
    api = module()
    item = record()
    item[field] = value
    with pytest.raises(api.WorkflowError):
        api.validate_record(item)


@pytest.mark.parametrize(
    "field,value",
    [
        ("cycles_used", True),
        ("cycles_used", 1.2),
        ("active_seconds", -1),
        ("active_seconds", float("nan")),
        ("active_seconds", float("inf")),
        ("max_cycles", None),
        ("max_active_seconds", True),
    ],
)
def test_invalid_budget(field, value):
    api = module()
    item = record()
    item["budget"][field] = value
    with pytest.raises(api.WorkflowError):
        api.validate_record(item)


@pytest.mark.parametrize("change", ["null_cycles", "null_seconds", "inflight", "cycles_cap", "time_cap"])
def test_unreconciled_or_exhausted_budget_blocks_admission(tmp_path, change):
    api = module()
    item = record()
    if change == "inflight":
        item["operation_in_flight"] = True
    else:
        field, value = {
            "null_cycles": ("cycles_used", None),
            "null_seconds": ("active_seconds", None),
            "cycles_cap": ("cycles_used", 3),
            "time_cap": ("active_seconds", 1200),
        }[change]
        item["budget"][field] = value
    canonical(tmp_path, item)
    assert api.restore_task(tmp_path, "one")["reconciliation_required"]
    with pytest.raises(api.WorkflowError):
        api.claim_writer(tmp_path, "one")
    assert not (tmp_path / ".omx/plans/dosey-agent-tasks/.writer-claim.json").exists()


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        "a/../b",
        ".env",
        "envs/.local.env",
        "data/raw.md",
        "uploads/x.md",
        ".git/config",
        "docs/*.md",
    ],
)
def test_unsafe_context_paths_rejected(path):
    api = module()
    item = record()
    item["allowed_files"] = [path]
    with pytest.raises(api.WorkflowError):
        api.validate_record(item)


def test_document_refs_and_bounded_json(tmp_path):
    api = module()
    for field in ["knowledge_refs", "authoritative_contract"]:
        item = record()
        item[field] = ["source.py"]
        with pytest.raises(api.WorkflowError):
            api.validate_record(item)
    item = record()
    item["extra"] = "x" * 1_100_000
    with pytest.raises(api.WorkflowError):
        api.validate_record(item)
    folder = canonical(tmp_path)
    folder.joinpath("task.json").write_text(" " * 1_100_000)
    with pytest.raises(api.WorkflowError):
        api.restore_task(tmp_path, "one")


@pytest.mark.parametrize("component", [".omx", ".omx/plans", ".omx/plans/dosey-agent-tasks", "source.py"])
def test_symlink_components_rejected(tmp_path, component):
    api = module()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = tmp_path / component
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(api.WorkflowError):
        if component == "source.py":
            claim = api.claim_writer(tmp_path, "one")["claim"]
            api.checkpoint_task(tmp_path, "one", claim, 0, record())
        else:
            api.discover_tasks(tmp_path)
    assert list(outside.iterdir()) == []


def test_cache_failure_preserves_canonical_and_counter_monotonicity(tmp_path, monkeypatch):
    api, token = initialized(tmp_path)
    original = api.os.replace

    def fail_cache(source, destination):
        if Path(destination).name == "checkpoint.json":
            raise OSError("injected cache failure")
        return original(source, destination)

    monkeypatch.setattr(api.os, "replace", fail_cache)
    item = record()
    item["next_action"] = "saved before cache failure"
    item["budget"]["cycles_used"] = 1
    with pytest.raises(api.WorkflowError):
        api.checkpoint_task(tmp_path, "one", token, 1, item)
    restored = api.restore_task(tmp_path, "one")
    assert restored["task"]["revision"] == 2
    assert restored["task"]["next_action"] == item["next_action"]
    assert restored["verification"] == "STALE"
    monkeypatch.setattr(api.os, "replace", original)
    with pytest.raises(api.WorkflowError):
        api.checkpoint_task(tmp_path, "one", token, 2, record())
    unknown = copy.deepcopy(item)
    unknown["budget"]["cycles_used"] = None
    api.checkpoint_task(tmp_path, "one", token, 2, unknown)
    with pytest.raises(api.WorkflowError):
        api.checkpoint_task(tmp_path, "one", token, 3, item)


def test_checkpoint_compare_and_swap_and_release_share_lock(tmp_path, monkeypatch):
    api, token = initialized(tmp_path)
    import threading

    entered, proceed = threading.Event(), threading.Event()
    original = api.os.replace

    def pause(source, destination):
        if Path(destination).name == "task.json":
            entered.set()
            assert proceed.wait(5)
        return original(source, destination)

    monkeypatch.setattr(api.os, "replace", pause)
    with ThreadPoolExecutor() as pool:
        saving = pool.submit(api.checkpoint_task, tmp_path, "one", token, 1, record())
        assert entered.wait(5)
        releasing = pool.submit(api.release_writer, tmp_path, "one", token)
        assert not releasing.done()
        proceed.set()
        assert saving.result()["revision"] == 2
        assert releasing.result()["released"]
    with pytest.raises(api.WorkflowError):
        api.checkpoint_task(tmp_path, "one", token, 2, record())


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["bogus"],
        ["restore"],
        ["restore", "--task", "../bad"],
        ["checkpoint", "--task", "one", "--claim", "x", "--expected-revision", "oops", "--record", "missing"],
    ],
)
def test_cli_errors_are_json_without_tracebacks(tmp_path, args):
    result = cli(tmp_path, *args)
    assert result.returncode != 0
    assert set(json.loads(result.stderr)) == {"error", "code"}
    assert not result.stdout
    assert "Traceback" not in result.stderr


def test_actual_process_crash_after_canonical_replace(tmp_path):
    api, token = initialized(tmp_path)
    runner = """import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('workflow', sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
root, token = sys.argv[2:]
item = m.restore_task(root, 'one')['task']
item['next_action'] = 'crash progress retained'
item['operation_in_flight'] = True
item['budget']['cycles_used'] = 1
original = m.os.replace
def crash(source, destination):
    original(source, destination)
    if Path(destination).name == 'task.json': os._exit(73)
m.os.replace = crash
m.checkpoint_task(root, 'one', token, 1, item)
"""
    result = subprocess.run([sys.executable, "-c", runner, str(SCRIPT), str(tmp_path), token])
    assert result.returncode == 73
    restored = api.restore_task(tmp_path, "one")
    assert restored["task"]["revision"] == 2
    assert restored["task"]["next_action"] == "crash progress retained"
    assert restored["task"]["budget"]["cycles_used"] == 1
    assert restored["verification"] == "UNKNOWN"
    assert restored["reconciliation_required"]
    api.release_writer(tmp_path, "one", token)
    with pytest.raises(api.WorkflowError):
        api.claim_writer(tmp_path, "one")


def test_subprocess_checkpoint_cas_one_winner(tmp_path):
    api, token = initialized(tmp_path)
    input_file = tmp_path / "record.json"
    input_file.write_text(json.dumps(record()))
    args = [
        sys.executable,
        str(SCRIPT),
        "checkpoint",
        "--root",
        str(tmp_path),
        "--task",
        "one",
        "--claim",
        token,
        "--expected-revision",
        "1",
        "--record",
        str(input_file),
    ]
    processes = [subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    results = [p.communicate(timeout=10) for p in processes]
    assert sorted(p.returncode for p in processes) == [0, 1]
    loser = next(result for p, result in zip(processes, results, strict=True) if p.returncode)
    assert json.loads(loser[1])["code"] == "conflict"
    assert api.restore_task(tmp_path, "one")["task"]["revision"] == 2


@pytest.mark.parametrize("relative", [".env", "data/record.json"])
def test_cli_record_input_rejects_sensitive_paths_before_read(tmp_path, relative):
    token = json.loads(cli(tmp_path, "claim", "--task", "one").stdout)["claim"]
    sensitive = tmp_path / relative
    sensitive.parent.mkdir(exist_ok=True)
    sensitive.write_text(json.dumps(record()))
    result = cli(
        tmp_path,
        "checkpoint",
        "--task",
        "one",
        "--claim",
        token,
        "--expected-revision",
        "0",
        "--record",
        str(sensitive),
    )
    assert result.returncode != 0
    assert json.loads(result.stderr)["code"] == "invalid"
    assert not (tmp_path / ".omx/plans/dosey-agent-tasks/one").exists()


def test_symlink_cache_and_nonregular_records_rejected(tmp_path):
    api, _ = initialized(tmp_path)
    folder = tmp_path / ".omx/plans/dosey-agent-tasks/one"
    cache = folder / "checkpoint.json"
    cache.unlink()
    cache.symlink_to(tmp_path / "missing")
    with pytest.raises(api.WorkflowError):
        api.restore_task(tmp_path, "one")
    cache.unlink()
    canonical_file = folder / "task.json"
    canonical_file.unlink()
    import os

    os.mkfifo(canonical_file)
    with pytest.raises(api.WorkflowError):
        api.restore_task(tmp_path, "one")


def test_safe_record_input_under_repository_with_private_ancestor(tmp_path):
    root = tmp_path / ".codex/worktrees/repo"
    root.mkdir(parents=True)
    token = json.loads(cli(root, "claim", "--task", "one").stdout)["claim"]
    input_file = root / "record.json"
    input_file.write_text(json.dumps(record()))
    result = cli(
        root, "checkpoint", "--task", "one", "--claim", token, "--expected-revision", "0", "--record", str(input_file)
    )
    assert result.returncode == 0, result.stderr


def test_read_only_missing_task_never_creates_store(tmp_path):
    for command in ["restore", "validate"]:
        result = cli(tmp_path, command, "--task", "one")
        assert result.returncode != 0
        assert set(json.loads(result.stderr)) == {"error", "code"}
        assert not (tmp_path / ".omx").exists()
