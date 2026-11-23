from scripts.ci.source_publisher_container_check import CONTEXT_PATHS, local_endpoint


def test_only_unix_docker_endpoints_are_accepted():
    assert local_endpoint("unix:///var/run/docker.sock")
    assert not local_endpoint("tcp://localhost:2375")
    assert not local_endpoint("ssh://localhost")
    assert not local_endpoint("unix:///var/run/docker.sock\nother")


def test_build_context_is_explicit_and_excludes_env_and_repo_root():
    assert "ai_worker" in CONTEXT_PATHS
    assert "." not in CONTEXT_PATHS
    assert all(".env" not in path for path in CONTEXT_PATHS)


def test_readiness_timeout_stops_before_build(monkeypatch):
    import json
    import subprocess

    import pytest

    from scripts.ci import source_publisher_container_check as probe

    monkeypatch.setattr(probe.os, "environ", {"PATH": "/bin"})
    monkeypatch.setattr(probe.shutil, "which", lambda *_args, **_kwargs: "/bin/true")
    monkeypatch.setattr(probe.shutil, "disk_usage", lambda _: type("Disk", (), {"free": 9 * 1024**3})())
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[1:3] == ["context", "inspect"]:
            return subprocess.CompletedProcess(
                args, 0, json.dumps([{"Endpoints": {"docker": {"Host": "unix:///tmp/docker.sock"}}}]), ""
            )
        assert args[1] == "info"
        assert kwargs["timeout"] == 15
        raise subprocess.TimeoutExpired(args, 15)

    monkeypatch.setattr(probe, "run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired):
        probe.main()
    assert len(calls) == 2


def test_original_build_failure_survives_unverified_cleanup(monkeypatch, capsys):
    import json
    import subprocess
    from pathlib import Path

    import pytest

    from scripts.ci import source_publisher_container_check as probe

    monkeypatch.setattr(probe.os, "environ", {"PATH": "/bin"})
    monkeypatch.setattr(probe.shutil, "which", lambda *_args, **_kwargs: "/bin/true")
    monkeypatch.setattr(probe.shutil, "disk_usage", lambda _: type("Disk", (), {"free": 9 * 1024**3})())
    monkeypatch.setattr(probe, "copy_context", lambda _: None)

    def fake_run(args, **kwargs):
        if args[1:3] == ["context", "inspect"]:
            return subprocess.CompletedProcess(
                args, 0, json.dumps([{"Endpoints": {"docker": {"Host": "unix:///tmp/docker.sock"}}}]), ""
            )
        if args[1] != "context":
            assert kwargs["env"]["DOCKER_HOST"] == "unix:///tmp/docker.sock"
            config = Path(kwargs["env"]["DOCKER_CONFIG"])
            assert json.loads((config / "config.json").read_text()) == {"auths": {}}
        if args[1] == "info":
            return subprocess.CompletedProcess(args, 0, "1", "")
        if args[1:3] == ["buildx", "version"]:
            return subprocess.CompletedProcess(args, 0, "buildx", "")
        if args[1:3] == ["buildx", "build"]:
            assert kwargs["timeout"] == 1800
            return subprocess.CompletedProcess(args, 1, "", "failed")
        return subprocess.CompletedProcess(args, 1, "", "daemon unavailable")

    monkeypatch.setattr(probe, "run", fake_run)
    with pytest.raises(RuntimeError, match="image build failed"):
        probe.main()
    captured = capsys.readouterr()
    assert "cleanup incomplete" in captured.err
    assert "Build cache and intermediate resource state is unknown" in captured.err


def test_embedded_container_probe_compiles():
    from scripts.ci.source_publisher_container_check import PROBE

    compile(PROBE, "<container-probe>", "exec")


def test_diagnostics_redact_sentinel_and_sensitive_lines(tmp_path):
    import stat

    from scripts.ci.source_publisher_container_check import SENTINEL, save_output

    save_output(
        tmp_path, "build", f"failure {SENTINEL}\nAuthorization: secret", "https://user:pass@example.test/problem"
    )
    stdout = tmp_path / "build.stdout.log"
    stderr = tmp_path / "build.stderr.log"
    assert SENTINEL not in stdout.read_text()
    assert "secret" not in stdout.read_text()
    assert "user:pass" not in stderr.read_text()
    assert "example.test/problem" in stderr.read_text()
    assert stat.S_IMODE(stdout.stat().st_mode) == 0o600
    assert stat.S_IMODE(stderr.stat().st_mode) == 0o600


def test_isolated_docker_config_never_reads_or_changes_existing_login(tmp_path):
    import json
    import stat

    from scripts.ci.source_publisher_container_check import isolated_docker_env

    home = tmp_path / "home"
    home.mkdir()
    existing = home / "config.json"
    existing.write_text("unchanged")
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    env = isolated_docker_env({"PATH": "/bin", "HOME": str(home)}, "unix:///tmp/docker.sock", fresh, "/bin/true")
    assert existing.read_text() == "unchanged"
    assert json.loads((fresh / "config.json").read_text()) == {"auths": {}}
    assert stat.S_IMODE((fresh / "config.json").stat().st_mode) == 0o600
    assert env["DOCKER_HOST"] == "unix:///tmp/docker.sock"
    assert env["DOCKER_CONFIG"] == str(fresh)
    assert env["PATH"] == str(fresh / "bin")
    assert [item.name for item in (fresh / "bin").iterdir()] == ["docker"]


def test_isolated_config_uses_only_buildx_from_installed_docker_bundle(tmp_path):
    import json

    from scripts.ci.source_publisher_container_check import isolated_docker_env

    resources = tmp_path / "Docker.app/Contents/Resources"
    (resources / "bin").mkdir(parents=True)
    docker = resources / "bin/docker"
    docker.write_text("synthetic executable")
    docker.chmod(0o755)
    (resources / "cli-plugins").mkdir()
    buildx = resources / "cli-plugins/docker-buildx"
    buildx.write_text("synthetic buildx")
    buildx.chmod(0o755)
    (resources / "cli-plugins/docker-other").write_text("must not load")
    fresh = tmp_path / "isolated"
    fresh.mkdir()
    isolated_docker_env({"PATH": "/bin"}, "unix:///tmp/docker.sock", fresh, str(docker))
    assert json.loads((fresh / "config.json").read_text()) == {"auths": {}}
    assert [p.name for p in (fresh / "cli-plugins").iterdir()] == ["docker-buildx"]
    assert (fresh / "cli-plugins/docker-buildx").resolve() == buildx


def test_low_disk_fails_before_docker_mutation(monkeypatch):
    import json
    import subprocess

    import pytest

    from scripts.ci import source_publisher_container_check as probe

    monkeypatch.setattr(probe.os, "environ", {"PATH": "/bin"})
    monkeypatch.setattr(
        probe.shutil, "disk_usage", lambda _: type("Disk", (), {"free": probe.MIN_HOST_FREE_BYTES - 1})()
    )
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, json.dumps([{"Endpoints": {"docker": {"Host": "unix:///tmp/docker.sock"}}}]), ""
        )

    monkeypatch.setattr(probe, "run", fake_run)
    with pytest.raises(RuntimeError, match="less than 8 GiB"):
        probe.main()
    assert calls == [["docker", "context", "inspect"]]


def test_missing_buildx_refuses_build(monkeypatch):
    import subprocess

    import pytest

    from scripts.ci import source_publisher_container_check as probe

    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, int(args[1:3] == ["buildx", "version"]), "", "")

    monkeypatch.setattr(probe, "run", fake_run)
    with pytest.raises(RuntimeError, match="Buildx plugin unavailable"):
        probe.run_probe({"PATH": "/bin"})
    assert calls == [["docker", "info", "--format", "{{.ServerVersion}}"], ["docker", "buildx", "version"]]


def test_volume_cleanup_requires_matching_label_and_handles_absence(monkeypatch):
    import subprocess

    from scripts.ci import source_publisher_container_check as probe

    calls = []
    responses = iter([(0, "other", ""), (1, "", "Error: No such volume: missing"), (0, "token", ""), (0, "", "")])

    def fake_run(args, **_kwargs):
        calls.append(args)
        code, stdout, stderr = next(responses)
        return subprocess.CompletedProcess(args, code, stdout, stderr)

    monkeypatch.setattr(probe, "run", fake_run)
    assert not probe.cleanup_resource("volume", "foreign", "token", {})
    assert probe.cleanup_resource("volume", "missing", "token", {})
    assert probe.cleanup_resource("volume", "owned", "token", {})
    assert all(".Labels" in call[4] and ".Config.Labels" not in call[4] for call in calls if "inspect" in call)
    assert calls[-1] == ["docker", "volume", "rm", "owned"]


def test_probe_stages_fixture_without_starting_initializer(monkeypatch):
    import subprocess

    from scripts.ci import source_publisher_container_check as probe

    calls = []
    monkeypatch.setattr(probe, "copy_context", lambda _: None)

    def fake_run(args, **_kwargs):
        calls.append(args)
        stdout = ""
        if args[1:3] == ["image", "inspect"]:
            stdout = "image-id" if "{{.Id}}" in args else token[0]
        elif "inspect" in args:
            stdout = token[0]
        elif args[1:3] == ["volume", "create"]:
            token.append(args[4].split("=", 1)[1])
        elif args[1:3] == ["buildx", "build"]:
            token.append(args[5].split("=", 1)[1])
        elif args[1] == "run":
            if "-c" in args:
                stdout = "synthetic config and mount checks passed"
            else:
                return subprocess.CompletedProcess(args, 1, "", "did not confirm a database commit")
        return subprocess.CompletedProcess(args, 0, stdout, "")

    token = []
    monkeypatch.setattr(probe, "run", fake_run)
    assert probe.run_probe({"PATH": "/bin"}) == 0
    create = next(c for c in calls if c[1] == "create")
    cp = next(c for c in calls if c[1] == "cp")
    assert "--network" in create and create[create.index("--network") + 1] == "none"
    assert "image-id" in create
    assert "-a" not in cp and "--archive" not in cp
    init_name = create[create.index("--name") + 1]
    assert cp[-1].startswith(init_name + ":")
    assert not any(c[1] == "start" for c in calls)
    runs = [c for c in calls if c[1] == "run"]
    assert all("--user" in c and c[c.index("--user") + 1] == "10001:10001" for c in runs)
    assert all(any("type=volume," in arg and "dst=/publisher-bin,readonly" in arg for arg in c) for c in runs)
    assert "assert command_uid != os.geteuid()" in probe.PROBE
    assert "finalizer uid={command_uid} publisher uid={os.geteuid()}" in probe.PROBE


def test_copy_failure_cleans_owned_resources_in_order(monkeypatch):
    import subprocess

    import pytest

    from scripts.ci import source_publisher_container_check as probe

    calls = []
    token = []
    monkeypatch.setattr(probe, "copy_context", lambda _: None)

    def fake_run(args, **_kwargs):
        calls.append(args)
        if args[1:3] == ["buildx", "build"]:
            token.append(args[5].split("=", 1)[1])
        if args[1] == "cp":
            raise subprocess.CalledProcessError(1, args)
        stdout = ""
        if args[1:3] == ["image", "inspect"] and "{{.Id}}" in args:
            stdout = "image-id"
        elif "inspect" in args:
            stdout = token[0]
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(probe, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        probe.run_probe({"PATH": "/bin"})
    removals = [c for c in calls if "rm" in c]
    assert removals[0][-1].endswith("-init")
    assert removals[1][1] == "rm"
    assert removals[2][1:3] == ["image", "rm"]
    assert removals[3][1:3] == ["volume", "rm"]
    assert not any(c[1] == "run" for c in calls)
