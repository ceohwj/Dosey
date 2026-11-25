#!/usr/bin/env python3
"""Synthetic, local-only publisher image probe. Builds an image; never contacts a Source or DB.

Run: python scripts/ci/source_publisher_container_check.py
Docker build may contact image/package registries. This is not a publication readiness test.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LABEL = "org.dosey.synthetic-publisher-probe"
CONTEXT_PATHS = (
    "pyproject.toml",
    "uv.lock",
    "apps/ai_worker",
    "packages/provider_contracts",
    "packages/ocr_runtime",
    "packages/provider_runtime",
    "packages/rag_runtime",
    "infra/python/protected_retrieval_role_policy.py",
    "infra/python/catalog_approval_role_policy.py",
    "infra/python/catalog_role_policy.py",
    "infra/python/source_role_policy.py",
)
SENTINEL = "synthetic-publisher-probe-secret"
MIN_HOST_FREE_BYTES = 8 * 1024**3
PROBE = r"""
import hashlib
import os
from pathlib import Path
print("probe: python started", flush=True)
from ai_worker.tasks.rag.source_ingestion.artifacts import RawArtifactMetadata
from ai_worker.admin.source_publisher import SourcePublisherConfig
from ai_worker.adapters.local_private_source_artifact_finalizer import FinalizingLocalPrivateSourceArtifactStore
print("probe: imports completed", flush=True)
assert os.getuid() == 10001 and os.getgid() == 10001
try:
    SourcePublisherConfig.from_environment(os.environ)
except ValueError as exc:
    assert str(exc) == "Source Publisher requires an isolated credential environment"
else:
    raise AssertionError("co-injected credential accepted")
import sys
from unittest.mock import patch
from ai_worker.admin import mfds_product_ingestion as cli_module
original_config = cli_module.MfdsProductIngestionConfig.from_environment
seen = []
def checked_config(env):
    try:
        return original_config(env)
    except ValueError as exc:
        seen.append(str(exc))
        raise
with patch.object(cli_module.MfdsProductIngestionConfig, "from_environment", side_effect=checked_config), \
     patch.object(cli_module, "run_ingestion") as ingestion, \
     patch.object(sys, "argv", ["probe", "--receipt-path", "/publisher-input/receipt.json", "--repository-root", "/publisher-input"]):
    assert cli_module.main() == 1
    assert seen == ["Source Publisher requires an isolated credential environment"]
    ingestion.assert_not_called()
print("probe: credential rejection verified", flush=True)
for root in ("/publisher-input", "/artifacts", "/publisher-bin"):
    p = Path(root) / "probe"
    try:
        p.write_text("probe")
    except OSError:
        pass
    else:
        raise AssertionError("read-only mount writable: " + root)
for root in ("/publisher-journal", "/publisher-spool"):
    p = Path(root) / "probe"
    p.write_text("probe")
    p.unlink()
print("probe: mount checks completed", flush=True)
command = Path("/publisher-bin/source-artifact-finalizer")
command_uid = command.stat().st_uid
print(f"probe: finalizer uid={command_uid} publisher uid={os.geteuid()}", flush=True)
assert command_uid != os.geteuid()
FinalizingLocalPrivateSourceArtifactStore._validate_finalizer_command(command)
for action in (lambda: command.write_text("x"), lambda: command.unlink()):
    try:
        action()
    except OSError:
        pass
    else:
        raise AssertionError("command mutable")
unsafe = Path("/publisher-spool/client")
unsafe.write_text("synthetic")
unsafe.chmod(0o755)
try:
    FinalizingLocalPrivateSourceArtifactStore._validate_finalizer_command(unsafe)
except ValueError:
    pass
else:
    raise AssertionError("unsafe client accepted")
print("probe: finalizer command validation completed", flush=True)
data = b"synthetic"
source = Path("/publisher-spool/source")
source.write_bytes(data)
checksum = hashlib.sha256(data).hexdigest()
class Reader:
    def read_verified(self, **kwargs):
        return data
store = FinalizingLocalPrivateSourceArtifactStore(finalizer_command=command, reader=Reader())
store.put_verified(page_number=1, file_path=source, metadata=RawArtifactMetadata(
    artifact_key="synthetic", raw_checksum=checksum, byte_size=len(data), content_type="text/plain"))
print("synthetic config and mount checks passed")
"""


def local_endpoint(endpoint: str) -> bool:
    return endpoint.startswith("unix:///") and not any(c in endpoint for c in "\r\n")


def copy_context(destination: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in {".git", ".env", "__pycache__", ".venv", "models"}
            or name.startswith(".env")
            or name.endswith((".env", ".pyc"))
        }

    for relative in CONTEXT_PATHS:
        source = ROOT / relative
        if source.is_symlink():
            raise ValueError(f"symlink in build context: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            for path in source.rglob("*"):
                if path.is_symlink():
                    raise ValueError(f"symlink in build context: {path.relative_to(ROOT)}")
            shutil.copytree(source, target, ignore=ignore)
        else:
            shutil.copy2(source, target)
    shutil.copy2(ROOT / "apps/ai_worker/Dockerfile", destination / "Dockerfile")


def run(
    args: list[str],
    *,
    env: dict[str, str],
    check: bool = True,
    timeout: int = 15,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, env=env, text=True, capture_output=True, check=check, timeout=timeout)


def save_output(directory: Path, stage: str, stdout: str | bytes | None, stderr: str | bytes | None) -> None:
    for name, value in (("stdout", stdout), ("stderr", stderr)):
        output = value.decode("utf-8", "replace") if isinstance(value, bytes) else (value or "")
        output = output.replace(SENTINEL, "[REDACTED]")
        output = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[REDACTED]@", output)
        lines = [
            "[REDACTED sensitive diagnostic line]"
            if re.search(r"(?i)(authorization|bearer|password|credential|api[_-]?key|access[_-]?token)", line)
            else line
            for line in output.splitlines()
        ]
        path = directory / f"{stage}.{name}.log"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n".join(lines)[-1_000_000:])


def run_logged(
    args: list[str], *, env: dict[str, str], timeout: int, stage: str, evidence: Path
) -> subprocess.CompletedProcess[str]:
    print(f"Stage: {stage}", flush=True)
    try:
        result = run(args, env=env, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        save_output(evidence, stage, exc.stdout, exc.stderr)
        raise
    save_output(evidence, stage, result.stdout, result.stderr)
    return result


def cleanup_resource(kind: str, identifier: str, token: str, env: dict[str, str]) -> bool:
    prefix = ["docker", kind] if kind in {"image", "volume"} else ["docker"]
    labels = ".Labels" if kind == "volume" else ".Config.Labels"
    try:
        result = run(
            [*prefix, "inspect", "--format", f'{{{{index {labels} "{LABEL}"}}}}', identifier],
            env=env,
            check=False,
            timeout=15,
        )
        if result.returncode:
            return any(
                message in result.stderr.lower()
                for message in ("no such object", "no such container", "no such image", "no such volume")
            )
        if result.stdout.strip() != token:
            return False
        command = [*prefix, "rm"] + (["-f"] if kind == "container" else []) + [identifier]
        return run(command, env=env, check=False, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def main() -> int:
    if any(key.startswith("DOCKER_") for key in os.environ):
        raise RuntimeError("DOCKER_* overrides are forbidden")
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
    print("Stage: docker-preflight", flush=True)
    context = run(["docker", "context", "inspect"], env=env)
    endpoint = json.loads(context.stdout)[0]["Endpoints"]["docker"]["Host"]
    if not local_endpoint(endpoint):
        raise RuntimeError("Docker context must use a local Unix socket")
    # Fixture admission only: Docker's separate disk or VM may have less free space.
    if shutil.disk_usage(tempfile.gettempdir()).free < MIN_HOST_FREE_BYTES:
        raise RuntimeError("host disk has less than 8 GiB free for the test fixture")
    docker_binary = shutil.which("docker", path=env.get("PATH"))
    if not docker_binary:
        raise RuntimeError("docker executable not found")
    with tempfile.TemporaryDirectory(prefix="dosey-docker-config-") as config_dir:
        isolated = isolated_docker_env(env, endpoint, Path(config_dir), docker_binary)
        return run_probe(isolated)


def isolated_docker_env(env: dict[str, str], endpoint: str, directory: Path, docker_binary: str) -> dict[str, str]:
    if not local_endpoint(endpoint):
        raise ValueError("Docker context must use a local Unix socket")
    config = directory / "config.json"
    fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({"auths": {}}, stream)
    bin_dir = directory / "bin"
    bin_dir.mkdir(mode=0o700)
    (bin_dir / "docker").symlink_to(docker_binary)
    installed = Path(docker_binary).resolve()
    if installed.parts[-4:] == ("Contents", "Resources", "bin", "docker"):
        resources = installed.parent.parent
        buildx = (resources / "cli-plugins/docker-buildx").resolve()
        if buildx.is_relative_to(resources) and buildx.is_file() and os.access(buildx, os.X_OK):
            plugins = directory / "cli-plugins"
            plugins.mkdir(mode=0o700)
            (plugins / "docker-buildx").symlink_to(buildx)
    return {**env, "PATH": str(bin_dir), "DOCKER_CONFIG": str(directory), "DOCKER_HOST": endpoint}


def run_probe(env: dict[str, str]) -> int:  # noqa: C901
    run(["docker", "info", "--format", "{{.ServerVersion}}"], env=env, timeout=15)
    buildx = run(["docker", "buildx", "version"], env=env, check=False, timeout=15)
    if buildx.returncode:
        raise RuntimeError("Buildx plugin unavailable in isolated Docker environment")
    evidence = Path(tempfile.mkdtemp(prefix="dosey-publisher-evidence-"))
    evidence.chmod(0o700)
    print(f"Private evidence: {evidence}", flush=True)
    token = uuid.uuid4().hex
    tag = f"dosey-publisher-probe:{token}"
    name = f"dosey-publisher-probe-{token}"
    init_name = f"{name}-init"
    volume = f"{name}-bin"
    build_attempted = False
    build_succeeded = False

    def interrupted(_signum: int, _frame: object) -> None:
        raise RuntimeError("interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        with tempfile.TemporaryDirectory(prefix="dosey-publisher-probe-") as tmp:
            base = Path(tmp)
            base.chmod(0o755)
            context_dir = base / "context"
            context_dir.mkdir()
            print("Stage: build-context", flush=True)
            copy_context(context_dir)
            for directory in ("input", "artifacts", "bin", "journal", "spool"):
                (base / directory).mkdir(mode=0o777 if directory in {"journal", "spool"} else 0o755)
                (base / directory).chmod(0o777 if directory in {"journal", "spool"} else 0o755)
            client = base / "bin/source-artifact-finalizer"
            client.write_text("""#!/usr/local/bin/python3
import json, os, sys
if open('/proc/self/environ', 'rb').read():
    sys.exit(1)
checksum = sys.argv[sys.argv.index('--checksum') + 1]
print(json.dumps({'schema': 'source-artifact-finalize@1', 'storage_backend': 'LOCAL_PRIVATE', 'object_key': 'sha256/' + checksum[:2] + '/' + checksum + '.artifact'}))
""")
            client.chmod(0o755)
            build_attempted = True
            build = run_logged(
                ["docker", "buildx", "build", "--load", "--label", f"{LABEL}={token}", "-t", tag, str(context_dir)],
                env=env,
                timeout=1800,
                stage="build",
                evidence=evidence,
            )
            if build.returncode:
                raise RuntimeError("image build failed (output suppressed)")
            build_succeeded = True
            print("Stage: image-ownership", flush=True)
            image_id = run(["docker", "image", "inspect", "--format", "{{.Id}}", tag], env=env).stdout.strip()
            label = run(
                ["docker", "image", "inspect", "--format", f'{{{{index .Config.Labels "{LABEL}"}}}}', image_id], env=env
            ).stdout.strip()
            if label != token:
                raise RuntimeError("image ownership label mismatch")
            run(["docker", "volume", "create", "--label", f"{LABEL}={token}", volume], env=env)
            run(
                [
                    "docker",
                    "create",
                    "--name",
                    init_name,
                    "--label",
                    f"{LABEL}={token}",
                    "--network",
                    "none",
                    "--mount",
                    f"type=volume,src={volume},dst=/publisher-bin",
                    image_id,
                ],
                env=env,
            )
            run(["docker", "cp", str(client), f"{init_name}:/publisher-bin/source-artifact-finalizer"], env=env)
            if not cleanup_resource("container", init_name, token, env):
                raise RuntimeError("initializer cleanup failed")
            mounts = ["--mount", f"type=volume,src={volume},dst=/publisher-bin,readonly"]
            for src, dst, ro in (
                ("input", "/publisher-input", True),
                ("artifacts", "/artifacts", True),
                ("journal", "/publisher-journal", False),
                ("spool", "/publisher-spool", False),
            ):
                mounts += ["--mount", f"type=bind,src={base / src},dst={dst}" + (",readonly" if ro else "")]
            common = [
                "docker",
                "run",
                "--name",
                name,
                "--label",
                f"{LABEL}={token}",
                "--network",
                "none",
                "--user",
                "10001:10001",
                *mounts,
                "-e",
                "UV_CACHE_DIR=/publisher-spool/uv-cache",
                "-e",
                f"SOURCE_PUBLISHER_PASSWORD={SENTINEL}",
                "-e",
                f"SOURCE_WRITER_PASSWORD={SENTINEL}",
            ]
            result = run_logged(
                [*common, image_id, "uv", "run", "--no-sync", "python", "-c", PROBE],
                env=env,
                timeout=60,
                stage="container-probe",
                evidence=evidence,
            )
            if (
                result.returncode
                or "synthetic config and mount checks passed" not in result.stdout
                or SENTINEL in result.stdout + result.stderr
            ):
                raise RuntimeError("container probe failed (output suppressed)")
            print("PASS: actual image UID, imports, credential rejection and mount checks")
            print("Stage: container-ownership", flush=True)
            owned = run(["docker", "inspect", "--format", f'{{{{index .Config.Labels "{LABEL}"}}}}', name], env=env)
            if owned.stdout.strip() != token:
                raise RuntimeError("container ownership label mismatch")
            run(["docker", "rm", name], env=env)
            cli = run_logged(
                [
                    *common,
                    image_id,
                    "uv",
                    "run",
                    "--no-sync",
                    "python",
                    "-m",
                    "ai_worker.admin.mfds_product_ingestion",
                    "--receipt-path",
                    "/publisher-input/receipt.json",
                    "--repository-root",
                    "/publisher-input",
                ],
                env=env,
                timeout=60,
                stage="publisher-cli",
                evidence=evidence,
            )
            if (
                cli.returncode != 1
                or "did not confirm a database commit" not in cli.stderr
                or SENTINEL in cli.stdout + cli.stderr
            ):
                raise RuntimeError("publisher CLI did not fail as expected (output suppressed)")
            print("PASS: actual publisher CLI returned generic failure without sentinel output")
    finally:
        prior_error = sys.exc_info()[0] is not None
        init_clean = cleanup_resource("container", init_name, token, env)
        container_clean = cleanup_resource("container", name, token, env)
        image_clean = not build_attempted or cleanup_resource("image", tag, token, env)
        volume_clean = cleanup_resource("volume", volume, token, env)
        if build_attempted and not build_succeeded:
            print("Build cache and intermediate resource state is unknown", file=sys.stderr)
        if not init_clean or not container_clean or not image_clean or not volume_clean:
            print("FAIL: owned resource cleanup incomplete or could not be verified", file=sys.stderr)
            if not prior_error:
                raise RuntimeError("cleanup failed")
        else:
            print("Named containers, image and volume cleanup completed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError) as exc:
        print(f"Probe failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
