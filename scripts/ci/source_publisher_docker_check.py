#!/usr/bin/env python3
"""Run the three Source publisher integration checks on an owned local cluster."""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "infra/docker/docker-compose.source-publisher-test.yml"
IMAGE = "pgvector/pgvector:0.8.6-pg17-bookworm"
NODES = (
    "tests/integration/rag/test_database_role_provisioning.py::test_bootstrap_then_provision_and_redeploy_do_not_reopen_permissions",
    "tests/integration/rag/test_database_role_provisioning.py::test_catalog_approval_bootstrap_login_provisions_and_validates",
    "tests/integration/rag/test_source_management.py::test_management_bootstrap_is_opt_in_and_redeploy_does_not_expand_rights",
)

ROLE_NODES = (
    "tests/integration/rag/test_source_publisher_roles.py::test_publisher_complete_graph_stale_grants_and_repeat_policy",
    "tests/integration/rag/test_source_publisher_roles.py::test_publisher_rejects_membership_admin_owner_and_global_defaults",
    "tests/integration/rag/test_source_publisher_roles.py::test_t1_commit_survives_t2_rollback_and_pre_t1_failure_preserves_old_state",
    "tests/integration/rag/test_source_writer_roles.py::test_separate_credentials_and_future_tables_are_fail_closed",
)


def selected_nodes(include_role_checks):
    return NODES + ROLE_NODES if include_role_checks else NODES


def safe_environment(source):
    forbidden = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")
    if any(source.get(name) for name in forbidden):
        raise ValueError("Explicit Docker endpoint or context environment is forbidden")
    return {key: source[key] for key in ("PATH", "HOME", "DOCKER_CONFIG", "SYSTEMROOT", "TMPDIR") if key in source}


def run(args, *, env, cwd, input=None, timeout=60, check=True):
    result = subprocess.run(args, env=env, cwd=cwd, input=input, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"Command failed: {args[0]} (exit {result.returncode})")
    return result


def verify_context(env, cwd):
    name = run(["docker", "context", "show"], env=env, cwd=cwd).stdout.strip()
    data = json.loads(run(["docker", "context", "inspect", name], env=env, cwd=cwd).stdout)
    host = data[0]["Endpoints"]["docker"]["Host"]
    if not (host.startswith("unix:///") or host.startswith("npipe://")):
        raise ValueError("Docker context is not local")
    return name


def verify_container(info, project):
    labels = info["Config"].get("Labels") or {}
    ports = info["NetworkSettings"]["Ports"].get("5432/tcp") or []
    mounts = info.get("Mounts") or []
    tmpfs = info["HostConfig"].get("Tmpfs") or {}
    if (
        labels.get("com.docker.compose.project") != project
        or labels.get("com.docker.compose.service") != "postgres"
        or info["Config"]["Image"] != IMAGE
        or any(
            mount.get("Type") != "tmpfs" or mount.get("Destination") != "/var/lib/postgresql/data" for mount in mounts
        )
        or "/var/lib/postgresql/data" not in tmpfs
        or len(ports) != 1
        or ports[0].get("HostIp") != "127.0.0.1"
    ):
        raise ValueError("Container ownership, image, storage or port mismatch")
    port = int(ports[0]["HostPort"])
    if not 1 <= port <= 65535:
        raise ValueError("Invalid mapped port")
    return port


def junit_pass(path):
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    return len(cases) == 1 and not any(cases[0].find(tag) is not None for tag in ("failure", "error", "skipped"))


def write_node_log(path, stdout, stderr, password):
    output = f"stdout:\n{stdout}\nstderr:\n{stderr}"
    output = output.replace(password, "[REDACTED]")
    output = re.sub(r"synthetic-[A-Za-z0-9_-]+", "[REDACTED]", output)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(output)


def record_manifest(path, event):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as file:
        file.write(json.dumps(event, sort_keys=True) + "\n")
        file.flush()
        os.fsync(file.fileno())


def main(argv=None):  # noqa: C901 - keep resource lifetime and cleanup together
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, help="Existing Python interpreter with test dependencies")
    parser.add_argument("--publisher-role-checks", action="store_true", help="Also run four publisher role checks")
    args = parser.parse_args(argv)
    python = Path(args.python).absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        parser.error("--python must name an existing executable")
    project = "sourcepublisher" + uuid.uuid4().hex
    password = "synthetic-" + uuid.uuid4().hex
    evidence = Path(tempfile.mkdtemp(prefix="source-publisher-evidence-"))
    manifest = evidence / "manifest.jsonl"
    status = 1
    cleanup_error = False
    container = None
    previous = {}

    def interrupted(signum, _frame):
        raise KeyboardInterrupt(signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, interrupted)
    try:
        with tempfile.TemporaryDirectory(prefix="source-publisher-work-") as work:
            cwd = Path(work)
            env = safe_environment(os.environ)
            env["COMPOSE_DISABLE_ENV_FILE"] = "1"
            envfile = cwd / "synthetic.env"
            envfile.write_text(f"SYNTHETIC_POSTGRES_PASSWORD={password}\n")
            envfile.chmod(0o600)
            compose = ["docker", "compose", "-p", project, "-f", str(COMPOSE), "--env-file", str(envfile)]
            print(f"Evidence directory: {evidence}", flush=True)
            record_manifest(manifest, {"project": project, "event": "initialized"})
            if shutil.disk_usage(cwd).free < 512 * 1024 * 1024:
                print("Host disk has less than 512 MiB free; Compose was not started", file=sys.stderr)
                raise RuntimeError("Host disk admission failed")
            run([str(python), "-c", "import asyncpg, pytest, pytest_asyncio.plugin"], env=env, cwd=cwd)
            print(f"Docker context: {verify_context(env, cwd)}", flush=True)
            run(["docker", "info", "--format", "{{.ID}}"], env=env, cwd=cwd, timeout=15)
            for command in (["docker", "--version"], ["docker", "compose", "version"], [str(python), "--version"]):
                print(run(command, env=env, cwd=cwd).stdout.strip(), flush=True)
            record_manifest(manifest, {"project": project, "event": "compose_up_attempted"})
            try:
                run(compose + ["up", "-d"], env=env, cwd=cwd, timeout=180)
                container = run(compose + ["ps", "-q", "postgres"], env=env, cwd=cwd).stdout.strip()
                if not re.fullmatch(r"[0-9a-f]{64}", container):
                    raise ValueError("Expected exactly one owned container ID")
                record_manifest(manifest, {"event": "container_discovered", "id": container})
                info = json.loads(run(["docker", "inspect", container], env=env, cwd=cwd).stdout)[0]
                port = verify_container(info, project)
                print(
                    f"Owned container {container[:12]}, project {project}, loopback port {port}, image ID {info['Image']}"
                )
                testenv = {
                    **env,
                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                    "PYTHONPATH": f"{ROOT / 'backend'}{os.pathsep}{ROOT}",
                    "DB_HOST": "127.0.0.1",
                    "DB_PORT": str(port),
                    "DB_EXPOSE_PORT": str(port),
                    "DB_USER": "postgres",
                    "DB_PASSWORD": password,
                    "DB_NAME": "test",
                    "ISSUE398_TEST_POSTGRES_CONTAINER": container,
                }
                print("Test environment keys: " + ", ".join(sorted(testenv)), flush=True)
                marker = uuid.uuid4().hex
                sql = f"CREATE TABLE harness_cluster_identity (marker text); INSERT INTO harness_cluster_identity VALUES ('{marker}');"
                for _attempt in range(30):
                    ready = run(
                        [
                            "docker",
                            "exec",
                            "-i",
                            container,
                            "psql",
                            "-X",
                            "-v",
                            "ON_ERROR_STOP=1",
                            "-U",
                            "postgres",
                            "-d",
                            "test",
                        ],
                        env=env,
                        cwd=cwd,
                        input=sql,
                        check=False,
                    )
                    if ready.returncode == 0:
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError("Disposable PostgreSQL did not become ready")
                probe = (
                    "import asyncio, asyncpg, os\n"
                    "async def main():\n"
                    " c = await asyncpg.connect(host=os.environ['DB_HOST'], port=int(os.environ['DB_PORT']), user=os.environ['DB_USER'], password=os.environ['DB_PASSWORD'], database='test')\n"
                    " print(await c.fetchval('SELECT marker FROM harness_cluster_identity'))\n"
                    " await c.close()\n"
                    "asyncio.run(main())\n"
                )
                if run([str(python), "-c", probe], env=testenv, cwd=cwd).stdout.strip() != marker:
                    raise ValueError("Host endpoint and container cluster differ")
                print("Host endpoint and docker exec cluster identity match", flush=True)
                status = 0
                for index, node in enumerate(selected_nodes(args.publisher_role_checks), 1):
                    label = f"test-{index}" if index <= len(NODES) else f"role-{index - len(NODES)}"
                    report = evidence / f"{label}.xml"
                    result = run(
                        [
                            str(python),
                            "-m",
                            "pytest",
                            "-c",
                            str(ROOT / "pyproject.toml"),
                            str(ROOT / node),
                            "-p",
                            "pytest_asyncio.plugin",
                            "-o",
                            "addopts=",
                            f"--junitxml={report}",
                        ],
                        env=testenv,
                        cwd=cwd,
                        timeout=900,
                        check=False,
                    )
                    log_label = f"node-{index}" if index <= len(NODES) else label
                    write_node_log(evidence / f"{log_label}.log", result.stdout, result.stderr, password)
                    passed = result.returncode == 0 and report.exists() and junit_pass(report)
                    if report.exists():
                        tree = ET.parse(report)
                        for element in tree.iter():
                            if element.tag in {"failure", "error", "system-out", "system-err"}:
                                element.text = "[details withheld]"
                                element.attrib.clear()
                            else:
                                element.text = (element.text or "").replace(password, "[REDACTED]")
                                element.attrib = {
                                    key: value.replace(password, "[REDACTED]") for key, value in element.attrib.items()
                                }
                        tree.write(report, encoding="unicode")
                    print(f"{label}: {'PASS' if passed else 'NOT PASS'} (exit {result.returncode})", flush=True)
                    if not passed and status == 0:
                        status = result.returncode or 1
            finally:
                # Query only this fresh project; remove only containers whose labels prove ownership.
                ids = {container} if container else set()
                try:
                    found = run(compose + ["ps", "-aq"], env=env, cwd=cwd, check=False)
                    ids.update(found.stdout.split())
                    if found.returncode:
                        cleanup_error = True
                except (OSError, subprocess.SubprocessError):
                    cleanup_error = True
                for candidate in ids:
                    try:
                        record_manifest(manifest, {"event": "container_cleanup_candidate", "id": candidate})
                    except OSError:
                        cleanup_error = True
                    try:
                        if not re.fullmatch(r"[0-9a-f]{64}", candidate):
                            raise ValueError("Invalid cleanup ID")
                        item = json.loads(run(["docker", "inspect", candidate], env=env, cwd=cwd).stdout)[0]
                        if (item["Config"].get("Labels") or {}).get("com.docker.compose.project") != project:
                            raise ValueError("Cleanup ownership mismatch")
                        run(["docker", "rm", "-f", candidate], env=env, cwd=cwd)
                    except (ValueError, RuntimeError, KeyError, subprocess.SubprocessError):
                        cleanup_error = True
                try:
                    networks = run(
                        ["docker", "network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"],
                        env=env,
                        cwd=cwd,
                    )
                    for network in networks.stdout.split():
                        try:
                            record_manifest(manifest, {"event": "network_cleanup_candidate", "id": network})
                        except OSError:
                            cleanup_error = True
                        if not re.fullmatch(r"[0-9a-f]{12,64}", network):
                            raise ValueError("Invalid network ID")
                        item = json.loads(run(["docker", "network", "inspect", network], env=env, cwd=cwd).stdout)[0]
                        if (item.get("Labels") or {}).get("com.docker.compose.project") != project:
                            raise ValueError("Network ownership mismatch")
                        run(["docker", "network", "rm", network], env=env, cwd=cwd)
                except (ValueError, RuntimeError, KeyError, subprocess.SubprocessError):
                    cleanup_error = True
                print(f"Owned container cleanup: {'FAILED' if cleanup_error else 'complete'}", flush=True)
    except KeyboardInterrupt as exc:
        status = 128 + int(exc.args[0]) if exc.args else 130
    except (ValueError, RuntimeError, KeyError, OSError, subprocess.SubprocessError, ET.ParseError) as exc:
        status = status or 1
        print(f"Harness failed: {type(exc).__name__}", file=sys.stderr)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return status if status else int(cleanup_error)


if __name__ == "__main__":
    sys.exit(main())
