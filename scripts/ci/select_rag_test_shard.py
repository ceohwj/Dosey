#!/usr/bin/env python3
"""Deterministic duration-balanced shard selector for RAG integration tests."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

DEFAULT_WEIGHT_SECONDS = 10
SHARD_1_ISOLATED_CLEANUP_OVERHEAD_SECONDS = 12

# Measured from GitHub Actions run 45147209306 (develop 0eeb87b) so the heaviest
# PostgreSQL integration files are spread across independent matrix shards.
KNOWN_FILE_WEIGHTS: dict[str, int] = {
    "tests/integration/rag/test_protected_retrieval_postgresql.py": 101,
    "tests/integration/rag/test_actual_retrieval_bootstrap.py": 99,
    "tests/integration/rag/test_database_role_provisioning.py": 74,
    "tests/integration/rag/test_mfds_detail_acquisition.py": 49,
    "tests/integration/rag/test_catalog_approval_storage.py": 48,
    "tests/integration/rag/test_source_management.py": 43,
    "tests/integration/rag/test_catalog_approval_command.py": 40,
    "tests/integration/rag/test_catalog_storage_roundtrip.py": 37,
    "tests/integration/rag/test_knowledge_evidence_index_postgresql.py": 35,
    "tests/integration/rag/test_ret_h_aws_smoke_verification.py": 33,
    "tests/integration/rag/test_reject_contract_165.py": 29,
    "tests/integration/rag/test_postgresql_evidence_search.py": 26,
    "tests/integration/rag/test_guide_evidence_authority_postgresql.py": 25,
    "tests/integration/rag/test_retrieval_run_postgresql.py": 24,
    "tests/integration/rag/test_source_publication_concurrency.py": 23,
    "tests/integration/rag/test_citation_authorization_authority_postgresql.py": 22,
    "tests/integration/rag/test_ret_h_bootstrap_authority_split.py": 21,
    "tests/integration/rag/test_actual_retrieval_evaluation.py": 17,
    "tests/integration/rag/test_knowledge_materialization_postgresql.py": 16,
    "tests/integration/rag/test_request_guard_runtime_binding_postgresql.py": 11,
}


def select_shard_files(test_files: Iterable[str], *, shard: int, total_shards: int) -> list[str]:
    if total_shards < 1:
        raise ValueError(f"total_shards must be >= 1, got {total_shards}")
    if not 1 <= shard <= total_shards:
        raise ValueError(f"shard must be between 1 and {total_shards}, got {shard}")

    normalized = sorted({Path(path).as_posix() for path in test_files})
    ordered = sorted(
        normalized,
        key=lambda path: (-KNOWN_FILE_WEIGHTS.get(path, DEFAULT_WEIGHT_SECONDS), path),
    )
    loads = [0 for _ in range(total_shards)]
    if total_shards > 1:
        loads[0] = SHARD_1_ISOLATED_CLEANUP_OVERHEAD_SECONDS
    buckets: list[list[str]] = [[] for _ in range(total_shards)]

    for path in ordered:
        target = min(range(total_shards), key=lambda index: (loads[index], index))
        buckets[target].append(path)
        loads[target] += KNOWN_FILE_WEIGHTS.get(path, DEFAULT_WEIGHT_SECONDS)

    return sorted(buckets[shard - 1])


def discover_test_files(root: Path, repo_root: Path) -> list[str]:
    resolved_root = (repo_root / root).resolve() if not root.is_absolute() else root.resolve()
    return sorted(
        path.resolve().relative_to(repo_root.resolve()).as_posix() for path in resolved_root.glob("test_*.py")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Select a balanced shard of RAG integration tests.")
    parser.add_argument("--root", type=Path, default=Path("tests/integration/rag"))
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--total-shards", type=int, required=True)
    parser.add_argument(
        "--run-coverage",
        action="store_true",
        help="Execute the selected shard directly under python -m coverage run -m pytest.",
    )
    args = parser.parse_args(argv)

    repo_root = Path.cwd().resolve()
    discovered = discover_test_files(args.root, repo_root)
    selected = select_shard_files(discovered, shard=args.shard, total_shards=args.total_shards)
    if args.run_coverage:
        completed = subprocess.run(
            [sys.executable, "-m", "coverage", "run", "-m", "pytest", *selected],
            check=False,
        )
        return completed.returncode
    for path in selected:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
