"""Unit tests for the offline 60-case Korean retrieval reranker benchmark."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_worker.tasks.evaluation.rerank_benchmark import (
    BASELINE_VARIANT_ID,
    BENCHMARK_SCOPE,
    CANDIDATE_VARIANT_ID,
    format_report_json,
    format_report_markdown,
    main,
    run_rerank_benchmark,
)
from ai_worker.tasks.rag.evidence_reranker import RerankerConfig

EVALS_ROOT = Path("evals")

EXPECTED_SLICES = frozenset(
    {
        "ALL",
        "EXPRESSION_CANONICAL",
        "EXPRESSION_COLLOQUIAL",
        "EXPRESSION_FRAGMENT",
        "EXPRESSION_LIMITED_TYPO",
        "EXPRESSION_SYNONYM",
        "EXPRESSION_WORD_ORDER_PARTICLE",
        "TOPIC_LIFESTYLE_MANAGEMENT",
        "TOPIC_MEDICATION_INFORMATION",
        "TOPIC_MISSED_DOSE",
        "TOPIC_PRECAUTIONS",
        "TOPIC_STORAGE",
    }
)


def test_benchmark_deterministic_repeatability() -> None:
    first = run_rerank_benchmark(evals_root=EVALS_ROOT)
    second = run_rerank_benchmark(evals_root=EVALS_ROOT)

    assert first.reranker_algorithm_id == second.reranker_algorithm_id
    assert first.reranker_config_hash == second.reranker_config_hash
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.corpus_sha256 == second.corpus_sha256
    assert first.deterministic_result_sha256 == second.deterministic_result_sha256
    assert first.baseline_slices == second.baseline_slices
    assert first.candidate_slices == second.candidate_slices
    assert first.delta_slices == second.delta_slices


def test_benchmark_non_release_boundary_and_slice_coverage() -> None:
    report = run_rerank_benchmark(evals_root=EVALS_ROOT)

    assert report.benchmark_scope == BENCHMARK_SCOPE == "DEV_DIAGNOSTIC_ONLY"
    assert report.release_eligible is False
    assert report.holdout_performed is False
    assert report.dataset_code == "rag-natural-language-retrieval-dev"
    assert report.dataset_version == "1.0.0"
    assert len(report.manifest_sha256) == 64
    assert len(report.corpus_sha256) == 64
    assert report.trigram_threshold == 0.3
    assert report.case_count == 60
    assert report.corpus_record_count == 100
    assert report.baseline_variant_id == BASELINE_VARIANT_ID
    assert report.candidate_variant_id == CANDIDATE_VARIANT_ID
    assert report.reranker_algorithm_id
    assert report.reranker_config_hash
    assert len(report.deterministic_result_sha256) == 64
    assert report.median_rerank_latency_ms >= 0.0
    assert report.p95_rerank_latency_ms >= report.median_rerank_latency_ms

    assert set(report.baseline_slices.keys()) == EXPECTED_SLICES
    assert set(report.candidate_slices.keys()) == EXPECTED_SLICES
    assert set(report.delta_slices.keys()) == EXPECTED_SLICES
    assert report.baseline_slices["ALL"].case_count == 60
    assert report.baseline_slices["ALL"].recall_at_20 == 1.0


def test_benchmark_json_and_markdown_formatting_and_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = run_rerank_benchmark(evals_root=EVALS_ROOT)

    json_text = format_report_json(report)
    parsed = json.loads(json_text)
    assert parsed["benchmark_scope"] == "DEV_DIAGNOSTIC_ONLY"
    assert parsed["release_eligible"] is False
    assert parsed["holdout_performed"] is False
    assert parsed["dataset_code"] == "rag-natural-language-retrieval-dev"
    assert parsed["dataset_version"] == "1.0.0"
    assert parsed["manifest_sha256"] == report.manifest_sha256
    assert parsed["corpus_sha256"] == report.corpus_sha256
    assert parsed["trigram_threshold"] == report.trigram_threshold
    assert parsed["reranker_config_hash"] == report.reranker_config_hash
    assert set(parsed["delta_slices"].keys()) == EXPECTED_SLICES

    markdown_text = format_report_markdown(report)
    assert "DEV_DIAGNOSTIC_ONLY" in markdown_text
    assert BASELINE_VARIANT_ID in markdown_text
    assert CANDIDATE_VARIANT_ID in markdown_text
    assert "EXPRESSION_SYNONYM" in markdown_text
    assert "TOPIC_MEDICATION_INFORMATION" in markdown_text
    assert "## Limitations" in markdown_text
    assert "PostgreSQL/pgvector" in markdown_text
    assert "cluster-bootstrap" in markdown_text

    exit_code_stdout = main(["--evals-root", str(EVALS_ROOT), "--format", "json"])
    assert exit_code_stdout == 0
    captured = capsys.readouterr()
    cli_payload = json.loads(captured.out)
    assert cli_payload["deterministic_result_sha256"] == report.deterministic_result_sha256

    output_md_path = tmp_path / "reports" / "rerank_benchmark.md"
    exit_code_file = main(
        [
            "--evals-root",
            str(EVALS_ROOT),
            "--format",
            "markdown",
            "--output",
            str(output_md_path),
        ]
    )
    assert exit_code_file == 0
    written = output_md_path.read_text(encoding="utf-8")
    assert "DEV_DIAGNOSTIC_ONLY" in written
    assert "## Limitations" in written


def test_reranker_improves_mrr_and_synonym_without_recall_regression() -> None:
    report = run_rerank_benchmark(evals_root=EVALS_ROOT)

    all_base = report.baseline_slices["ALL"]
    all_cand = report.candidate_slices["ALL"]
    all_delta = report.delta_slices["ALL"]

    assert all_cand.recall_at_5 >= all_base.recall_at_5
    assert all_delta.recall_at_5 >= 0.0
    assert all_cand.mrr_at_5 > all_base.mrr_at_5
    assert all_delta.mrr_at_5 > 0.0

    syn_base = report.baseline_slices["EXPRESSION_SYNONYM"]
    syn_cand = report.candidate_slices["EXPRESSION_SYNONYM"]
    syn_delta = report.delta_slices["EXPRESSION_SYNONYM"]

    assert syn_cand.mrr_at_5 > syn_base.mrr_at_5
    assert syn_delta.mrr_at_5 > 0.0


def test_custom_max_input_candidates_tracks_evaluated_pool_recall() -> None:
    default_report = run_rerank_benchmark(evals_root=EVALS_ROOT)
    narrow_report = run_rerank_benchmark(
        evals_root=EVALS_ROOT,
        reranker_config=RerankerConfig(max_input_candidates=3),
    )

    assert len(narrow_report.manifest_sha256) == 64
    assert len(narrow_report.corpus_sha256) == 64
    assert narrow_report.candidate_slices["ALL"].recall_at_20 <= narrow_report.baseline_slices["ALL"].recall_at_20
    assert narrow_report.candidate_slices["ALL"].recall_at_20 < default_report.candidate_slices["ALL"].recall_at_20
    assert narrow_report.delta_slices["ALL"].recall_at_20 < 0.0
    assert narrow_report.deterministic_result_sha256 != default_report.deterministic_result_sha256


def test_cli_error_handling_and_parameter_validation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_root = tmp_path / "does-not-exist"
    exit_missing = main(["--evals-root", str(missing_root), "--format", "json"])
    assert exit_missing == 1
    assert "rerank_benchmark error:" in capsys.readouterr().err

    output_dir = tmp_path / "existing_dir"
    output_dir.mkdir()
    exit_dir = main(["--evals-root", str(EVALS_ROOT), "--format", "json", "--output", str(output_dir)])
    assert exit_dir == 1
    assert "rerank_benchmark error:" in capsys.readouterr().err

    with pytest.raises(ValueError, match="stage1_limit"):
        run_rerank_benchmark(evals_root=EVALS_ROOT, stage1_limit=0)
    with pytest.raises(ValueError, match="stage1_limit"):
        run_rerank_benchmark(evals_root=EVALS_ROOT, stage1_limit=True)
    with pytest.raises(ValueError, match="rrf_k"):
        run_rerank_benchmark(evals_root=EVALS_ROOT, rrf_k=0)
