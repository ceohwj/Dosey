"""Offline guards for the disposable Source publisher harness."""

import json
from types import SimpleNamespace

import pytest

from scripts.ci import source_publisher_docker_check as harness


@pytest.fixture(autouse=True)
def ample_disk(monkeypatch):
    monkeypatch.setattr(harness.shutil, "disk_usage", lambda path: SimpleNamespace(free=1024 * 1024 * 1024))


def test_environment_rejects_endpoint_overrides_and_drops_unrelated_values():
    with pytest.raises(ValueError):
        harness.safe_environment({"DOCKER_HOST": "tcp://example:2375"})
    assert harness.safe_environment({"PATH": "/bin", "DB_PASSWORD": "private"}) == {"PATH": "/bin"}


def test_context_rejects_remote(monkeypatch, tmp_path):
    def fake_run(args, **kwargs):
        if args[-1] == "show":
            return SimpleNamespace(stdout="remote\n")
        return SimpleNamespace(stdout=json.dumps([{"Endpoints": {"docker": {"Host": "tcp://example:2375"}}}]))

    monkeypatch.setattr(harness, "run", fake_run)
    with pytest.raises(ValueError, match="not local"):
        harness.verify_context({}, tmp_path)


def container_info():
    return {
        "Config": {
            "Labels": {"com.docker.compose.project": "owned", "com.docker.compose.service": "postgres"},
            "Image": harness.IMAGE,
        },
        "NetworkSettings": {"Ports": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "49152"}]}},
        "HostConfig": {"Tmpfs": {"/var/lib/postgresql/data": ""}},
        "Mounts": [],
    }


def test_container_requires_owned_loopback_tmpfs():
    info = container_info()
    assert harness.verify_container(info, "owned") == 49152
    with pytest.raises(ValueError):
        harness.verify_container(info, "other")
    info["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostIp"] = "0.0.0.0"
    with pytest.raises(ValueError):
        harness.verify_container(info, "owned")
    info = container_info()
    info["Mounts"] = [{"Type": "volume"}]
    with pytest.raises(ValueError):
        harness.verify_container(info, "owned")


@pytest.mark.parametrize(
    "child, expected", [("", True), ("<skipped/>", False), ("<failure/>", False), ("<error/>", False)]
)
def test_junit_requires_one_executed_passing_test(tmp_path, child, expected):
    report = tmp_path / "result.xml"
    report.write_text(f"<testsuite><testcase>{child}</testcase></testsuite>")
    assert harness.junit_pass(report) is expected


def test_main_stops_before_creation_for_remote_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote:2375")
    monkeypatch.setattr(harness, "run", lambda *args, **kwargs: pytest.fail("must not call Docker"))
    assert harness.main(["--python", str(harness.sys.executable)]) == 1


def test_command_failure_does_not_expose_subprocess_output(monkeypatch, tmp_path):
    monkeypatch.setattr(
        harness.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="secret")
    )
    with pytest.raises(RuntimeError) as error:
        harness.run(["docker", "info"], env={}, cwd=tmp_path)
    assert "secret" not in str(error.value)


def test_container_accepts_inspected_tmpfs_mount():
    info = container_info()
    info["Mounts"] = [{"Type": "tmpfs", "Destination": "/var/lib/postgresql/data"}]
    assert harness.verify_container(info, "owned") == 49152


@pytest.mark.parametrize("failure", ["timeout", "signal", "ownership", "cleanup", "discovery_timeout"])
def test_lifecycle_failures_preserve_status_and_protect_cleanup(monkeypatch, failure):  # noqa: C901
    import signal
    import subprocess

    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.delenv("DOCKER_TLS_VERIFY", raising=False)
    monkeypatch.delenv("DOCKER_CERT_PATH", raising=False)
    monkeypatch.setattr(harness, "verify_context", lambda env, cwd: "local")
    monkeypatch.setattr(harness, "verify_container", lambda info, project: 49152)
    state = {"project": None, "marker": None, "tests": 0, "inspects": 0, "removed": [], "network_checked": False}
    container = "a" * 64
    events = []
    original_record = harness.record_manifest

    def capture_manifest(path, event):
        events.append(event)
        original_record(path, event)

    monkeypatch.setattr(harness, "record_manifest", capture_manifest)

    def fake_run(args, **kwargs):  # noqa: C901
        if args[:2] == ["docker", "compose"] and "-p" in args:
            state["project"] = args[args.index("-p") + 1]
        if args[:2] == ["docker", "inspect"]:
            state["inspects"] += 1
            project = state["project"]
            if failure == "ownership" and state["inspects"] > 1:
                project = "somebody-else"
            return SimpleNamespace(
                stdout=json.dumps(
                    [{"Config": {"Labels": {"com.docker.compose.project": project}}, "Image": "sha256:test"}]
                ),
                returncode=0,
            )
        if args[:3] == ["docker", "compose", "-p"] and "ps" in args:
            if failure == "discovery_timeout" and "-aq" in args:
                raise subprocess.TimeoutExpired(args, 1)
            return SimpleNamespace(stdout=container, returncode=0)
        if args[:2] == ["docker", "exec"]:
            state["marker"] = kwargs["input"].split("VALUES ('")[1].split("'")[0]
        if "-c" in args and args[0] != "docker" and "pytest" not in args:
            return SimpleNamespace(stdout=state["marker"], returncode=0)
        if "pytest" in args:
            state["tests"] += 1
            if state["tests"] == 2:
                if failure == "signal":
                    raise KeyboardInterrupt(signal.SIGTERM)
                if failure in {"timeout", "discovery_timeout"}:
                    raise subprocess.TimeoutExpired(args, 1)
            report = next(arg.split("=", 1)[1] for arg in args if arg.startswith("--junitxml="))
            harness.Path(report).write_text("<testsuite><testcase/></testsuite>")
            if failure == "discovery_timeout" and state["tests"] == 1:
                return SimpleNamespace(stdout="", stderr="", returncode=17)
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if args[:3] == ["docker", "network", "ls"]:
            state["network_checked"] = True
        if args[:3] == ["docker", "rm", "-f"]:
            state["removed"].append(args[-1])
            if failure == "cleanup":
                raise RuntimeError("cleanup failed")
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(harness, "run", fake_run)
    status = harness.main(["--python", harness.sys.executable])
    expected = 128 + signal.SIGTERM if failure == "signal" else 17 if failure == "discovery_timeout" else 1
    assert status == expected
    assert state["removed"] == ([] if failure == "ownership" else [container])
    assert state["network_checked"]
    assert any(event["event"] == "compose_up_attempted" for event in events)
    assert {"event": "container_cleanup_candidate", "id": container} in events


def test_daemon_timeout_happens_before_compose_up(monkeypatch):
    import subprocess

    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(harness, "verify_context", lambda env, cwd: "local")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[0] != "docker":
            return SimpleNamespace(stdout="", returncode=0)
        assert args[:2] == ["docker", "info"]
        assert kwargs["timeout"] == 15
        raise subprocess.TimeoutExpired(args, 15)

    monkeypatch.setattr(harness, "run", fake_run)
    assert harness.main(["--python", harness.sys.executable]) == 1
    assert len(calls) == 2


def test_node_log_redacts_passwords_and_is_private(tmp_path):
    log = tmp_path / "node.log"
    harness.write_node_log(
        log, "failure synthetic-provision398-only and admin-secret", "synthetic-ab12-only", "admin-secret"
    )
    output = log.read_text()
    assert "failure" in output
    assert "admin-secret" not in output
    assert "synthetic-" not in output
    assert output.count("[REDACTED]") == 3
    assert log.stat().st_mode & 0o777 == 0o600


def test_role_checks_are_opt_in_and_preserve_default_nodes():
    assert harness.selected_nodes(False) == harness.NODES
    assert len(harness.NODES) == 3
    assert harness.selected_nodes(True) == harness.NODES + harness.ROLE_NODES
    assert len(harness.ROLE_NODES) == 4


def test_low_disk_stops_before_docker_and_records_project(monkeypatch, tmp_path):
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        monkeypatch.delenv(name, raising=False)
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    original_mkdtemp = harness.tempfile.mkdtemp

    def make_temp(*args, **kwargs):
        if kwargs.get("prefix") == "source-publisher-evidence-":
            return str(evidence)
        return original_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(harness.tempfile, "mkdtemp", make_temp)
    monkeypatch.setattr(harness.shutil, "disk_usage", lambda path: SimpleNamespace(free=511 * 1024 * 1024))
    monkeypatch.setattr(harness, "run", lambda *args, **kwargs: pytest.fail("must not run Docker"))
    assert harness.main(["--python", harness.sys.executable]) == 1
    manifest = evidence / "manifest.jsonl"
    entries = [json.loads(line) for line in manifest.read_text().splitlines()]
    assert len(entries) == 1 and entries[0]["event"] == "initialized"
    assert entries[0]["project"].startswith("sourcepublisher")
    assert evidence.stat().st_mode & 0o777 == 0o700
    assert manifest.stat().st_mode & 0o777 == 0o600
