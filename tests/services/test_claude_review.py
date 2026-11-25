"""Synthetic, credential-free checks for the external review boundary."""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "claude_review.py"


def run(tmp_path, *args, env=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=tmp_path, text=True, capture_output=True, env=env)


def setup(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    for name in ("AGENTS.md", "SECURITY.md", "docs/privacy-safety.md"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic guidance\n")
    (tmp_path / "source.py").write_text("original\n")
    (tmp_path / ".gitignore").write_text("outputs/\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "initial"], check=True)


def test_dry_run_added_untracked_deleted_and_scope(tmp_path):
    setup(tmp_path)
    (tmp_path / "source.py").unlink()
    (tmp_path / "new.py").write_text("new\n")
    result = run(tmp_path, "--working-tree", "--file", "source.py", "--file", "new.py", "--dry-run")
    assert result.returncode == 0, result.stderr
    info = json.loads(result.stdout)
    packet = json.loads((Path(info["output"]) / "packet.json").read_text())
    assert {entry["file"] for entry in packet["files"]} == {"source.py", "new.py"}
    assert "-original" in next(e for e in packet["files"] if e["file"] == "source.py")["diff"]
    assert all("final_source" not in e for e in packet["files"])
    assert "synthetic guidance" in packet["context"]["AGENTS.md"]
    assert "claude" not in result.stderr.lower()


def test_rejects_sensitive_and_escape_paths(tmp_path):
    setup(tmp_path)
    for name in ("../outside", ".env", "secrets/token.txt", "outputs/result.txt", "-evil", "source.py\nother"):
        result = run(tmp_path, "--working-tree", "--file", name, "--dry-run")
        assert result.returncode != 0, name
    (tmp_path / "link.py").symlink_to(tmp_path / "source.py")
    assert run(tmp_path, "--working-tree", "--file", "link.py", "--dry-run").returncode != 0


def test_valid_fake_cli_and_disabled_tools(tmp_path):
    setup(tmp_path)
    (tmp_path / "source.py").write_text("changed\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cli = bindir / "claude"
    cli.write_text(
        "#!/usr/bin/env python3\nimport json,sys\nif '--version' in sys.argv: print('fake 1.0'); sys.exit(0)\na=sys.argv\nassert a[a.index('--tools')+1]==''\nassert '--strict-mcp-config' in a and '--disable-slash-commands' in a and '--no-session-persistence' in a\nassert '--setting-sources' in a and a[a.index('--setting-sources')+1]==''\nassert '--permission-mode' in a and a[a.index('--permission-mode')+1]=='plan'\nassert '--safe-mode' in a\npacket=sys.stdin.read()\nassert 'changed' in packet\nprint(json.dumps({'structured_output':{'summary':'Checked','findings':[{'severity':'MEDIUM','file':'source.py','line':1,'message':'Issue','recommendation':'Fix'}]}}))\n"
    )
    cli.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    result = run(tmp_path, "--working-tree", "--file", "source.py", env=env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["findings"] == 1


def test_cli_failure_and_malformed_output(tmp_path):
    setup(tmp_path)
    (tmp_path / "source.py").write_text("changed\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cli = bindir / "claude"
    cli.write_text("#!/bin/sh\nexit 2\n")
    cli.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    assert run(tmp_path, "--working-tree", "--file", "source.py", env=env).returncode == 1
    cli.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then printf 'fake 1.0\\n'; exit 0; fi\nprintf '{}\\n'\n")
    assert run(tmp_path, "--working-tree", "--file", "source.py", env=env).returncode == 1


def test_timeout_and_stale_working_tree(tmp_path):
    setup(tmp_path)
    (tmp_path / "source.py").write_text("changed\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cli = bindir / "claude"
    cli.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then printf \'fake 1.0\\n\'; exit 0; fi\nsleep 3\n')
    cli.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    assert run(tmp_path, "--working-tree", "--file", "source.py", "--timeout", "1", env=env).returncode == 1
    cli.write_text(
        "#!/usr/bin/env python3\nimport json, pathlib, sys\nif '--version' in sys.argv: print('fake 1.0'); sys.exit(0)\npathlib.Path('"
        + str(tmp_path / "source.py")
        + "').write_text('changed again\\n')\nprint(json.dumps({'structured_output':{'summary':'Checked','findings':[]}}))\n"
    )
    assert "stale" in run(tmp_path, "--working-tree", "--file", "source.py", env=env).stderr


def test_fixed_commit_comparison(tmp_path):
    setup(tmp_path)
    (tmp_path / "source.py").write_text("committed\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "source.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "second"], check=True)
    result = run(tmp_path, "--base", "HEAD~1", "--head", "HEAD", "--file", "source.py", "--dry-run")
    assert result.returncode == 0, result.stderr
    packet = json.loads((Path(json.loads(result.stdout)["output"]) / "packet.json").read_text())
    assert "+committed" in packet["files"][0]["diff"]


def test_missing_cli_and_error_envelope(tmp_path):
    setup(tmp_path)
    (tmp_path / "source.py").write_text("changed\n")
    missing = run(
        tmp_path,
        "--working-tree",
        "--file",
        "source.py",
        env={**os.environ, "PATH": "/usr/bin:/bin"},
    )
    assert missing.returncode == 1
    assert "unavailable" in missing.stderr
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cli = bindir / "claude"
    cli.write_text(
        "#!/usr/bin/env python3\nimport json,sys\nif '--version' in sys.argv: print('fake 1.0'); sys.exit(0)\nprint(json.dumps({'is_error':True,'structured_output':{'summary':'ok','findings':[]}}))\n"
    )
    cli.chmod(0o755)
    result = run(
        tmp_path,
        "--working-tree",
        "--file",
        "source.py",
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
    )
    assert result.returncode == 1
    assert "malformed" in result.stderr


def test_no_change_binary_oversize_and_domain_source(tmp_path):
    setup(tmp_path)
    assert run(tmp_path, "--working-tree", "--file", "source.py", "--dry-run").returncode == 1
    target = tmp_path / "apps/backend/app/services/prescriptions.py"
    target.parent.mkdir(parents=True)
    target.write_text("safe synthetic source\n")
    assert (
        run(
            tmp_path,
            "--working-tree",
            "--file",
            "apps/backend/app/services/prescriptions.py",
            "--dry-run",
        ).returncode
        == 0
    )
    target.write_bytes(b"binary\0payload")
    assert (
        run(
            tmp_path,
            "--working-tree",
            "--file",
            "apps/backend/app/services/prescriptions.py",
            "--dry-run",
        ).returncode
        == 1
    )
    target.write_bytes(b"a" * (128 * 1024 + 1))
    assert (
        run(
            tmp_path,
            "--working-tree",
            "--file",
            "apps/backend/app/services/prescriptions.py",
            "--dry-run",
        ).returncode
        == 1
    )


def test_context_change_marks_review_stale(tmp_path):
    setup(tmp_path)
    (tmp_path / "source.py").write_text("changed\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cli = bindir / "claude"
    cli.write_text(
        "#!/usr/bin/env python3\nimport json, pathlib, sys\nif '--version' in sys.argv: print('fake 1.0'); sys.exit(0)\n"
        + f"pathlib.Path({str(tmp_path / 'SECURITY.md')!r}).write_text('changed context\\n')\n"
        + "print(json.dumps({'structured_output':{'summary':'Checked','findings':[]}}))\n"
    )
    cli.chmod(0o755)
    result = run(
        tmp_path,
        "--working-tree",
        "--file",
        "source.py",
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
    )
    assert result.returncode == 1
    assert "stale" in result.stderr


def test_private_artifact_permissions_and_outside_output_symlink(tmp_path):
    setup(tmp_path)
    (tmp_path / "source.py").write_text("changed\n")
    result = run(tmp_path, "--working-tree", "--file", "source.py", "--dry-run")
    assert result.returncode == 0, result.stderr
    output = Path(json.loads(result.stdout)["output"])
    assert output.stat().st_mode & 0o777 == 0o700
    assert (output / "packet.json").stat().st_mode & 0o777 == 0o600
    assert (output / "metadata.json").stat().st_mode & 0o777 == 0o600

    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir()
    (tmp_path / "outside-link").symlink_to(outside, target_is_directory=True)
    result = run(
        tmp_path,
        "--working-tree",
        "--file",
        "source.py",
        "--dry-run",
        "--output-dir",
        "outside-link",
    )
    assert result.returncode == 1
    assert not list(outside.iterdir())


def test_github_template_and_gitignore_are_allowed(tmp_path):
    setup(tmp_path)
    template = tmp_path / ".github/template.md"
    template.parent.mkdir()
    template.write_text("synthetic template\n")
    (tmp_path / ".gitignore").write_text("outputs/\n")
    result = run(
        tmp_path,
        "--working-tree",
        "--file",
        ".github/template.md",
        "--file",
        ".gitignore",
        "--context-range",
        ".gitignore:1:1",
        "--dry-run",
    )
    assert result.returncode == 0, result.stderr


def test_ignored_and_private_inputs_are_rejected(tmp_path):
    setup(tmp_path)
    (tmp_path / ".gitignore").write_text("outputs/\nignored.py\n")
    (tmp_path / "ignored.py").write_text("synthetic\n")
    for name in (
        "ignored.py",
        "data/table.csv",
        "records.jsonl",
        "prod.env",
        "id_rsa",
        ".npmrc",
        "config.crt",
        "private.jks",
        "arbitrary.json",
    ):
        result = run(tmp_path, "--working-tree", "--file", name, "--dry-run")
        assert result.returncode == 1, name
    (tmp_path / "untracked.py").write_text("synthetic\n")
    assert run(tmp_path, "--working-tree", "--file", "untracked.py", "--dry-run").returncode == 0


def test_missing_context_non_utf8_and_packet_limit(tmp_path):
    setup(tmp_path)
    (tmp_path / "source.py").write_text("changed\n")
    (tmp_path / "AGENTS.md").unlink()
    assert run(tmp_path, "--working-tree", "--file", "source.py", "--dry-run").returncode == 1
    (tmp_path / "AGENTS.md").write_text("synthetic\n")
    (tmp_path / "source.py").write_bytes(b"\xff")
    assert run(tmp_path, "--working-tree", "--file", "source.py", "--dry-run").returncode == 1
    (tmp_path / "source.py").write_text("x" * 100_000)
    (tmp_path / "AGENTS.md").write_text("y" * 100_000)
    (tmp_path / "SECURITY.md").write_text("z" * 100_000)
    assert "packet exceeds" in run(tmp_path, "--working-tree", "--file", "source.py", "--dry-run").stderr


def test_report_validation_rejects_bad_fields_and_context_findings():
    from scripts.claude_review import ReviewError, validate_report

    files = {"source.py": {"after": "changed\n", "before": "original\n"}}
    valid = {"severity": "HIGH", "file": "source.py", "line": 1, "message": "Issue", "recommendation": "Fix"}
    for patch in (
        {"severity": "UNKNOWN"},
        {"file": "SECURITY.md"},
        {"line": 2},
        {"line": True},
        {"message": ""},
    ):
        finding = {**valid, **patch}
        try:
            validate_report({"summary": "Checked", "findings": [finding]}, files)
        except ReviewError:
            pass
        else:
            raise AssertionError(f"Accepted invalid finding: {patch}")


def test_timeout_kills_descendant_and_returns_promptly(tmp_path):
    import time

    setup(tmp_path)
    (tmp_path / "source.py").write_text("changed\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cli = bindir / "claude"
    cli.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo "fake 1.0"; exit 0; fi\nsleep 30 &\nwait\n')
    cli.chmod(0o755)
    start = time.monotonic()
    result = run(
        tmp_path,
        "--working-tree",
        "--file",
        "source.py",
        "--timeout",
        "1",
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
    )
    assert result.returncode == 1
    assert "timed out" in result.stderr
    assert time.monotonic() - start < 5


def test_deleted_intermediate_directory_and_git_tree_rejected(tmp_path):
    import shutil

    setup(tmp_path)
    deleted = tmp_path / "old/file.py"
    deleted.parent.mkdir()
    deleted.write_text("original\n")
    tree = tmp_path / "folder.md/child.py"
    tree.parent.mkdir()
    tree.write_text("original\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "old", "folder.md"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "more files"], check=True)
    shutil.rmtree(deleted.parent)
    shutil.rmtree(tree.parent)
    result = run(tmp_path, "--working-tree", "--file", "old/file.py", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert run(tmp_path, "--working-tree", "--file", "folder.md", "--dry-run").returncode == 1


def test_relative_output_dir_uses_repository_root(tmp_path):
    setup(tmp_path)
    (tmp_path / "source.py").write_text("changed\n")
    result = run(
        tmp_path / "docs",
        "--working-tree",
        "--file",
        "source.py",
        "--dry-run",
        "--output-dir",
        "outputs/custom",
    )
    assert result.returncode == 0, result.stderr
    assert Path(json.loads(result.stdout)["output"]).is_relative_to(tmp_path / "outputs/custom")


def test_committed_symlink_rejected_without_worktree_link(tmp_path):
    setup(tmp_path)
    link = tmp_path / "link.py"
    link.symlink_to("source.py")
    subprocess.run(["git", "-C", str(tmp_path), "add", "link.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "synthetic link"], check=True)
    link.unlink()
    for revisions in ([], ["--base", "HEAD~1", "--head", "HEAD"]):
        target = revisions or ["--working-tree"]
        result = run(tmp_path, *target, "--file", "link.py", "--dry-run")
        assert result.returncode == 1
        assert "not a regular file" in result.stderr


def test_focused_packet_omits_distant_source_and_has_line_anchors(tmp_path):
    setup(tmp_path)
    lines = [f"line {i}\n" for i in range(1, 151)]
    (tmp_path / "source.py").write_text("".join(lines))
    subprocess.run(["git", "-C", str(tmp_path), "add", "source.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "baseline"], check=True)
    lines[75] = "changed line 76\n"
    (tmp_path / "source.py").write_text("".join(lines))
    result = run(tmp_path, "--working-tree", "--file", "source.py", "--dry-run")
    assert result.returncode == 0, result.stderr
    output = Path(json.loads(result.stdout)["output"])
    packet = json.loads((output / "packet.json").read_text())
    entry = packet["files"][0]
    assert "line 1\n" not in entry["diff"] and "line 150" not in entry["diff"]
    assert "changed line 76" in entry["diff"] and "@@ -70,13 +70,13 @@" in entry["diff"]
    assert "final_source" not in entry
    assert json.loads((output / "metadata.json").read_text())["review_mode"] == "focused-hunks"


def test_explicit_unchanged_context_is_narrow_and_validated(tmp_path):
    setup(tmp_path)
    (tmp_path / "helper.py").write_text("one\ntwo\nthree\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "helper.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "helper"], check=True)
    (tmp_path / "source.py").write_text("changed\n")
    args = ("--working-tree", "--file", "source.py", "--file", "helper.py", "--dry-run")
    assert run(tmp_path, *args).returncode == 1
    result = run(tmp_path, *args, "--context-range", "helper.py:2:2")
    assert result.returncode == 0, result.stderr
    packet = json.loads((Path(json.loads(result.stdout)["output"]) / "packet.json").read_text())
    assert packet["files"][1]["context_ranges"] == [{"start_line": 2, "end_line": 2, "numbered_source": "2: two"}]
    for bounds in ("helper.py:0:2", "helper.py:1:4", "helper.py:3:1", "../outside:1:2", ".env:1:2"):
        assert run(tmp_path, *args, "--context-range", bounds).returncode == 1


def test_completed_review_reused_and_content_policy_changes_invalidate(tmp_path):
    setup(tmp_path)
    (tmp_path / "source.py").write_text("changed\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    count = tmp_path / "calls"
    cli = bindir / "claude"
    cli.write_text(
        "#!/usr/bin/env python3\nimport json, pathlib, sys\n"
        "if '--version' in sys.argv: print('fake 1.0'); sys.exit(0)\n"
        + f"p=pathlib.Path({str(count)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
        + "print(json.dumps({'structured_output':{'summary':'checked','findings':[]}}))\n"
    )
    cli.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    args = ("--working-tree", "--file", "source.py")
    dry = run(tmp_path, *args, "--dry-run", env=env)
    assert dry.returncode == 0 and not count.exists()
    first = run(tmp_path, *args, env=env)
    assert json.loads(first.stdout)["status"] == "reviewed"
    second = run(tmp_path, *args, env=env)
    assert json.loads(second.stdout)["status"] == "reused"
    assert count.read_text() == "x"
    (tmp_path / "source.py").write_text("new change\n")
    assert json.loads(run(tmp_path, *args, env=env).stdout)["status"] == "reviewed"
    (tmp_path / "SECURITY.md").write_text("new policy\n")
    assert json.loads(run(tmp_path, *args, env=env).stdout)["status"] == "reviewed"
    assert count.read_text() == "xxx"


def test_failed_reviews_are_not_cached(tmp_path):
    setup(tmp_path)
    (tmp_path / "source.py").write_text("changed\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cli = bindir / "claude"
    cli.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo \'fake 1.0\'; exit 0; fi\nexit 2\n')
    cli.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    for _ in range(2):
        assert run(tmp_path, "--working-tree", "--file", "source.py", env=env).returncode == 1
    assert not (tmp_path / "outputs/claude-reviews/review-index.json").exists()


def test_review_range_excludes_other_hunks_and_changes_cache_key(tmp_path):
    setup(tmp_path)
    lines = [f"baseline {i}\n" for i in range(1, 151)]
    (tmp_path / "source.py").write_text("".join(lines))
    subprocess.run(["git", "-C", str(tmp_path), "add", "source.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "range baseline"], check=True)
    lines[19], lines[119] = "first changed\n", "second changed\n"
    (tmp_path / "source.py").write_text("".join(lines))
    args = ("--working-tree", "--file", "source.py", "--dry-run")
    first = run(tmp_path, *args, "--review-range", "source.py:20:20")
    assert first.returncode == 0, first.stderr
    info = json.loads(first.stdout)
    packet = json.loads((Path(info["output"]) / "packet.json").read_text())
    entry = packet["files"][0]
    assert "first changed" in entry["diff"] and "second changed" not in entry["diff"]
    assert entry["review_ranges"] == [[20, 20]] and entry["other_changes_excluded"] is True
    second = run(tmp_path, *args, "--review-range", "source.py:120:120")
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["packet_sha256"] != info["packet_sha256"]
    for bounds in ("source.py:70:70", "source.py:0:2", "source.py:150:151", "source.py:20:1", ".env:1:1"):
        assert run(tmp_path, *args, "--review-range", bounds).returncode == 1


def test_review_range_deleted_file_anchor_and_no_full_fallback(tmp_path):
    setup(tmp_path)
    (tmp_path / "source.py").unlink()
    (tmp_path / "other.py").write_text("new unrelated content\n")
    result = run(
        tmp_path,
        "--working-tree",
        "--file",
        "source.py",
        "--file",
        "other.py",
        "--review-range",
        "source.py:1:1",
        "--context-range",
        "other.py:1:1",
        "--dry-run",
    )
    assert result.returncode == 0, result.stderr
    packet = json.loads((Path(json.loads(result.stdout)["output"]) / "packet.json").read_text())
    assert "-original" in packet["files"][0]["diff"]
    assert packet["files"][1]["diff"] == ""
    assert packet["files"][1]["context_ranges"][0]["numbered_source"] == "1: new unrelated content"
    assert (
        run(
            tmp_path,
            "--working-tree",
            "--file",
            "source.py",
            "--file",
            "other.py",
            "--review-range",
            "source.py:1:1",
            "--dry-run",
        ).returncode
        == 1
    )
