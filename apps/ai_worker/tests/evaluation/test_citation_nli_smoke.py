"""Offline unit tests for the local NLI citation smoke runner."""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from ai_worker.tasks.evaluation.citation_nli_smoke import (
    DEFAULT_NLI_MODEL_NAME,
    SMOKE_SCOPE,
    CanonicalNliLabel,
    RawScoreMap,
    SyntheticNliSampleCase,
    estimate_token_count,
    load_huggingface_nli_scorer,
    main,
    normalize_nli_label,
    normalize_score_map,
    run_citation_nli_smoke,
    score_claim_evidence_pair,
    split_evidence_for_budget,
    split_sentences,
    summarize_latencies_ms,
)


class _OfflineKeywordNliScorer:
    """Deterministic offline scorer supporting `LABEL_0/1/2` via `id2label`."""

    device = "offline-test-cpu"
    id2label = {0: "entailment", 1: "neutral", 2: "contradiction"}

    def __init__(self) -> None:
        self.recorded_pairs: list[tuple[str, str]] = []

    def score_pairs(self, pairs: Sequence[tuple[str, str]]) -> Sequence[RawScoreMap]:
        outputs: list[dict[str, float]] = []
        for claim, evidence in pairs:
            self.recorded_pairs.append((claim, evidence))
            if "초과하지 마십시오" in evidence or "반드시 의사 또는 약사와 상의" in evidence:
                outputs.append({"LABEL_0": 0.03, "LABEL_1": 0.07, "LABEL_2": 0.90})
            elif "식후 30분" in evidence or "식사 직후 충분한 물과 함께 복용" in evidence:
                outputs.append({"LABEL_0": 0.92, "LABEL_1": 0.05, "LABEL_2": 0.03})
            else:
                outputs.append({"LABEL_0": 0.08, "LABEL_1": 0.86, "LABEL_2": 0.06})
        return outputs


def test_sentence_splitting_preserves_decimals_and_splits_over_512_tokens() -> None:
    short_text = "성인은 1회 1.5정을 복용합니다. 식후 30분에 드세요! 직사광선을 피하세요?"
    assert split_sentences(short_text) == (
        "성인은 1회 1.5정을 복용합니다.",
        "식후 30분에 드세요!",
        "직사광선을 피하세요?",
    )
    assert split_evidence_for_budget("짧은 근거 문장입니다.", claim="짧은 주장입니다.", max_tokens=512) == (
        "짧은 근거 문장입니다.",
    )

    long_filler = "이 약은 실온에서 밀폐 용기에 보관하십시오. " * 35
    long_evidence = f"{long_filler}성인은 1일 3회 식후 30분에 1정을 복용하십시오."
    assert estimate_token_count(long_evidence) > 512

    chunks = split_evidence_for_budget(long_evidence, claim="식후 30분에 복용합니다.", max_tokens=512)
    assert len(chunks) > 5
    assert chunks[-1] == "성인은 1일 3회 식후 30분에 1정을 복용하십시오."

    char_chunks = split_evidence_for_budget(
        "첫 번째 문장은 꽤 깁니다. 두 번째 문장도 있습니다.",
        max_tokens=512,
        max_chars=20,
    )
    assert len(char_chunks) == 2
    assert all(len(chunk) <= 20 for chunk in char_chunks)


def test_max_score_aggregation_and_conservative_alignment() -> None:
    evidence = (
        "보관 시 직사광선을 피하십시오. 성인은 1일 3회 식후에 1정을 복용합니다. 어린이의 손이 닿지 않는 곳에 두십시오."
    )

    def _chunk_scorer(_claim: str, chunk: str) -> Mapping[str | int, float]:
        if "식후에 1정을 복용" in chunk:
            return {"entailment": 0.91, "neutral": 0.06, "contradiction": 0.03}
        if "직사광선" in chunk:
            return {"entailment": 0.05, "neutral": 0.70, "contradiction": 0.25}
        return {"entailment": 0.10, "neutral": 0.80, "contradiction": 0.10}

    result = score_claim_evidence_pair(
        case_id="agg-support",
        claim="성인은 식후에 1정을 복용합니다.",
        evidence=evidence,
        scorer=_chunk_scorer,
        max_chars=30,
    )
    assert result.chunk_count == 3
    assert result.predicted_label == "support"
    assert result.class_scores == {"support": 0.91, "contradict": 0.25, "neutral": 0.8}
    assert result.selected_chunk_index == 1
    assert "식후에 1정을 복용" in result.selected_chunk_text

    # Conservative tie-breaking: contradict > neutral > support when max scores tie.
    def _tie_scorer(_claim: str, chunk: str) -> Mapping[str | int, float]:
        if "첫째" in chunk:
            return {"support": 0.85, "neutral": 0.10, "contradict": 0.05}
        return {"support": 0.05, "neutral": 0.10, "contradict": 0.85}

    tie_result = score_claim_evidence_pair(
        case_id="agg-tie-conservative",
        claim="복용 여부 주장",
        evidence="첫째 문장은 지지합니다. 둘째 문장은 모순됩니다.",
        scorer=_tie_scorer,
        max_chars=15,
    )
    assert tie_result.predicted_label == "contradict"
    assert tie_result.selected_chunk_index == 1


def test_label_normalization_variants_and_logit_softmax() -> None:
    assert normalize_nli_label("ENTAILMENT") == CanonicalNliLabel.SUPPORT
    assert normalize_nli_label("  Supports ") == CanonicalNliLabel.SUPPORT
    assert normalize_nli_label("Contradiction") == CanonicalNliLabel.CONTRADICT
    assert normalize_nli_label("contradict") == CanonicalNliLabel.CONTRADICT
    assert normalize_nli_label("NEUTRAL") == CanonicalNliLabel.NEUTRAL

    mdeberta_id2label = {0: "entailment", 1: "neutral", 2: "contradiction"}
    assert normalize_nli_label("LABEL_0", id2label=mdeberta_id2label) == CanonicalNliLabel.SUPPORT
    assert normalize_nli_label("label_1", id2label=mdeberta_id2label) == CanonicalNliLabel.NEUTRAL
    assert normalize_nli_label(2, id2label=mdeberta_id2label) == CanonicalNliLabel.CONTRADICT

    explicit = {"pos": "support", "neg": "contradict", "unk": "neutral"}
    assert normalize_nli_label("POS", label_mapping=explicit) == CanonicalNliLabel.SUPPORT

    with pytest.raises(ValueError, match="Unsupported NLI label"):
        normalize_nli_label("UNKNOWN_CLASS")

    softmax_scores = normalize_score_map(
        {"LABEL_0": 3.0, "LABEL_1": 0.0, "LABEL_2": -1.0},
        id2label=mdeberta_id2label,
    )
    assert pytest.approx(sum(softmax_scores.values()), rel=1e-6) == 1.0
    assert softmax_scores[CanonicalNliLabel.SUPPORT] > softmax_scores[CanonicalNliLabel.NEUTRAL]
    assert softmax_scores[CanonicalNliLabel.NEUTRAL] > softmax_scores[CanonicalNliLabel.CONTRADICT]


def test_latency_p50_p95_summary() -> None:
    assert summarize_latencies_ms([]) == (0.0, 0.0)
    assert summarize_latencies_ms([12.3456]) == (12.346, 12.346)

    samples = [float(i) for i in range(1, 21)]
    p50, p95 = summarize_latencies_ms(samples)
    assert p50 == 10.5
    assert p95 == 19.0


def test_cli_json_and_markdown_with_injected_offline_scorer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scorer = _OfflineKeywordNliScorer()

    exit_json = main(
        ["--model", "custom/local-nli", "--max-tokens", "512", "--format", "json"],
        scorer=scorer,
    )
    assert exit_json == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["smoke_scope"] == SMOKE_SCOPE
    assert payload["release_eligible"] is False
    assert payload["model_name"] == "custom/local-nli"
    assert payload["device"] == "offline-test-cpu"
    assert payload["max_tokens"] == 512
    assert isinstance(payload["dependency_status"]["ai_group_ready"], bool)
    assert payload["dependency_status"]["install_hint"] == "uv sync --group ai"
    assert payload["load_seconds"] == 0.0
    assert payload["latency_ms_p50"] >= 0.0
    assert payload["latency_ms_p95"] >= payload["latency_ms_p50"]
    assert payload["peak_rss_mb"] > 0.0
    assert payload["sample_count"] == 5
    assert payload["git_commit"]
    assert "--format json" in payload["command"]

    by_id = {item["case_id"]: item for item in payload["sample_results"]}
    assert by_id["nli-smoke-01-support"]["predicted_label"] == "support"
    assert by_id["nli-smoke-02-contradict"]["predicted_label"] == "contradict"
    assert by_id["nli-smoke-03-neutral"]["predicted_label"] == "neutral"
    assert by_id["nli-smoke-04-long-support"]["predicted_label"] == "support"
    assert by_id["nli-smoke-04-long-support"]["chunk_count"] > 1
    assert by_id["nli-smoke-05-contradict-dose"]["predicted_label"] == "contradict"

    md_path = tmp_path / "nli_smoke.md"
    exit_md = main(
        ["--format", "markdown", "--max-chars", "120", "--output", str(md_path)],
        scorer=_OfflineKeywordNliScorer(),
    )
    assert exit_md == 0
    md_text = md_path.read_text(encoding="utf-8")
    assert "# Local NLI Citation Smoke Report" in md_text
    assert DEFAULT_NLI_MODEL_NAME in md_text
    assert "nli-smoke-04-long-support" in md_text
    assert "P50" in md_text and "P95" in md_text


def test_missing_ai_dependencies_fail_closed_with_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_import_module = importlib.import_module

    def _blocked_import_module(name: str, package: str | None = None) -> Any:
        if name in {"torch", "sentence_transformers"}:
            raise ImportError(f"Simulated missing package: {name}")
        return real_import_module(name, package)

    monkeypatch.setitem(sys.modules, "torch", None)
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    monkeypatch.setattr(importlib, "import_module", _blocked_import_module)

    with pytest.raises(RuntimeError, match=r"uv sync --group ai"):
        load_huggingface_nli_scorer()

    with pytest.raises(RuntimeError, match=r"\[dependency-groups\] ai"):
        run_citation_nli_smoke(
            sample_cases=(
                SyntheticNliSampleCase(
                    case_id="single",
                    claim="주장",
                    evidence="근거",
                    expected_hint=CanonicalNliLabel.NEUTRAL,
                ),
            )
        )

    exit_code = main(["--format", "json"])
    assert exit_code == 1
    err_output = capsys.readouterr().err
    assert "citation_nli_smoke error:" in err_output
    assert "uv sync --group ai" in err_output
