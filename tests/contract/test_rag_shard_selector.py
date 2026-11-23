from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "ci" / "select_rag_test_shard.py"
RAG_TEST_ROOT = PROJECT_ROOT / "tests" / "integration" / "rag"


def _load_module():
    spec = importlib.util.spec_from_file_location("select_rag_test_shard", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_rag_shards_cover_all_discovered_files_without_overlap() -> None:
    module = _load_module()
    discovered = sorted(path.relative_to(PROJECT_ROOT).as_posix() for path in RAG_TEST_ROOT.glob("test_*.py"))
    assert len(discovered) >= 3

    shards = [module.select_shard_files(discovered, shard=index, total_shards=3) for index in (1, 2, 3)]

    assert all(shards)
    flattened = [path for shard_files in shards for path in shard_files]
    assert len(flattened) == len(set(flattened))
    assert sorted(flattened) == discovered


def test_rag_shards_spread_heaviest_tests_across_distinct_shards() -> None:
    module = _load_module()
    discovered = sorted(path.relative_to(PROJECT_ROOT).as_posix() for path in RAG_TEST_ROOT.glob("test_*.py"))
    shards = [set(module.select_shard_files(discovered, shard=index, total_shards=3)) for index in (1, 2, 3)]

    heavy_files = (
        "tests/integration/rag/test_protected_retrieval_postgresql.py",
        "tests/integration/rag/test_actual_retrieval_bootstrap.py",
        "tests/integration/rag/test_database_role_provisioning.py",
    )
    assigned_shards = {
        next(index for index, shard_set in enumerate(shards) if path in shard_set) for path in heavy_files
    }
    assert len(assigned_shards) == 3


def test_rag_shard_selector_handles_unknown_files_and_validates_bounds() -> None:
    module = _load_module()
    files = [
        "tests/integration/rag/test_new_unknown_a.py",
        "tests/integration/rag/test_new_unknown_b.py",
        "tests/integration/rag/test_new_unknown_c.py",
    ]
    union = sorted(
        path for index in (1, 2, 3) for path in module.select_shard_files(files, shard=index, total_shards=3)
    )
    assert union == files

    with pytest.raises(ValueError):
        module.select_shard_files(files, shard=0, total_shards=3)
    with pytest.raises(ValueError):
        module.select_shard_files(files, shard=4, total_shards=3)
    with pytest.raises(ValueError):
        module.select_shard_files(files, shard=1, total_shards=0)


def test_rag_shard_selector_cli_outputs_valid_repo_relative_paths() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--root",
            "tests/integration/rag",
            "--shard",
            "1",
            "--total-shards",
            "3",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    assert lines
    assert all(line.startswith("tests/integration/rag/test_") and line.endswith(".py") for line in lines)
    assert all((PROJECT_ROOT / line).is_file() for line in lines)
