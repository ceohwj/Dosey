"""Offline reproducible 60-case Korean retrieval reranker benchmark (DEV_DIAGNOSTIC_ONLY)."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
import time
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from ai_worker.tasks.evaluation.retrieval_metrics import (
    RetrievalObservation,
    ndcg_at_k,
    no_hit,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)
from ai_worker.tasks.rag.evidence_rank_fusion import (
    LexicalCandidateInput,
    LexicalFusionCandidate,
    StableCoordinate,
    fuse_lexical_subsearches,
)
from ai_worker.tasks.rag.evidence_reranker import (
    RerankCandidateInput,
    RerankerConfig,
    rerank_evidence_candidates,
)

BENCHMARK_SCOPE = "DEV_DIAGNOSTIC_ONLY"
DATASET_MANIFEST_RELPATH = Path("retrieval/manifests/rag-natural-language-retrieval-dev-v1.dataset.json")
SYNTHETIC_INDEX_RELPATH = Path(
    "retrieval/evidence/resources/rag-natural-language-retrieval-dev-v1/synthetic-knowledge-index.json"
)
BASELINE_VARIANT_ID = "RET-L-OFFLINE"
CANDIDATE_VARIANT_ID = "RET-LR-OFFLINE"
DEFAULT_STAGE1_LIMIT = 20
DEFAULT_RRF_K = 60
DEFAULT_TRIGRAM_THRESHOLD = 0.3
SYNTHETIC_SOURCE_CODE = "SYNTHETIC_DEV"
SYNTHETIC_SOURCE_VERSION = "1.0.0"

_SIX_PLACES = Decimal("0.000001")
_TOKEN_PATTERN = re.compile(r"[0-9a-zA-Z가-힣]+")
_WHITESPACE_PATTERN = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class BenchmarkCorpusRecord:
    evidence_ref_id: str
    content_sha256: str
    product_code: str
    statement: str
    topic: str


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    case_id: str
    query: str
    slice_ids: tuple[str, ...]
    required_ids: tuple[str, ...]
    relevant_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LoadedBenchmarkDataset:
    dataset_code: str
    dataset_version: str
    manifest_sha256: str
    corpus_sha256: str
    cases: tuple[BenchmarkCase, ...]
    corpus: tuple[BenchmarkCorpusRecord, ...]


@dataclass(frozen=True, slots=True)
class SliceMetrics:
    case_count: int
    recall_at_5: float
    precision_at_5: float
    mrr_at_5: float
    ndcg_at_5: float
    no_hit_rate_at_5: float
    top1_accuracy: float
    recall_at_20: float


@dataclass(frozen=True, slots=True)
class SliceMetricDelta:
    recall_at_5: float
    precision_at_5: float
    mrr_at_5: float
    ndcg_at_5: float
    no_hit_rate_at_5: float
    top1_accuracy: float
    recall_at_20: float


@dataclass(frozen=True, slots=True)
class RerankBenchmarkReport:
    benchmark_scope: str
    release_eligible: bool
    holdout_performed: bool
    dataset_code: str
    dataset_version: str
    manifest_sha256: str
    corpus_sha256: str
    case_count: int
    corpus_record_count: int
    baseline_variant_id: str
    candidate_variant_id: str
    stage1_limit: int
    rrf_k: int
    trigram_threshold: float
    reranker_algorithm_id: str
    reranker_config_hash: str
    deterministic_result_sha256: str
    median_rerank_latency_ms: float
    p95_rerank_latency_ms: float
    baseline_slices: dict[str, SliceMetrics]
    candidate_slices: dict[str, SliceMetrics]
    delta_slices: dict[str, SliceMetricDelta]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _validate_positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _normalize_text(text: str) -> str:
    return _WHITESPACE_PATTERN.sub(" ", unicodedata.normalize("NFKC", text).strip().lower())


def _extract_tokens(normalized_text: str) -> list[str]:
    return _TOKEN_PATTERN.findall(normalized_text)


def _extract_pg_trigrams(normalized_text: str) -> frozenset[str]:
    grams: set[str] = set()
    for word in _extract_tokens(normalized_text):
        padded = f"  {word} "
        for idx in range(len(padded) - 2):
            grams.add(padded[idx : idx + 3])
    return frozenset(grams)


def _trigram_similarity(query_grams: frozenset[str], doc_grams: frozenset[str]) -> float:
    if not query_grams or not doc_grams:
        return 0.0
    union_size = len(query_grams | doc_grams)
    return len(query_grams & doc_grams) / union_size if union_size > 0 else 0.0


def _simple_fts_score(query_tokens: frozenset[str], doc_tokens: Sequence[str]) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    doc_token_set = set(doc_tokens)
    unique_hits = len(query_tokens & doc_token_set)
    if unique_hits == 0:
        return 0.0
    total_hits = sum(1 for token in doc_tokens if token in query_tokens)
    return (unique_hits / len(query_tokens)) + (0.01 * total_hits / len(doc_tokens))


def load_dev_benchmark_dataset(evals_root: Path = Path("evals")) -> LoadedBenchmarkDataset:
    resolved_root = evals_root.resolve()
    manifest_path = (resolved_root / DATASET_MANIFEST_RELPATH).resolve()
    corpus_path = (resolved_root / SYNTHETIC_INDEX_RELPATH).resolve()

    manifest_bytes = manifest_path.read_bytes()
    corpus_bytes = corpus_path.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    corpus_sha256 = hashlib.sha256(corpus_bytes).hexdigest()

    manifest = json.loads(manifest_bytes.decode("utf-8"))
    corpus_payload = json.loads(corpus_bytes.decode("utf-8"))

    cases: list[BenchmarkCase] = []
    for entry in manifest["case_resources"]:
        case_path = (resolved_root / entry["path"]).resolve()
        if not case_path.is_relative_to(resolved_root):
            raise ValueError(f"case path escapes evals_root: {entry['path']}")
        raw_case = json.loads(case_path.read_text(encoding="utf-8"))
        expected = raw_case["expected"]
        cases.append(
            BenchmarkCase(
                case_id=str(raw_case["case_id"]),
                query=str(raw_case["query"]),
                slice_ids=tuple(str(s) for s in raw_case["slice_ids"]),
                required_ids=tuple(str(r) for r in expected["required_evidence_refs"]),
                relevant_ids=tuple(str(r) for r in expected["relevant_evidence_refs"]),
            )
        )

    corpus = tuple(
        BenchmarkCorpusRecord(
            evidence_ref_id=str(rec["evidence_ref_id"]),
            content_sha256=str(rec["content_sha256"]),
            product_code=str(rec["product_code"]),
            statement=str(rec["statement"]),
            topic=str(rec["topic"]),
        )
        for rec in corpus_payload["records"]
    )

    return LoadedBenchmarkDataset(
        dataset_code=str(manifest["dataset_code"]),
        dataset_version=str(manifest["dataset_version"]),
        manifest_sha256=manifest_sha256,
        corpus_sha256=corpus_sha256,
        cases=tuple(cases),
        corpus=corpus,
    )


@dataclass(frozen=True, slots=True)
class _PreparedCorpusRecord:
    record: BenchmarkCorpusRecord
    coordinate: StableCoordinate
    normalized_statement: str
    trigrams: frozenset[str]
    tokens: tuple[str, ...]


def _prepare_corpus(corpus: Sequence[BenchmarkCorpusRecord]) -> tuple[_PreparedCorpusRecord, ...]:
    prepared: list[_PreparedCorpusRecord] = []
    for rec in corpus:
        normalized = _normalize_text(rec.statement)
        prepared.append(
            _PreparedCorpusRecord(
                record=rec,
                coordinate=StableCoordinate(
                    source_code=SYNTHETIC_SOURCE_CODE,
                    source_version=SYNTHETIC_SOURCE_VERSION,
                    external_document_id=rec.evidence_ref_id,
                    chunk_index=0,
                ),
                normalized_statement=normalized,
                trigrams=_extract_pg_trigrams(normalized),
                tokens=tuple(_extract_tokens(normalized)),
            )
        )
    return tuple(prepared)


def run_stage1_offline_lexical(
    query: str,
    prepared_corpus: Sequence[_PreparedCorpusRecord],
    *,
    limit: int = DEFAULT_STAGE1_LIMIT,
    rrf_k: int = DEFAULT_RRF_K,
    trigram_threshold: float = DEFAULT_TRIGRAM_THRESHOLD,
) -> list[LexicalFusionCandidate]:
    _validate_positive_int("limit", limit)
    _validate_positive_int("rrf_k", rrf_k)

    normalized_query = _normalize_text(query)
    query_grams = _extract_pg_trigrams(normalized_query)
    query_tokens = frozenset(_extract_tokens(normalized_query))

    lexical_inputs: list[LexicalCandidateInput] = []
    for item in prepared_corpus:
        is_exact = bool(normalized_query) and normalized_query in item.normalized_statement
        trigram_sim = _trigram_similarity(query_grams, item.trigrams)
        trigram_score = trigram_sim if trigram_sim >= trigram_threshold else None
        fts_raw = _simple_fts_score(query_tokens, item.tokens)
        fts_score = fts_raw if fts_raw > 0.0 else None

        if is_exact or trigram_score is not None or fts_score is not None:
            lexical_inputs.append(
                LexicalCandidateInput(
                    coordinate=item.coordinate,
                    content_hash=item.record.content_sha256,
                    is_exact=is_exact,
                    trigram_score=trigram_score,
                    fts_score=fts_score,
                )
            )

    return fuse_lexical_subsearches(lexical_inputs, limit=limit, rrf_k=rrf_k)


def _round_six(value: Decimal) -> float:
    return float(value.quantize(_SIX_PLACES, rounding=ROUND_HALF_EVEN))


def _top1_hit(observation: RetrievalObservation) -> Decimal:
    if observation.ranked_ids and observation.ranked_ids[0] in set(observation.relevant_ids):
        return Decimal("1.000000")
    return Decimal("0.000000")


def _slice_observation_pool(
    observations: Sequence[RetrievalObservation],
    max_candidates: int,
) -> list[RetrievalObservation]:
    return [
        RetrievalObservation(
            case_id=obs.case_id,
            slice_ids=obs.slice_ids,
            independent_group_id=obs.independent_group_id,
            required_ids=obs.required_ids,
            relevant_ids=obs.relevant_ids,
            ranked_ids=obs.ranked_ids[:max_candidates],
        )
        for obs in observations
    ]


def _compute_slice_metrics(
    observations: Sequence[RetrievalObservation],
    evaluated_pool_observations: Sequence[RetrievalObservation],
) -> SliceMetrics:
    count = len(observations)
    if count == 0:
        raise ValueError("slice observations must not be empty")
    divisor = Decimal(count)

    recall5_sum = sum((recall_at_k(obs, k=5) for obs in observations), Decimal(0))
    precision5_sum = sum((precision_at_k(obs, k=5) for obs in observations), Decimal(0))
    mrr5_sum = sum((reciprocal_rank(obs, k=5) for obs in observations), Decimal(0))
    ndcg5_sum = sum((ndcg_at_k(obs, k=5) for obs in observations), Decimal(0))
    no_hit5_sum = sum((no_hit(obs, k=5) for obs in observations), Decimal(0))
    top1_sum = sum((_top1_hit(obs) for obs in observations), Decimal(0))
    recall20_sum = sum((recall_at_k(obs, k=20) for obs in evaluated_pool_observations), Decimal(0))

    return SliceMetrics(
        case_count=count,
        recall_at_5=_round_six(recall5_sum / divisor),
        precision_at_5=_round_six(precision5_sum / divisor),
        mrr_at_5=_round_six(mrr5_sum / divisor),
        ndcg_at_5=_round_six(ndcg5_sum / divisor),
        no_hit_rate_at_5=_round_six(no_hit5_sum / divisor),
        top1_accuracy=_round_six(top1_sum / divisor),
        recall_at_20=_round_six(recall20_sum / divisor),
    )


def _compute_slice_delta(baseline: SliceMetrics, candidate: SliceMetrics) -> SliceMetricDelta:
    def _diff(cand: float, base: float) -> float:
        return _round_six(Decimal(str(cand)) - Decimal(str(base)))

    return SliceMetricDelta(
        recall_at_5=_diff(candidate.recall_at_5, baseline.recall_at_5),
        precision_at_5=_diff(candidate.precision_at_5, baseline.precision_at_5),
        mrr_at_5=_diff(candidate.mrr_at_5, baseline.mrr_at_5),
        ndcg_at_5=_diff(candidate.ndcg_at_5, baseline.ndcg_at_5),
        no_hit_rate_at_5=_diff(candidate.no_hit_rate_at_5, baseline.no_hit_rate_at_5),
        top1_accuracy=_diff(candidate.top1_accuracy, baseline.top1_accuracy),
        recall_at_20=_diff(candidate.recall_at_20, baseline.recall_at_20),
    )


def _ordered_slice_ids(cases: Sequence[BenchmarkCase]) -> tuple[str, ...]:
    all_slices = {slice_id for case in cases for slice_id in case.slice_ids}
    ordered: list[str] = []
    if "ALL" in all_slices:
        ordered.append("ALL")
    ordered.extend(sorted(s for s in all_slices if s.startswith("EXPRESSION_")))
    ordered.extend(sorted(s for s in all_slices if s.startswith("TOPIC_")))
    remaining = sorted(s for s in all_slices if s not in set(ordered))
    ordered.extend(remaining)
    return tuple(ordered)


def _p95_ms(latencies_ms: Sequence[float]) -> float:
    if not latencies_ms:
        return 0.0
    ordered = sorted(latencies_ms)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return round(ordered[index], 3)


def _compute_deterministic_hash(
    *,
    dataset_code: str,
    dataset_version: str,
    manifest_sha256: str,
    corpus_sha256: str,
    stage1_limit: int,
    rrf_k: int,
    trigram_threshold: float,
    reranker_algorithm_id: str,
    reranker_config_hash: str,
    baseline_rankings: Mapping[str, tuple[str, ...]],
    candidate_rankings: Mapping[str, tuple[str, ...]],
    baseline_slices: Mapping[str, SliceMetrics],
    candidate_slices: Mapping[str, SliceMetrics],
    delta_slices: Mapping[str, SliceMetricDelta],
) -> str:
    payload = {
        "dataset_code": dataset_code,
        "dataset_version": dataset_version,
        "manifest_sha256": manifest_sha256,
        "corpus_sha256": corpus_sha256,
        "stage1_limit": stage1_limit,
        "rrf_k": rrf_k,
        "trigram_threshold": trigram_threshold,
        "reranker_algorithm_id": reranker_algorithm_id,
        "reranker_config_hash": reranker_config_hash,
        "baseline_rankings": {k: list(v) for k, v in sorted(baseline_rankings.items())},
        "candidate_rankings": {k: list(v) for k, v in sorted(candidate_rankings.items())},
        "baseline_slices": {k: asdict(v) for k, v in baseline_slices.items()},
        "candidate_slices": {k: asdict(v) for k, v in candidate_slices.items()},
        "delta_slices": {k: asdict(v) for k, v in delta_slices.items()},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _evaluate_single_case(
    case: BenchmarkCase,
    prepared_corpus: Sequence[_PreparedCorpusRecord],
    record_by_id: Mapping[str, BenchmarkCorpusRecord],
    *,
    stage1_limit: int,
    rrf_k: int,
    trigram_threshold: float,
    reranker_config: RerankerConfig,
) -> tuple[RetrievalObservation, RetrievalObservation, float]:
    stage1_fused = run_stage1_offline_lexical(
        case.query,
        prepared_corpus,
        limit=stage1_limit,
        rrf_k=rrf_k,
        trigram_threshold=trigram_threshold,
    )
    baseline_ids = tuple(item.coordinate.external_document_id for item in stage1_fused)

    rerank_inputs = [
        RerankCandidateInput(
            coordinate=item.coordinate,
            content_hash=item.content_hash,
            content_text=record_by_id[item.coordinate.external_document_id].statement,
            stage1_rank=item.lexical_rank,
            stage1_score=item.score,
            product_code=record_by_id[item.coordinate.external_document_id].product_code,
            topic=record_by_id[item.coordinate.external_document_id].topic,
        )
        for item in stage1_fused
    ]

    started = time.perf_counter()
    outcome = rerank_evidence_candidates(case.query, rerank_inputs, config=reranker_config)
    latency_ms = (time.perf_counter() - started) * 1000.0

    candidate_ids = tuple(item.coordinate.external_document_id for item in outcome.results)
    baseline_obs = RetrievalObservation(
        case_id=case.case_id,
        slice_ids=case.slice_ids,
        independent_group_id=case.case_id,
        required_ids=case.required_ids,
        relevant_ids=case.relevant_ids,
        ranked_ids=baseline_ids,
    )
    candidate_obs = RetrievalObservation(
        case_id=case.case_id,
        slice_ids=case.slice_ids,
        independent_group_id=case.case_id,
        required_ids=case.required_ids,
        relevant_ids=case.relevant_ids,
        ranked_ids=candidate_ids,
    )
    return baseline_obs, candidate_obs, latency_ms


def _aggregate_slices(
    cases: Sequence[BenchmarkCase],
    baseline_observations: Sequence[RetrievalObservation],
    candidate_observations: Sequence[RetrievalObservation],
    max_input_candidates: int,
) -> tuple[dict[str, SliceMetrics], dict[str, SliceMetrics], dict[str, SliceMetricDelta]]:
    baseline_slices: dict[str, SliceMetrics] = {}
    candidate_slices: dict[str, SliceMetrics] = {}
    delta_slices: dict[str, SliceMetricDelta] = {}

    for slice_id in _ordered_slice_ids(cases):
        base_slice_obs = [obs for obs in baseline_observations if slice_id in obs.slice_ids]
        cand_slice_obs = [obs for obs in candidate_observations if slice_id in obs.slice_ids]
        cand_pool_obs = _slice_observation_pool(base_slice_obs, max_input_candidates)
        base_metrics = _compute_slice_metrics(base_slice_obs, base_slice_obs)
        cand_metrics = _compute_slice_metrics(cand_slice_obs, cand_pool_obs)
        baseline_slices[slice_id] = base_metrics
        candidate_slices[slice_id] = cand_metrics
        delta_slices[slice_id] = _compute_slice_delta(base_metrics, cand_metrics)

    return baseline_slices, candidate_slices, delta_slices


def run_rerank_benchmark(
    *,
    evals_root: Path = Path("evals"),
    reranker_config: RerankerConfig | None = None,
    stage1_limit: int = DEFAULT_STAGE1_LIMIT,
    rrf_k: int = DEFAULT_RRF_K,
    trigram_threshold: float = DEFAULT_TRIGRAM_THRESHOLD,
) -> RerankBenchmarkReport:
    _validate_positive_int("stage1_limit", stage1_limit)
    _validate_positive_int("rrf_k", rrf_k)

    effective_config = reranker_config if reranker_config is not None else RerankerConfig()
    probe = rerank_evidence_candidates("config-probe", (), config=effective_config)
    algorithm_id = probe.algorithm_id
    config_hash = probe.config_hash

    dataset = load_dev_benchmark_dataset(evals_root)
    prepared_corpus = _prepare_corpus(dataset.corpus)
    record_by_id = {rec.evidence_ref_id: rec for rec in dataset.corpus}

    baseline_observations: list[RetrievalObservation] = []
    candidate_observations: list[RetrievalObservation] = []
    latencies_ms: list[float] = []

    for case in dataset.cases:
        base_obs, cand_obs, latency_ms = _evaluate_single_case(
            case,
            prepared_corpus,
            record_by_id,
            stage1_limit=stage1_limit,
            rrf_k=rrf_k,
            trigram_threshold=trigram_threshold,
            reranker_config=effective_config,
        )
        baseline_observations.append(base_obs)
        candidate_observations.append(cand_obs)
        latencies_ms.append(latency_ms)

    baseline_slices, candidate_slices, delta_slices = _aggregate_slices(
        dataset.cases,
        baseline_observations,
        candidate_observations,
        effective_config.max_input_candidates,
    )

    result_sha256 = _compute_deterministic_hash(
        dataset_code=dataset.dataset_code,
        dataset_version=dataset.dataset_version,
        manifest_sha256=dataset.manifest_sha256,
        corpus_sha256=dataset.corpus_sha256,
        stage1_limit=stage1_limit,
        rrf_k=rrf_k,
        trigram_threshold=trigram_threshold,
        reranker_algorithm_id=algorithm_id,
        reranker_config_hash=config_hash,
        baseline_rankings={obs.case_id: obs.ranked_ids for obs in baseline_observations},
        candidate_rankings={obs.case_id: obs.ranked_ids for obs in candidate_observations},
        baseline_slices=baseline_slices,
        candidate_slices=candidate_slices,
        delta_slices=delta_slices,
    )

    return RerankBenchmarkReport(
        benchmark_scope=BENCHMARK_SCOPE,
        release_eligible=False,
        holdout_performed=False,
        dataset_code=dataset.dataset_code,
        dataset_version=dataset.dataset_version,
        manifest_sha256=dataset.manifest_sha256,
        corpus_sha256=dataset.corpus_sha256,
        case_count=len(dataset.cases),
        corpus_record_count=len(dataset.corpus),
        baseline_variant_id=BASELINE_VARIANT_ID,
        candidate_variant_id=CANDIDATE_VARIANT_ID,
        stage1_limit=stage1_limit,
        rrf_k=rrf_k,
        trigram_threshold=trigram_threshold,
        reranker_algorithm_id=algorithm_id,
        reranker_config_hash=config_hash,
        deterministic_result_sha256=result_sha256,
        median_rerank_latency_ms=round(statistics.median(latencies_ms), 3) if latencies_ms else 0.0,
        p95_rerank_latency_ms=_p95_ms(latencies_ms),
        baseline_slices=baseline_slices,
        candidate_slices=candidate_slices,
        delta_slices=delta_slices,
    )


def format_report_json(report: RerankBenchmarkReport) -> str:
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n"


def _fmt_delta(value: float) -> str:
    return f"{value:+.4f}"


def format_report_markdown(report: RerankBenchmarkReport) -> str:
    lines: list[str] = [
        "# Offline Korean Retrieval Reranker Benchmark",
        "",
        f"- **Benchmark Scope**: `{report.benchmark_scope}`",
        f"- **Release Eligible**: `{report.release_eligible}`",
        f"- **Holdout Performed**: `{report.holdout_performed}`",
        f"- **Dataset**: `{report.dataset_code}` v`{report.dataset_version}` ({report.case_count} cases / {report.corpus_record_count} records)",
        f"- **Manifest SHA-256**: `{report.manifest_sha256}`",
        f"- **Corpus SHA-256**: `{report.corpus_sha256}`",
        f"- **Variants**: Baseline `{report.baseline_variant_id}` (top-{report.stage1_limit}, rrf_k={report.rrf_k}, trigram>={report.trigram_threshold}) vs Candidate `{report.candidate_variant_id}`",
        f"- **Reranker**: `{report.reranker_algorithm_id}` (config hash `{report.reranker_config_hash}`)",
        f"- **Deterministic Result SHA-256**: `{report.deterministic_result_sha256}`",
        f"- **Rerank Latency (ms)**: median `{report.median_rerank_latency_ms:.3f}` ms, p95 `{report.p95_rerank_latency_ms:.3f}` ms",
        "",
        "## Per-Slice Metrics (k=5, Evaluated Pool Recall@20)",
        "",
        "| Slice | N | Base R@20 | Cand R@20 | Base R@5 | Cand R@5 | Δ R@5 | Base MRR@5 | Cand MRR@5 | Δ MRR@5 | Base NDCG@5 | Cand NDCG@5 | Base Top1 | Cand Top1 | Base NoHit | Cand NoHit |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for slice_id, base in report.baseline_slices.items():
        cand = report.candidate_slices[slice_id]
        delta = report.delta_slices[slice_id]
        lines.append(
            f"| `{slice_id}` | {base.case_count} | {base.recall_at_20:.4f} | {cand.recall_at_20:.4f} | "
            f"{base.recall_at_5:.4f} | {cand.recall_at_5:.4f} | {_fmt_delta(delta.recall_at_5)} | "
            f"{base.mrr_at_5:.4f} | {cand.mrr_at_5:.4f} | {_fmt_delta(delta.mrr_at_5)} | "
            f"{base.ndcg_at_5:.4f} | {cand.ndcg_at_5:.4f} | "
            f"{base.top1_accuracy:.4f} | {cand.top1_accuracy:.4f} | "
            f"{base.no_hit_rate_at_5:.4f} | {cand.no_hit_rate_at_5:.4f} |"
        )
    lines.extend(
        [
            "",
            "## Limitations",
            "",
            "- Stage-1 `RET-L-OFFLINE` is an in-memory Python lexical approximation, not the real PostgreSQL/pgvector runtime.",
            "- No 95% cluster-bootstrap confidence intervals are computed in this lightweight offline benchmark.",
            "- Metrics on the 100-record synthetic DEV corpus are `DEV_DIAGNOSTIC_ONLY` and do not establish real MFDS/eDrug, HOLDOUT, or clinical readiness.",
            "",
        ]
    )
    return "\n".join(lines)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Dosey offline 60-case Korean retrieval reranker benchmark (DEV_DIAGNOSTIC_ONLY)."
    )
    parser.add_argument(
        "--evals-root",
        type=Path,
        default=Path("evals"),
        help="Path to the evals directory (default: evals).",
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
        dest="output_format",
        help="Report output format (json or markdown).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional output file path (writes to stdout when omitted).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    try:
        report = run_rerank_benchmark(evals_root=args.evals_root)
        rendered = format_report_json(report) if args.output_format == "json" else format_report_markdown(report)
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"rerank_benchmark error: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
