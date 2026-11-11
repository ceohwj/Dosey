import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/ci/check_agent_hygiene.py"


def module(path=SCRIPT):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    item = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = item
    spec.loader.exec_module(item)
    return item


def write(root, name, content=""):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def cli(root, *args):
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), *args], capture_output=True, text=True)


def snapshot(root):
    return {
        str(p.relative_to(root)): (p.lstat().st_mtime_ns, p.read_bytes())
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink()
    }


def test_cli_findings_line_numbers_and_zero_mutation(tmp_path):
    write(tmp_path, "guide.md", "# Guide\n[missing](absent.md)\n[anchor](target.md#absent)\n")
    write(tmp_path, "target.md", "# Present\n")
    write(tmp_path, "scripts/tmp_probe.py", "synthetic")
    before = snapshot(tmp_path)
    result = cli(tmp_path, "--document", "guide.md", "--check")
    assert result.returncode == 1
    data = json.loads(result.stdout)
    assert data["mutation"] is False
    assert data["checked_paths"] == ["guide.md"]
    assert {(f["path"], f["line"], f["code"]) for f in data["findings"]} == {
        ("guide.md", 2, "missing_target"),
        ("guide.md", 3, "missing_anchor"),
        ("scripts/tmp_probe.py", 1, "temporary_source"),
    }
    assert all(f["message"] for f in data["findings"])
    assert snapshot(tmp_path) == before


def test_good_links_unicode_duplicate_headings_relative_directories_and_references(tmp_path):
    write(
        tmp_path,
        "docs/guide.md",
        "[up](../target.md#한글-제목)\n[dir](../folder/)\n[dup](../target.md#repeat-1)\n[ref][ok]\n\n[ok]: ../target.md#repeat\n",
    )
    write(tmp_path, "target.md", "# 한글 제목\n## Repeat\n## Repeat\n")
    (tmp_path / "folder").mkdir()
    result = cli(tmp_path, "--document", "docs/guide.md")
    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout)["findings"] == []


def test_external_fenced_inline_examples_and_history_are_not_recursive(tmp_path):
    write(
        tmp_path,
        "guide.md",
        "[web](https://example.invalid/absent#no)\n[mail](mailto:a@example.invalid)\n```md\n[example](absent.md)\n```\n~~~\n[example](absent.md)\n~~~\n`[inline example](absent.md)`\nHistorical: [old](history.md)\n",
    )
    write(tmp_path, "history.md", "[old broken link](absent.md)\n")
    assert cli(tmp_path, "--document", "guide.md").returncode == 0
    assert cli(tmp_path, "--document", "history.md").returncode == 1
    write(tmp_path, "guide.md", "Historical context does not hide [normative](absent.md)\n")
    assert cli(tmp_path, "--document", "guide.md").returncode == 1


@pytest.mark.parametrize(
    "target",
    [
        "../outside.md",
        "/etc/passwd",
        ".omx/state/x.md",
        "data/raw.md",
        "outputs/raw.md",
        "knowledge/raw.md",
        ".env",
        "docs/../uploads/raw.md",
    ],
)
def test_unsafe_links_are_findings_without_reading_target(tmp_path, target):
    write(tmp_path, "guide.md", f"[unsafe]({target})\n")
    result = module().scan_hygiene(tmp_path, ["guide.md"])
    assert result["findings"][0]["code"] == "unsafe_path"


def test_symlink_components_and_source_trees_are_not_followed(tmp_path):
    outside = tmp_path / "outside"
    write(outside, "raw.md", "# Outside")
    write(outside, "tmp_secret.py")
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    (tmp_path / "scripts").symlink_to(outside, target_is_directory=True)
    write(tmp_path, "guide.md", "[link](link/raw.md#outside)\n")
    result = module().scan_hygiene(tmp_path, ["guide.md"])
    assert [f["code"] for f in result["findings"]] == ["unsafe_path"]
    bad = cli(tmp_path, "--document", "link/raw.md")
    assert bad.returncode == 2
    assert json.loads(bad.stderr)["code"] == "invocation_error"


def test_bounded_source_names_exclude_data_cache_and_symlinks(tmp_path):
    candidates = [
        "scripts/temp_probe.py",
        "backend/app/a_old.py",
        "ai_worker/a_backup.py",
        "frontend/src/tmp_probe.tsx",
    ]
    for name in candidates + [
        "docs/tmp_note.md",
        "backend/elsewhere/tmp_no.py",
        "scripts/node_modules/tmp_no.js",
        "ai_worker/data/tmp_no.py",
        "scripts/cache/tmp_no.py",
        "scripts/normal.py",
    ]:
        write(tmp_path, name)
    write(tmp_path, "guide.md", "# Good")
    result = module().scan_hygiene(tmp_path, ["guide.md"])
    assert {f["path"] for f in result["findings"]} == set(candidates)


@pytest.mark.parametrize(
    "args", [("--document", "../outside.md"), ("--document", "missing.md"), ("--apply",), ("--delete",)]
)
def test_invalid_invocation_is_json_error(tmp_path, args):
    result = cli(tmp_path, *args)
    assert result.returncode == 2
    assert json.loads(result.stderr)["code"] == "invocation_error"
    assert not result.stdout


def test_default_documents_and_repeatable_selection(tmp_path):
    defaults = [
        "AGENTS.md",
        "docs/ai/workflow.md",
        "docs/ai/task-template.md",
        "docs/ai/knowledge-index.md",
        "docs/failures/README.md",
    ]
    for name in defaults:
        write(tmp_path, name, "# Good")
    write(tmp_path, "docs/history.md", "[bad](missing.md)")
    result = json.loads(cli(tmp_path).stdout)
    assert result["checked_paths"] == defaults
    assert result["findings"] == []
    selected = cli(tmp_path, "--document", defaults[0], "--document", defaults[1])
    assert json.loads(selected.stdout)["checked_paths"] == defaults[:2]


def test_ci_schedules_stdlib_hygiene_for_docs_and_existing_events(tmp_path):
    # Parse the workflow and execute its actual scanner command against a bad fixture.
    workflow = yaml.load((ROOT / ".github/workflows/checks.yml").read_text(), Loader=yaml.BaseLoader)
    assert {"push", "pull_request", "merge_group"} <= workflow["on"].keys()
    for event in workflow["on"].values():
        assert not {"paths", "paths-ignore"} & event.keys()
    jobs = workflow["jobs"]
    hygiene = jobs["agent-hygiene"]
    assert "if" not in hygiene and "needs" not in hygiene
    assert all("if" not in step for step in hygiene["steps"])
    commands = [step["run"] for step in hygiene["steps"] if "run" in step]
    assert len(commands) == 1
    write(tmp_path, "AGENTS.md", "[bad](missing.md)")
    for name in [
        "docs/ai/workflow.md",
        "docs/ai/task-template.md",
        "docs/ai/knowledge-index.md",
        "docs/failures/README.md",
    ]:
        write(tmp_path, name)
    # Run from fixture checkout with the same relative script location.
    write(tmp_path, "scripts/ci/check_agent_hygiene.py", SCRIPT.read_text())
    result = subprocess.run(
        commands[0],
        cwd=tmp_path,
        shell=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]},
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["findings"][0]["code"] == "missing_target"
    classifier = module(ROOT / "scripts/ci/classify_ci_scope.py")
    docs = classifier.classify_paths(["docs/ai/workflow.md"])
    assert docs.contract and not docs.lint
    assert classifier.classify_paths(["unknown-file"]).full
    assert classifier.classify_paths(["tests/services/test_agent_hygiene.py"]).contract
    assert {"test", "lint", "frontend", "test-contract", "classify-test-scope"} <= jobs.keys()


def test_anchors_preserve_inline_code_and_setext_headings(tmp_path):
    write(
        tmp_path,
        "guide.md",
        "[code](target.md#api-scan_hygiene)\n[setext](target.md#setext-title)\n[html](target.md#custom)\n",
    )
    write(tmp_path, "target.md", '# API `scan_hygiene`\n\nSetext title\n---\n<a id="custom"></a>\n')
    assert cli(tmp_path, "--document", "guide.md").returncode == 0


def test_encoded_links_and_symlink_escape_remain_bounded(tmp_path):
    write(tmp_path, "guide.md", "[space](<some file.md#제목>)\n")
    write(tmp_path, "some file.md", "# 제목\n")
    assert cli(tmp_path, "--document", "guide.md").returncode == 0
    write(tmp_path, "guide.md", "[escape](%2e%2e/outside.md)\n")
    assert json.loads(cli(tmp_path, "--document", "guide.md").stdout)["findings"][0]["code"] == "unsafe_path"
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    write(outside, "sentinel.md", "# Outside")
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    write(tmp_path, "guide.md", "[escape](escape/sentinel.md#outside)\n")
    assert json.loads(cli(tmp_path, "--document", "guide.md").stdout)["findings"][0]["code"] == "unsafe_path"


def test_invalid_root_and_bounded_document_read(tmp_path):
    assert cli(tmp_path / "missing").returncode == 2
    write(tmp_path, "guide.md", "x" * (1024 * 1024 + 1))
    assert cli(tmp_path, "--document", "guide.md").returncode == 2


@pytest.mark.parametrize(
    "destination",
    ["file://localhost/etc/passwd", "file:///etc/passwd", "file:relative.md", "FILE://localhost/etc/passwd"],
)
def test_all_file_urls_are_unsafe_repository_references(tmp_path, destination):
    write(tmp_path, "guide.md", f"[local]({destination})\n")
    result = cli(tmp_path, "--document", "guide.md")
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["findings"][0]["code"] == "unsafe_path"
    assert report["mutation"] is False


def test_https_and_protocol_relative_urls_remain_external(tmp_path):
    write(
        tmp_path, "guide.md", "[https](https://example.invalid/missing.md)\n[relative](//example.invalid/missing.md)\n"
    )
    result = cli(tmp_path, "--document", "guide.md")
    assert result.returncode == 0
    assert json.loads(result.stdout)["findings"] == []
