"""Offline-testable local NLI citation smoke runner (LOCAL_NLI_SMOKE_ONLY).

Measures model load time, per-pair inference P50/P95 latency, peak RSS memory,
and 3-class canonical label output (`support` / `contradict` / `neutral`) on
synthetic Korean Claim-evidence pairs. Supports an injectable offline scorer
for zero-network unit tests and fails closed when `[dependency-groups] ai`
(`torch`, `sentence-transformers`) is not installed.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import importlib.util
import json
import math
import re
import resource
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

SMOKE_SCOPE = "LOCAL_NLI_SMOKE_ONLY"
DEFAULT_NLI_MODEL_NAME = "MoritzLaurer/mDeBERTa-v3-base-mnli-xnli"
DEFAULT_DEVICE = "cpu"
DEFAULT_MAX_TOKENS = 512

Id2LabelMap = Mapping[int, str] | Mapping[str, str] | Mapping[int | str, str]
RawScoreMap = Mapping[str, float] | Mapping[int, float] | Mapping[str | int, float]
LabelMapping = Mapping[str, "CanonicalNliLabel | str"]

_SENTENCE_SPLIT_RE = re.compile(r"(?<=(?<!\d)\.)\s+|(?<=[!?。！？…])\s+|\n+")
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[,;:])\s+")
_WHITESPACE_RE = re.compile(r"\s+")
_TOKEN_ESTIMATE_RE = re.compile(r"[가-힣]|[0-9a-zA-Z]+|[^\s\w]")
_LABEL_INDEX_RE = re.compile(r"^(?:label_)?(\d+)$", re.IGNORECASE)


class CanonicalNliLabel(StrEnum):
    """Canonical 3-class citation NLI verdict."""

    SUPPORT = "support"
    CONTRADICT = "contradict"
    NEUTRAL = "neutral"


CONSERVATIVE_LABEL_PRIORITY: tuple[CanonicalNliLabel, ...] = (
    CanonicalNliLabel.CONTRADICT,
    CanonicalNliLabel.NEUTRAL,
    CanonicalNliLabel.SUPPORT,
)

_LABEL_SYNONYMS: dict[str, CanonicalNliLabel] = {
    "entailment": CanonicalNliLabel.SUPPORT,
    "entails": CanonicalNliLabel.SUPPORT,
    "entailed": CanonicalNliLabel.SUPPORT,
    "support": CanonicalNliLabel.SUPPORT,
    "supports": CanonicalNliLabel.SUPPORT,
    "supported": CanonicalNliLabel.SUPPORT,
    "contradiction": CanonicalNliLabel.CONTRADICT,
    "contradict": CanonicalNliLabel.CONTRADICT,
    "contradicts": CanonicalNliLabel.CONTRADICT,
    "contradicted": CanonicalNliLabel.CONTRADICT,
    "neutral": CanonicalNliLabel.NEUTRAL,
    "neither": CanonicalNliLabel.NEUTRAL,
}


@runtime_checkable
class NliPairScorer(Protocol):
    """Injectable NLI pair scorer protocol for offline testing and live execution."""

    def score_pairs(self, pairs: Sequence[tuple[str, str]]) -> Sequence[RawScoreMap]:
        """Score `(claim, evidence)` pairs and return raw per-label score mappings."""


NliScorerInput = NliPairScorer | Callable[[str, str], RawScoreMap]


@dataclass(frozen=True, slots=True)
class SyntheticNliSampleCase:
    """Synthetic Korean Claim-evidence pair for local NLI smoke checks."""

    case_id: str
    claim: str
    evidence: str
    expected_hint: CanonicalNliLabel


@dataclass(frozen=True, slots=True)
class DependencyStatus:
    """Installed status of `[dependency-groups] ai` runtime packages."""

    torch_installed: bool
    torch_version: str | None
    sentence_transformers_installed: bool
    sentence_transformers_version: str | None
    transformers_installed: bool
    transformers_version: str | None
    ai_group_ready: bool
    missing_packages: tuple[str, ...]
    install_hint: str


@dataclass(frozen=True, slots=True)
class PairSmokeResult:
    """Aggregated NLI inference result for one Claim-evidence pair."""

    case_id: str
    claim: str
    evidence_char_count: int
    evidence_estimated_tokens: int
    expected_hint: str | None
    predicted_label: str
    class_scores: dict[str, float]
    chunk_count: int
    selected_chunk_index: int
    selected_chunk_text: str
    latency_ms: float


@dataclass(frozen=True, slots=True)
class CitationNliSmokeReport:
    """Structured smoke report payload for local NLI evaluation."""

    smoke_scope: str
    release_eligible: bool
    model_name: str
    device: str
    max_tokens: int
    max_chars: int | None
    dependency_status: DependencyStatus
    load_seconds: float
    latency_ms_p50: float
    latency_ms_p95: float
    peak_rss_mb: float
    model_cache_size_mb: float | None
    sample_count: int
    sample_results: tuple[PairSmokeResult, ...]
    git_commit: str
    command: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _long_korean_support_evidence() -> str:
    """Return synthetic >512-token Korean medication label text for chunking smoke."""
    filler_sentences = [
        "이 약은 흰색 또는 미황색의 원형 필름코팅정제로서 직사광선을 피하여 실온의 건조한 장소에 보관하십시오.",
        "어린이의 손이 닿지 않는 곳에 원래의 포장 용기 그대로 밀폐하여 보관하고 다른 용기에 옮겨 담지 마십시오.",
        "포장 표면에 표시된 유효기간이 지난 제품은 약국이나 보건소의 폐의약품 수거함을 통해 안전하게 폐기하십시오.",
        "첨가제로 유당수화물, 미결정셀룰로오스, 스테아르산마그네슘, 히프로멜로오스 등이 포함되어 있습니다.",
        "습기가 많은 욕실이나 고온의 차량 내부에 장기간 보관할 경우 정제의 성상이 변질될 우려가 있으므로 주의하십시오.",
        "외관상 변색, 균열 또는 붕해가 관찰되는 경우에는 복용하지 말고 전문가에게 문의하여 확인하시기 바랍니다.",
    ]
    repeated_block = " ".join(filler_sentences * 4)
    key_sentence = (
        "성인은 1회 1정을 하루 3회 식사 직후 충분한 물과 함께 복용하며 위장 부담을 줄이기 위해 공복 복용은 피하십시오."
    )
    return f"{repeated_block} {key_sentence}"


DEFAULT_SYNTHETIC_SAMPLE_PAIRS: tuple[SyntheticNliSampleCase, ...] = (
    SyntheticNliSampleCase(
        case_id="nli-smoke-01-support",
        claim="이 약은 식후 30분에 물과 함께 복용하는 것이 좋습니다.",
        evidence="성인 기준 1일 3회, 1회 1정을 식후 30분에 충분한 물과 함께 복용하십시오.",
        expected_hint=CanonicalNliLabel.SUPPORT,
    ),
    SyntheticNliSampleCase(
        case_id="nli-smoke-02-contradict",
        claim="임산부는 의사나 약사의 상담 없이도 이 약을 임의로 복용할 수 있습니다.",
        evidence="임부 또는 임신하고 있을 가능성이 있는 여성은 이 약을 복용하기 전에 반드시 의사 또는 약사와 상의하십시오.",
        expected_hint=CanonicalNliLabel.CONTRADICT,
    ),
    SyntheticNliSampleCase(
        case_id="nli-smoke-03-neutral",
        claim="이 약을 복용하는 동안에는 카페인 음료 섭취를 제한하는 편이 좋습니다.",
        evidence="직사광선을 피하고 습기가 적은 서늘한 곳에 밀폐하여 보관하십시오.",
        expected_hint=CanonicalNliLabel.NEUTRAL,
    ),
    SyntheticNliSampleCase(
        case_id="nli-smoke-04-long-support",
        claim="성인은 하루 세 번 식사 직후 물과 함께 한 알씩 복용합니다.",
        evidence=_long_korean_support_evidence(),
        expected_hint=CanonicalNliLabel.SUPPORT,
    ),
    SyntheticNliSampleCase(
        case_id="nli-smoke-05-contradict-dose",
        claim="증상이 심할 때는 하루 최대 6정까지 한 번에 늘려 복용해도 됩니다.",
        evidence="1일 최대 복용량은 3정을 초과하지 마십시오. 정해진 용법과 용량을 준수하십시오.",
        expected_hint=CanonicalNliLabel.CONTRADICT,
    ),
)


def _validate_positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _lookup_id2label(
    index_key: int,
    id2label: Id2LabelMap,
) -> str | None:
    str_key = str(index_key)
    lower_map = {str(k).strip().lower(): str(v) for k, v in id2label.items()}
    return lower_map.get(str_key) or lower_map.get(f"label_{index_key}")


def _resolve_custom_label(raw_label: str | int, label_mapping: LabelMapping | None) -> CanonicalNliLabel | None:
    if label_mapping is None:
        return None
    normalized_custom = {str(k).strip().lower(): v for k, v in label_mapping.items()}
    custom_hit = normalized_custom.get(str(raw_label).strip().lower())
    if custom_hit is None:
        return None
    return normalize_nli_label(custom_hit)


def normalize_nli_label(
    raw_label: str | int,
    *,
    id2label: Id2LabelMap | None = None,
    label_mapping: LabelMapping | None = None,
) -> CanonicalNliLabel:
    """Normalize model-specific NLI labels into `support` / `contradict` / `neutral`."""
    if isinstance(raw_label, CanonicalNliLabel):
        return raw_label

    custom_hit = _resolve_custom_label(raw_label, label_mapping)
    if custom_hit is not None:
        return custom_hit

    if isinstance(raw_label, bool):
        raise ValueError(f"Unsupported NLI label: {raw_label!r}")

    if isinstance(raw_label, int):
        resolved = _lookup_id2label(raw_label, id2label) if id2label is not None else None
        if resolved is None:
            raise ValueError(f"Numeric NLI label {raw_label!r} requires a matching id2label entry")
        return normalize_nli_label(resolved, label_mapping=label_mapping)

    cleaned = _WHITESPACE_RE.sub(" ", str(raw_label).strip()).lower()
    if cleaned in _LABEL_SYNONYMS:
        return _LABEL_SYNONYMS[cleaned]

    index_match = _LABEL_INDEX_RE.match(cleaned)
    if index_match is not None and id2label is not None:
        resolved = _lookup_id2label(int(index_match.group(1)), id2label)
        if resolved is not None:
            return normalize_nli_label(resolved, label_mapping=label_mapping)

    raise ValueError(f"Unsupported NLI label: {raw_label!r}")


def _apply_softmax(values: Sequence[float]) -> list[float]:
    if not values:
        return []
    max_val = max(values)
    exps = [math.exp(val - max_val) for val in values]
    total = sum(exps)
    return [val / total for val in exps] if total > 0.0 else [0.0 for _ in values]


def normalize_score_map(
    raw_scores: RawScoreMap,
    *,
    id2label: Id2LabelMap | None = None,
    label_mapping: LabelMapping | None = None,
    apply_softmax: bool | None = None,
) -> dict[CanonicalNliLabel, float]:
    """Map raw label scores to canonical 3-class probabilities."""
    if not raw_scores:
        raise ValueError("NLI scorer returned an empty score mapping")

    items: list[tuple[str | int, float]] = [(key, float(value)) for key, value in raw_scores.items()]
    raw_values = [score for _, score in items]
    needs_softmax = apply_softmax if apply_softmax is not None else any(v < 0.0 or v > 1.0 for v in raw_values)
    probs = _apply_softmax(raw_values) if needs_softmax else raw_values

    normalized: dict[CanonicalNliLabel, float] = {label: 0.0 for label in CanonicalNliLabel}
    for (key, _), score in zip(items, probs, strict=True):
        canonical = normalize_nli_label(key, id2label=id2label, label_mapping=label_mapping)
        normalized[canonical] = max(normalized[canonical], float(score))
    return normalized


def estimate_token_count(text: str) -> int:
    """Conservatively estimate subword token count for Korean/mixed text offline."""
    if not text or not text.strip():
        return 0
    return len(_TOKEN_ESTIMATE_RE.findall(text))


def split_sentences(text: str) -> tuple[str, ...]:
    """Split Korean/mixed evidence text into non-empty sentences."""
    stripped = text.strip()
    if not stripped:
        return ()
    parts = [part.strip() for part in _SENTENCE_SPLIT_RE.split(stripped) if part.strip()]
    return tuple(parts)


def _fits_budget(
    text: str,
    *,
    max_tokens: int,
    max_chars: int | None,
    token_counter: Callable[[str], int],
) -> bool:
    if max_chars is not None and len(text) > max_chars:
        return False
    return token_counter(text) <= max_tokens


def _hard_slice_text(
    text: str,
    *,
    max_tokens: int,
    max_chars: int | None,
    token_counter: Callable[[str], int],
) -> list[str]:
    chunks: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        step = max_chars if max_chars is not None else max_tokens
        end = min(length, max(start + 1, start + step))
        while end > start + 1 and not _fits_budget(
            text[start:end],
            max_tokens=max_tokens,
            max_chars=max_chars,
            token_counter=token_counter,
        ):
            end = start + max(1, (end - start) // 2)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end
    return chunks


def _pack_units(
    units: Sequence[str],
    *,
    max_tokens: int,
    max_chars: int | None,
    token_counter: Callable[[str], int],
) -> list[str]:
    chunks: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current} {unit}".strip() if current else unit
        if _fits_budget(candidate, max_tokens=max_tokens, max_chars=max_chars, token_counter=token_counter):
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if _fits_budget(unit, max_tokens=max_tokens, max_chars=max_chars, token_counter=token_counter):
            current = unit
        else:
            chunks.extend(
                _hard_slice_text(unit, max_tokens=max_tokens, max_chars=max_chars, token_counter=token_counter)
            )
    if current:
        chunks.append(current)
    return chunks


def _split_oversized_sentence(
    sentence: str,
    *,
    max_tokens: int,
    max_chars: int | None,
    token_counter: Callable[[str], int],
) -> tuple[str, ...]:
    if _fits_budget(sentence, max_tokens=max_tokens, max_chars=max_chars, token_counter=token_counter):
        return (sentence,)

    clauses = [part.strip() for part in _CLAUSE_SPLIT_RE.split(sentence) if part.strip()]
    if len(clauses) > 1 and all(
        _fits_budget(clause, max_tokens=max_tokens, max_chars=max_chars, token_counter=token_counter)
        for clause in clauses
    ):
        return tuple(_pack_units(clauses, max_tokens=max_tokens, max_chars=max_chars, token_counter=token_counter))

    words = sentence.split()
    if len(words) > 1:
        return tuple(_pack_units(words, max_tokens=max_tokens, max_chars=max_chars, token_counter=token_counter))

    return tuple(_hard_slice_text(sentence, max_tokens=max_tokens, max_chars=max_chars, token_counter=token_counter))


def split_evidence_for_budget(
    evidence: str,
    *,
    claim: str = "",
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_chars: int | None = None,
    token_counter: Callable[[str], int] | None = None,
) -> tuple[str, ...]:
    """Split evidence by sentence when `(claim, evidence)` exceeds the length budget."""
    _validate_positive_int("max_tokens", max_tokens)
    if max_chars is not None:
        _validate_positive_int("max_chars", max_chars)

    stripped_evidence = evidence.strip()
    if not stripped_evidence:
        return ()

    counter = token_counter if token_counter is not None else estimate_token_count
    stripped_claim = claim.strip()
    combined = f"{stripped_claim} {stripped_evidence}".strip() if stripped_claim else stripped_evidence

    if _fits_budget(combined, max_tokens=max_tokens, max_chars=None, token_counter=counter) and (
        max_chars is None or len(stripped_evidence) <= max_chars
    ):
        return (stripped_evidence,)

    claim_tokens = counter(stripped_claim) if stripped_claim else 0
    chunk_token_budget = max(1, max_tokens - claim_tokens)

    chunks: list[str] = []
    for sentence in split_sentences(stripped_evidence):
        chunks.extend(
            _split_oversized_sentence(
                sentence,
                max_tokens=chunk_token_budget,
                max_chars=max_chars,
                token_counter=counter,
            )
        )
    return tuple(chunks)


def summarize_latencies_ms(latencies_ms: Sequence[float]) -> tuple[float, float]:
    """Return `(p50_ms, p95_ms)` rounded to 3 decimal places."""
    if not latencies_ms:
        return 0.0, 0.0
    ordered = sorted(float(val) for val in latencies_ms)
    p50 = round(statistics.median(ordered), 3)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    p95 = round(ordered[p95_index], 3)
    return p50, p95


def get_peak_rss_mb() -> float:
    """Return current process peak resident set size in megabytes."""
    raw_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = (1024 * 1024) if sys.platform == "darwin" else 1024
    return round(raw_rss / divisor, 2)


def _installed_package_version(package_name: str) -> tuple[bool, str | None]:
    if importlib.util.find_spec(package_name) is None:
        return False, None
    try:
        return True, importlib.metadata.version(package_name)
    except importlib.metadata.PackageNotFoundError:
        return True, None


def probe_ai_dependencies() -> DependencyStatus:
    """Inspect whether `[dependency-groups] ai` packages are available in `.venv`."""
    torch_ok, torch_ver = _installed_package_version("torch")
    st_ok, st_ver = _installed_package_version("sentence_transformers")
    tf_ok, tf_ver = _installed_package_version("transformers")

    missing: list[str] = []
    if not torch_ok:
        missing.append("torch")
    if not st_ok:
        missing.append("sentence-transformers")

    return DependencyStatus(
        torch_installed=torch_ok,
        torch_version=torch_ver,
        sentence_transformers_installed=st_ok,
        sentence_transformers_version=st_ver,
        transformers_installed=tf_ok,
        transformers_version=tf_ver,
        ai_group_ready=torch_ok and st_ok,
        missing_packages=tuple(missing),
        install_hint="uv sync --group ai",
    )


def _extract_model_id2label(model: Any) -> dict[int | str, str] | None:
    for candidate in (
        getattr(getattr(model, "model", None), "config", None),
        getattr(model, "config", None),
    ):
        raw_map = getattr(candidate, "id2label", None)
        if isinstance(raw_map, Mapping) and raw_map:
            return {k: str(v) for k, v in raw_map.items()}
    return None


def _estimate_hf_cache_size_mb(model_name: str) -> float | None:
    try:
        hub_module = importlib.import_module("huggingface_hub")
    except ImportError:
        return None
    scan_cache_dir = getattr(hub_module, "scan_cache_dir", None)
    if scan_cache_dir is None:
        return None
    try:
        cache_info = scan_cache_dir()
    except OSError:
        return None
    for repo in cache_info.repos:
        if repo.repo_id == model_name:
            return round(repo.size_on_disk / (1024 * 1024), 2)
    return None


class HuggingFaceCrossEncoderNliScorer:
    """Lazy-loading `sentence_transformers.CrossEncoder` NLI scorer."""

    def __init__(
        self,
        *,
        model_name: str = DEFAULT_NLI_MODEL_NAME,
        device: str = DEFAULT_DEVICE,
    ) -> None:
        try:
            torch_module = importlib.import_module("torch")
            st_module = importlib.import_module("sentence_transformers")
            cross_encoder_cls = st_module.CrossEncoder
        except (ImportError, AttributeError) as exc:
            raise RuntimeError(
                "Missing required AI dependencies (torch, sentence-transformers) for live NLI smoke execution. "
                "Install `[dependency-groups] ai` in `.venv` via: uv sync --group ai"
            ) from exc

        resolved_device = _resolve_torch_device(torch_module, device)
        self.model_name = model_name
        self.device = resolved_device
        started = time.perf_counter()
        self._model = cross_encoder_cls(model_name, device=resolved_device)
        self.load_seconds = round(time.perf_counter() - started, 3)
        self.id2label: dict[int | str, str] | None = _extract_model_id2label(self._model)
        self.model_cache_size_mb: float | None = _estimate_hf_cache_size_mb(model_name)

    def count_tokens(self, text: str) -> int:
        tokenizer = getattr(self._model, "tokenizer", None)
        if tokenizer is None:
            return estimate_token_count(text)
        encoded = tokenizer.encode(text, add_special_tokens=False)
        return len(encoded)

    def score_pairs(self, pairs: Sequence[tuple[str, str]]) -> Sequence[RawScoreMap]:
        if not pairs:
            return ()
        raw_predictions = self._model.predict(list(pairs), show_progress_bar=False)
        results: list[dict[int, float]] = []
        for row in raw_predictions:
            values = row.tolist() if hasattr(row, "tolist") else list(row)
            results.append({idx: float(score) for idx, score in enumerate(values)})
        return results


def _resolve_torch_device(torch_module: Any, requested_device: str) -> str:
    cleaned = requested_device.strip().lower()
    if cleaned != "auto":
        return cleaned
    if getattr(torch_module, "cuda", None) is not None and torch_module.cuda.is_available():
        return "cuda"
    mps_backend = getattr(getattr(torch_module, "backends", None), "mps", None)
    if mps_backend is not None and mps_backend.is_available():
        return "mps"
    return "cpu"


def load_huggingface_nli_scorer(
    *,
    model_name: str = DEFAULT_NLI_MODEL_NAME,
    device: str = DEFAULT_DEVICE,
) -> HuggingFaceCrossEncoderNliScorer:
    """Load the HuggingFace CrossEncoder NLI scorer or raise an actionable `RuntimeError`."""
    return HuggingFaceCrossEncoderNliScorer(model_name=model_name, device=device)


class _CallableScorerAdapter:
    """Wrap a single-pair `(claim, evidence) -> scores` callable as an `NliPairScorer`."""

    def __init__(self, fn: Callable[[str, str], RawScoreMap]) -> None:
        self._fn = fn
        self.id2label: Id2LabelMap | None = getattr(fn, "id2label", None)

    def score_pairs(self, pairs: Sequence[tuple[str, str]]) -> Sequence[RawScoreMap]:
        return [self._fn(claim, evidence) for claim, evidence in pairs]


def _coerce_scorer(scorer: NliScorerInput) -> NliPairScorer:
    if isinstance(scorer, NliPairScorer):
        return scorer
    if callable(scorer):
        return _CallableScorerAdapter(scorer)
    raise TypeError("scorer must implement `score_pairs(pairs)` or be callable `(claim, evidence) -> scores`")


def _select_conservative_label(aggregated_scores: Mapping[CanonicalNliLabel, float]) -> CanonicalNliLabel:
    best_score = max(aggregated_scores.values())
    for label in CONSERVATIVE_LABEL_PRIORITY:
        if aggregated_scores[label] == best_score:
            return label
    return CanonicalNliLabel.NEUTRAL


def score_claim_evidence_pair(
    *,
    case_id: str,
    claim: str,
    evidence: str,
    scorer: NliScorerInput,
    expected_hint: CanonicalNliLabel | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_chars: int | None = None,
    id2label: Id2LabelMap | None = None,
    label_mapping: LabelMapping | None = None,
    token_counter: Callable[[str], int] | None = None,
) -> PairSmokeResult:
    """Evaluate one `(claim, evidence)` pair with sentence splitting and max-score aggregation."""
    pair_scorer = _coerce_scorer(scorer)
    effective_id2label = id2label if id2label is not None else getattr(pair_scorer, "id2label", None)
    effective_counter = token_counter if token_counter is not None else getattr(pair_scorer, "count_tokens", None)

    chunks = split_evidence_for_budget(
        evidence,
        claim=claim,
        max_tokens=max_tokens,
        max_chars=max_chars,
        token_counter=effective_counter,
    )
    if not chunks:
        raise ValueError(f"Case {case_id!r} has empty evidence text")

    started = time.perf_counter()
    raw_chunk_scores = pair_scorer.score_pairs([(claim, chunk) for chunk in chunks])
    latency_ms = round((time.perf_counter() - started) * 1000.0, 3)

    if len(raw_chunk_scores) != len(chunks):
        raise ValueError(f"Scorer returned {len(raw_chunk_scores)} score rows for {len(chunks)} evidence chunks")

    normalized_chunks = [
        normalize_score_map(row, id2label=effective_id2label, label_mapping=label_mapping) for row in raw_chunk_scores
    ]
    aggregated = {label: max(chunk_scores[label] for chunk_scores in normalized_chunks) for label in CanonicalNliLabel}
    predicted_label = _select_conservative_label(aggregated)

    selected_chunk_index = max(
        range(len(chunks)),
        key=lambda idx: (normalized_chunks[idx][predicted_label], -idx),
    )

    return PairSmokeResult(
        case_id=case_id,
        claim=claim,
        evidence_char_count=len(evidence),
        evidence_estimated_tokens=estimate_token_count(evidence),
        expected_hint=expected_hint.value if expected_hint is not None else None,
        predicted_label=predicted_label.value,
        class_scores={label.value: round(aggregated[label], 6) for label in CanonicalNliLabel},
        chunk_count=len(chunks),
        selected_chunk_index=selected_chunk_index,
        selected_chunk_text=chunks[selected_chunk_index],
        latency_ms=latency_ms,
    )


def _resolve_git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _default_command_string() -> str:
    if len(sys.argv) <= 1:
        return "python -m ai_worker.tasks.evaluation.citation_nli_smoke"
    return " ".join(["python -m ai_worker.tasks.evaluation.citation_nli_smoke", *sys.argv[1:]])


def run_citation_nli_smoke(
    *,
    model_name: str = DEFAULT_NLI_MODEL_NAME,
    device: str = DEFAULT_DEVICE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_chars: int | None = None,
    sample_cases: Sequence[SyntheticNliSampleCase] = DEFAULT_SYNTHETIC_SAMPLE_PAIRS,
    scorer: NliScorerInput | None = None,
    id2label: Id2LabelMap | None = None,
    label_mapping: LabelMapping | None = None,
    token_counter: Callable[[str], int] | None = None,
    command: str | None = None,
) -> CitationNliSmokeReport:
    """Execute the NLI citation smoke check and build a structured report."""
    _validate_positive_int("max_tokens", max_tokens)
    if max_chars is not None:
        _validate_positive_int("max_chars", max_chars)

    dependency_status = probe_ai_dependencies()

    load_seconds = 0.0
    model_cache_size_mb: float | None = None
    effective_device = device
    active_scorer: NliScorerInput

    if scorer is not None:
        active_scorer = scorer
        if device == DEFAULT_DEVICE:
            effective_device = str(getattr(scorer, "device", "offline-injected"))
    else:
        hf_scorer = load_huggingface_nli_scorer(model_name=model_name, device=device)
        active_scorer = hf_scorer
        load_seconds = hf_scorer.load_seconds
        model_cache_size_mb = hf_scorer.model_cache_size_mb
        effective_device = hf_scorer.device

    results: list[PairSmokeResult] = []
    for case in sample_cases:
        results.append(
            score_claim_evidence_pair(
                case_id=case.case_id,
                claim=case.claim,
                evidence=case.evidence,
                scorer=active_scorer,
                expected_hint=case.expected_hint,
                max_tokens=max_tokens,
                max_chars=max_chars,
                id2label=id2label,
                label_mapping=label_mapping,
                token_counter=token_counter,
            )
        )

    p50_ms, p95_ms = summarize_latencies_ms([item.latency_ms for item in results])

    return CitationNliSmokeReport(
        smoke_scope=SMOKE_SCOPE,
        release_eligible=False,
        model_name=model_name,
        device=effective_device,
        max_tokens=max_tokens,
        max_chars=max_chars,
        dependency_status=dependency_status,
        load_seconds=load_seconds,
        latency_ms_p50=p50_ms,
        latency_ms_p95=p95_ms,
        peak_rss_mb=get_peak_rss_mb(),
        model_cache_size_mb=model_cache_size_mb,
        sample_count=len(results),
        sample_results=tuple(results),
        git_commit=_resolve_git_commit(),
        command=command if command is not None else _default_command_string(),
    )


def format_report_json(report: CitationNliSmokeReport) -> str:
    """Serialize `CitationNliSmokeReport` as indented UTF-8 JSON."""
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n"


def format_report_markdown(report: CitationNliSmokeReport) -> str:
    """Render `CitationNliSmokeReport` as a concise Markdown summary."""
    dep = report.dependency_status
    cache_text = f"{report.model_cache_size_mb:.2f} MB" if report.model_cache_size_mb is not None else "N/A"
    lines: list[str] = [
        "# Local NLI Citation Smoke Report",
        "",
        f"- **Smoke Scope**: `{report.smoke_scope}`",
        f"- **Release Eligible**: `{report.release_eligible}`",
        f"- **Model**: `{report.model_name}`",
        f"- **Device**: `{report.device}`",
        f"- **Chunk Budget**: `max_tokens={report.max_tokens}`, `max_chars={report.max_chars}`",
        (
            f"- **Dependency Status**: `ai_group_ready={dep.ai_group_ready}` "
            f"(torch={dep.torch_version or 'missing'}, "
            f"sentence-transformers={dep.sentence_transformers_version or 'missing'})"
        ),
        f"- **Load Seconds**: `{report.load_seconds:.3f}` s",
        f"- **Latency (ms)**: P50 `{report.latency_ms_p50:.3f}` ms, P95 `{report.latency_ms_p95:.3f}` ms",
        f"- **Peak RSS**: `{report.peak_rss_mb:.2f}` MB",
        f"- **Model Cache Size**: `{cache_text}`",
        f"- **Git Commit**: `{report.git_commit}`",
        f"- **Command**: `{report.command}`",
        "",
        "## Synthetic Korean Claim-Evidence Pairs",
        "",
        "| Case ID | Expected Hint | Predicted | Support | Contradict | Neutral | Chunks | Selected Chunk | Latency (ms) |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in report.sample_results:
        lines.append(
            f"| `{item.case_id}` | `{item.expected_hint or '-'}` | `{item.predicted_label}` | "
            f"{item.class_scores['support']:.4f} | {item.class_scores['contradict']:.4f} | "
            f"{item.class_scores['neutral']:.4f} | {item.chunk_count} | "
            f"{item.selected_chunk_index} | {item.latency_ms:.3f} |"
        )
    lines.append("")
    return "\n".join(lines)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run local NLI citation smoke check on synthetic Korean Claim-evidence pairs."
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_NLI_MODEL_NAME,
        help=f"HuggingFace NLI CrossEncoder model ID (default: {DEFAULT_NLI_MODEL_NAME}).",
    )
    parser.add_argument(
        "--device",
        default=DEFAULT_DEVICE,
        help=f"Torch inference device: cpu, mps, cuda, or auto (default: {DEFAULT_DEVICE}).",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help=f"Maximum token budget per pair before sentence splitting (default: {DEFAULT_MAX_TOKENS}).",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=None,
        help="Optional maximum evidence character budget before sentence splitting.",
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


def main(
    argv: Sequence[str] | None = None,
    *,
    scorer: NliScorerInput | None = None,
) -> int:
    args = _build_arg_parser().parse_args(argv)
    cli_command = (
        " ".join(["python -m ai_worker.tasks.evaluation.citation_nli_smoke", *argv])
        if argv is not None
        else _default_command_string()
    )
    try:
        report = run_citation_nli_smoke(
            model_name=args.model,
            device=args.device,
            max_tokens=args.max_tokens,
            max_chars=args.max_chars,
            scorer=scorer,
            command=cli_command,
        )
        rendered = format_report_json(report) if args.output_format == "json" else format_report_markdown(report)
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        sys.stderr.write(f"citation_nli_smoke error: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
