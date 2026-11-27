"""Offline unit tests for citation judge comparison benchmark and runners."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from ai_worker.tasks.evaluation import citation_nli_smoke
from ai_worker.tasks.evaluation.citation_judge_compare import (
    BENCHMARK_CASES,
    CaseJudgeRecord,
    LocalNliCitationJudge,
    OpenAILlmCitationJudge,
    RulePlusNliCitationJudge,
    compute_judge_metrics,
    compute_openai_cost_usd,
    main,
    parse_judges_arg,
    select_top_k_evidence_sentences,
)
from ai_worker.tasks.evaluation.citation_nli_smoke import RawScoreMap, estimate_token_count


class _RecordingNliScorer:
    """Offline NLI scorer that records `(premise, hypothesis)` pairs and supports logits/labels."""

    device = "offline-test-cpu"
    id2label = {0: "entailment", 1: "neutral", 2: "contradiction"}

    def __init__(self, fixed_scores: RawScoreMap | None = None) -> None:
        self.recorded_pairs: list[tuple[str, str]] = []
        self._fixed_scores = fixed_scores or {"LABEL_0": 2.4, "LABEL_1": -0.8, "LABEL_2": -1.5}

    def score_pairs(self, pairs: Sequence[tuple[str, str]]) -> Sequence[RawScoreMap]:
        self.recorded_pairs.extend(pairs)
        return [dict(self._fixed_scores) for _ in pairs]


def test_benchmark_dataset_balance_and_integrity() -> None:
    assert len(BENCHMARK_CASES) == 60
    case_ids = [case.case_id for case in BENCHMARK_CASES]
    assert len(set(case_ids)) == 60

    verdict_counts = Counter(case.gold_verdict for case in BENCHMARK_CASES)
    assert verdict_counts == {"support": 20, "neutral": 20, "contradict": 20}

    kind_counts = Counter(case.claim_kind for case in BENCHMARK_CASES)
    assert kind_counts == {"critical": 30, "non_critical": 30}

    per_verdict_kind = Counter((case.gold_verdict, case.claim_kind) for case in BENCHMARK_CASES)
    for verdict in ("support", "neutral", "contradict"):
        assert per_verdict_kind[(verdict, "critical")] == 10
        assert per_verdict_kind[(verdict, "non_critical")] == 10


def test_compute_judge_metrics_exact_calculations() -> None:
    records = [
        CaseJudgeRecord("c1", "critical", "support", "support", 10.0, 100, 20, 0.000027, None, "ok"),
        CaseJudgeRecord("c2", "critical", "contradict", "contradict", 20.0, 100, 20, 0.000027, None, "ok"),
        CaseJudgeRecord("c3", "critical", "contradict", "neutral", 30.0, 100, 20, 0.000027, None, "miss"),
        CaseJudgeRecord("c4", "critical", "neutral", "support", 40.0, 100, 20, 0.000027, None, "false_sup"),
        CaseJudgeRecord("c5", "non_critical", "neutral", "neutral", 100.0, 0, 0, 0.0, "err", "fail"),
    ]
    metrics = compute_judge_metrics(judge_name="test_judge", records=records)

    assert metrics.total_cases == 5
    assert metrics.correct_count == 3
    assert metrics.accuracy == pytest.approx(0.6)
    assert metrics.support_recall == pytest.approx(1.0)
    assert metrics.neutral_recall == pytest.approx(0.5)
    assert metrics.contradict_recall == pytest.approx(0.5)
    # support: tp=1, fp=1, fn=0 -> f1=2/3; neutral: tp=1, fp=1, fn=1 -> f1=0.5; contradict: tp=1, fp=0, fn=1 -> f1=2/3
    expected_macro_f1 = round((2 / 3 + 0.5 + 2 / 3) / 3, 4)
    assert metrics.macro_f1 == pytest.approx(expected_macro_f1)

    assert metrics.critical_case_count == 4
    assert metrics.critical_contradict_count == 2
    assert metrics.critical_contradict_recall == pytest.approx(0.5)
    assert metrics.critical_non_support_count == 3  # c2, c3, c4
    assert metrics.critical_false_support_count == 1  # c4
    assert metrics.critical_false_support_rate == pytest.approx(round(1 / 3, 4))
    assert metrics.passes_adr_0004_critical_fsr is False
    assert metrics.passes_adr_0004_macro_f1 is False
    assert metrics.latency_ms_p50 == pytest.approx(30.0)
    assert metrics.latency_ms_p95 == pytest.approx(100.0)
    assert metrics.error_count == 1
    assert metrics.total_cost_usd == pytest.approx(0.000108)
    assert metrics.cost_per_100_claims_usd == pytest.approx(0.00216)


def test_top_k_sentence_prefilter_and_token_bound() -> None:
    filler = "이 약은 흰색 원형 필름코팅정제로 실온에 밀폐 보관하십시오. " * 15
    target_sentence = "성인은 1회 1정을 하루 3회 식후 30분에 충분한 물과 함께 복용하십시오."
    tail_filler = "사용기한이 지난 의약품은 약국 폐의약품 수거함에 폐기하십시오. " * 10
    long_evidence = f"{filler} {target_sentence} {tail_filler}"

    claim = "성인은 1일 3회 식후 30분에 1정씩 복용합니다."
    selected = select_top_k_evidence_sentences(
        claim=claim,
        evidence=long_evidence,
        max_sentences=3,
        max_tokens=512,
    )
    assert 1 <= len(selected) <= 3
    assert target_sentence in selected
    joined = " ".join(selected)
    assert estimate_token_count(joined) + estimate_token_count(claim) < 512

    oversized_single = "약물 " * 600
    bounded = select_top_k_evidence_sentences(
        claim=claim,
        evidence=oversized_single,
        max_sentences=3,
        max_tokens=120,
    )
    assert len(bounded) == 1
    assert estimate_token_count(bounded[0]) + estimate_token_count(claim) <= 120


def test_local_nli_uses_premise_hypothesis_order_and_fails_closed_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scorer = _RecordingNliScorer()
    judge = LocalNliCitationJudge(scorer=scorer)
    claim = "이 약은 식후 30분에 물과 함께 복용합니다."
    evidence = "성인은 1일 3회 식후 30분에 충분한 물과 함께 1정을 복용하십시오."

    pred = judge.evaluate_sync(claim=claim, evidence=evidence, claim_kind="critical")
    assert pred.verdict == "support"
    assert scorer.recorded_pairs == [(evidence, claim)]

    real_import = importlib.import_module

    def _missing_ai_import(name: str, package: str | None = None) -> Any:
        if name in {"torch", "sentence_transformers"}:
            raise ImportError(f"Simulated missing {name}")
        return real_import(name, package)

    monkeypatch.setattr(citation_nli_smoke.importlib, "import_module", _missing_ai_import)
    live_judge = LocalNliCitationJudge(scorer=None)
    with pytest.raises(RuntimeError, match="uv sync --group ai"):
        live_judge.evaluate_sync(claim=claim, evidence=evidence)


def test_rule_plus_nli_catches_conflicts_and_preserves_critical_supports() -> None:
    always_support_scorer = _RecordingNliScorer(fixed_scores={0: 0.95, 1: 0.03, 2: 0.02})
    rule_judge = RulePlusNliCitationJudge(scorer=always_support_scorer)

    # Numeric/frequency conflict: 1일 1회 vs 1일 2회
    freq_pred = rule_judge.evaluate_sync(
        claim="성인은 1일 2회 1정씩 복용합니다.",
        evidence="성인은 1일 1회 1정을 매일 같은 시간에 복용하십시오.",
        claim_kind="critical",
    )
    assert freq_pred.verdict == "contradict"
    assert "daily_frequency_conflict" in freq_pred.reason

    # Timing conflict: 식전 vs 식후
    timing_pred = rule_judge.evaluate_sync(
        claim="반드시 식전에 복용하세요.",
        evidence="위장 자극을 줄이기 위해 식후 30분에 복용하십시오.",
        claim_kind="critical",
    )
    assert timing_pred.verdict == "contradict"
    assert "meal_timing_conflict" in timing_pred.reason

    # Contraindication / prohibition flip: 임부 금기 vs 안전 복용
    contra_pred = rule_judge.evaluate_sync(
        claim="임산부도 별도 상담 없이 안전하게 복용할 수 있습니다.",
        evidence="임부 또는 임신 가능성이 있는 여성에게는 이 약을 투여하지 않는다.",
        claim_kind="critical",
    )
    assert contra_pred.verdict == "contradict"
    assert "prohibition_flipped_to_permissive" in contra_pred.reason

    # Unverified critical numeric detail -> downgraded from support to neutral
    unverified_pred = rule_judge.evaluate_sync(
        claim="증상이 가벼우면 1회 250mg으로 복용합니다.",
        evidence="보관방법: 실온에서 밀폐 보관하십시오.",
        claim_kind="critical",
    )
    assert unverified_pred.verdict == "neutral"
    assert "unverified_critical_unit" in unverified_pred.reason

    # All 10 critical support cases in BENCHMARK_CASES remain `support`.
    critical_supports = [
        case for case in BENCHMARK_CASES if case.gold_verdict == "support" and case.claim_kind == "critical"
    ]
    assert len(critical_supports) == 10
    for case in critical_supports:
        pred = rule_judge.evaluate_sync(claim=case.claim, evidence=case.evidence, claim_kind=case.claim_kind)
        assert pred.verdict == "support", f"Unexpected downgrade on {case.case_id}: {pred.reason}"


def test_openai_llm_judge_parsing_cost_and_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert compute_openai_cost_usd("gpt-4o-mini", input_tokens=1_000_000, output_tokens=1_000_000) == pytest.approx(
        0.75
    )

    class _FakeOpenAIClient:
        def __init__(self, responses: Sequence[Any]) -> None:
            self._responses = list(responses)
            self._idx = 0

        async def __call__(self, *, model: str, messages: Sequence[Mapping[str, str]]) -> Any:
            del model, messages
            item = self._responses[self._idx]
            self._idx += 1
            if isinstance(item, Exception):
                raise item
            return item

    fake_client = _FakeOpenAIClient(
        [
            {
                "choices": [{"message": {"content": '{"verdict": "support", "reason": "용법이 일치합니다."}'}}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 40},
            },
            {
                "choices": [{"message": {"content": "not-valid-json"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            },
            {
                "choices": [{"message": {"content": '{"verdict": "maybe", "reason": "invalid label"}'}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            },
            {
                "choices": [{"message": {"content": '{"verdict": "support", "reason": "ok", "extra": 1}'}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            },
            RuntimeError("simulated network error"),
        ]
    )

    judge = OpenAILlmCitationJudge(model="gpt-4o-mini", client=fake_client)

    valid_pred = asyncio.run(judge.evaluate(claim="A", evidence="A", claim_kind="critical"))
    assert valid_pred.verdict == "support"
    assert valid_pred.error is None
    assert valid_pred.input_tokens == 200
    assert valid_pred.output_tokens == 40
    expected_cost = (200 * 0.15 + 40 * 0.60) / 1_000_000.0
    assert valid_pred.cost_usd == pytest.approx(expected_cost)

    for _ in range(4):
        bad_pred = asyncio.run(judge.evaluate(claim="A", evidence="B", claim_kind="critical"))
        assert bad_pred.verdict == "neutral"
        assert bad_pred.error is not None

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    live_no_key = OpenAILlmCitationJudge(client=None)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        asyncio.run(live_no_key.evaluate(claim="A", evidence="B"))


def test_offline_smoke_cli_json_and_markdown(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc_json = main(["--offline-smoke", "--format", "json"])
    assert rc_json == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["benchmark_size"] == 60
    assert payload["offline_smoke"] is True
    assert [j["judge_name"] for j in payload["judges"]] == [
        "local_nli",
        "rule_plus_nli",
        "openai_llm_judge",
    ]
    for judge_metrics in payload["judges"]:
        assert judge_metrics["total_cases"] == 60
        assert judge_metrics["critical_case_count"] == 30
        assert judge_metrics["error_count"] == 0

    md_path = tmp_path / "compare.md"
    rc_md = main(
        [
            "--offline-smoke",
            "--judges",
            "local_nli,rule_plus_nli",
            "--format",
            "markdown",
            "--output",
            str(md_path),
        ]
    )
    assert rc_md == 0
    md_text = md_path.read_text(encoding="utf-8")
    assert "# Citation Judge Comparison Report" in md_text
    assert "`local_nli`" in md_text
    assert "`rule_plus_nli`" in md_text
    assert "`openai_llm_judge`" not in md_text

    with pytest.raises(ValueError, match="Unknown judge"):
        parse_judges_arg("local_nli,unknown_judge")

    rc_bad = main(["--offline-smoke", "--judges", "bad_judge"])
    assert rc_bad == 1
    assert "Unknown judge" in capsys.readouterr().err
    assert sys.version_info >= (3, 11)
