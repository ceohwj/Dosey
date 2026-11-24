"""Pure, persistence-free, deterministic 2nd-stage Korean evidence reranker.

Implements ``korean-lexical-concept-rerank@1``, a transparent rule-based reranker
for Korean medication-guidance evidence chunks.

Scope and domain-generalization caveats:
- Scoring combines stage-1 rank prior, alphanumeric entity-code alignment,
  Korean particle-stripped token recall, a versioned 20-group medication-guidance
  concept taxonomy, Hangul character-bigram recall, and an optional substantive
  stub penalty.
- Default stopwords and stub markers are intentionally restricted to generic
  Korean phrasing; caller- or corpus-specific terms must be opted in explicitly
  via ``RerankerConfig.extra_stopwords`` and ``RerankerConfig.stub_markers``.
- This module performs no neural inference, network I/O, or persistence.
- ``RerankCandidateInput.topic`` is reserved optional metadata and is not used
  in the current scoring formula.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from fractions import Fraction

from ai_worker.tasks.rag.evidence_rank_fusion import (
    HybridFusionResult,
    StableCoordinate,
    stable_coordinate_sort_key,
)

RERANKER_ALGORITHM_ID = "korean-lexical-concept-rerank@1"
DEFAULT_MAX_INPUT_CANDIDATES = 20
CONCEPT_GROUPS_VERSION = "1.0.0"

DEFAULT_STUB_MARKERS: tuple[str, ...] = (
    "구체적인 내용은 적혀 있지 않습니다",
    "실제 내용은 비어 있습니다",
)

_QUANTUM = Decimal("0.000001")
_ZERO = Decimal("0.000000")
_ONE = Decimal("1.000000")
_NO_ENTITY_ALIGNMENT = Decimal("0.250000")
_MAX_WEIGHT_VALUE = Decimal("1000")

_ENTITY_CODE_RE = re.compile(r"(?<![a-z0-9])[a-z]{2,}(?:-[a-z0-9]+)+(?![a-z0-9])")
_TOKEN_RE = re.compile(r"[가-힣a-z0-9]+(?:-[a-z0-9]+)*")
_HANGUL_SEGMENT_RE = re.compile(r"[가-힣]+")

_KOREAN_PARTICLES: tuple[str, ...] = (
    "에서",
    "으로",
    "이요",
    "이나",
    "까지",
    "부터",
    "한테",
    "에게",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "의",
    "에",
    "로",
    "요",
    "도",
    "만",
    "과",
    "와",
    "나",
    "께",
)

_STOPWORDS: frozenset[str] = frozenset(
    {
        "알려",
        "주세요",
        "알려주세요",
        "알고",
        "싶어요",
        "있나요",
        "인가요",
        "무엇",
        "어떻게",
        "어느",
        "정도",
        "제품",
        "자료",
        "안내",
        "정보",
    }
)

_CONCEPT_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Medication info
    ("med_ingredients", ("성분", "원료", "구성")),
    ("med_form", ("제형", "외형", "모양", "생김새", "필름")),
    ("med_purpose", ("사용목적", "효능", "효과", "용도", "어디에쓰")),
    ("med_label", ("라벨", "식별", "표식", "구분")),
    # Precautions
    ("precaution_before", ("복용전", "주의", "조심", "먹기전")),
    ("precaution_allergy", ("알레르기", "과민", "두드러기", "발진")),
    ("precaution_adverse", ("이상반응", "부작용", "증상관찰")),
    ("precaution_professional", ("전문가", "의료진", "의사", "약사", "상담", "물어봐야")),
    # Lifestyle
    ("lifestyle_hydration", ("수분", "물", "마시", "음료", "섭취")),
    ("lifestyle_diet", ("식사", "음식", "밥", "식습관")),
    ("lifestyle_activity", ("활동", "운동", "걷기", "휴식", "움직")),
    ("lifestyle_logging", ("상태기록", "몸상태", "적어", "기록")),
    # Storage
    ("storage_temp", ("보관온도", "온도", "냉장", "실온", "두어야")),
    ("storage_light_moisture", ("빛", "습기", "햇빛", "건조", "차단")),
    ("storage_location", ("보관위치", "장소", "어디에보관", "보관함")),
    ("storage_container", ("원래용기", "원래통", "용기유지", "뚜껑")),
    # Missed dose
    ("missed_early", ("일찍알았", "빨리알았", "놓쳤", "빼먹", "복용누락")),
    ("missed_near_next", ("다음복용", "가까울때", "시간임박", "곧먹을")),
    ("missed_double", ("중복복용", "두번", "겹쳐", "한꺼번에", "금지")),
    ("missed_repeated", ("반복", "자주놓쳤", "계속놓쳤", "여러번")),
)

_METADATA_QUERY_TERMS: tuple[str, ...] = (
    "포장",
    "관리 코드",
    "관리코드",
    "색인",
    "관리 번호",
    "관리번호",
)


class EvidenceRerankerErrorCode(StrEnum):
    EMPTY_QUERY = "EMPTY_QUERY"
    INVALID_CONFIG = "INVALID_CONFIG"
    INVALID_CANDIDATE = "INVALID_CANDIDATE"
    CONTENT_HASH_MISMATCH = "CONTENT_HASH_MISMATCH"
    DUPLICATE_COORDINATE = "DUPLICATE_COORDINATE"
    MISSING_CONTENT = "MISSING_CONTENT"


class EvidenceRerankerError(Exception):
    def __init__(self, code: EvidenceRerankerErrorCode, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code.value)
        self.code = code


@dataclass(frozen=True, slots=True)
class RerankCandidateInput:
    """Stage-1 candidate passed into the 2nd-stage reranker.

    ``topic`` is reserved optional metadata and does not currently affect scoring.
    """

    coordinate: StableCoordinate
    content_hash: str
    content_text: str
    stage1_rank: int
    stage1_score: Fraction | Decimal | None = None
    product_code: str | None = None
    topic: str | None = None


@dataclass(frozen=True, slots=True)
class RerankFeatureBreakdown:
    stage1_prior: Decimal
    entity_alignment: Decimal
    token_overlap: Decimal
    concept_overlap: Decimal
    char_ngram_overlap: Decimal
    substantive_adjustment: Decimal


@dataclass(frozen=True, slots=True)
class RerankCandidateResult:
    coordinate: StableCoordinate
    content_hash: str
    rerank_rank: int
    stage1_rank: int
    score: Decimal
    features: RerankFeatureBreakdown


@dataclass(frozen=True, slots=True)
class RerankerConfig:
    algorithm_id: str = RERANKER_ALGORITHM_ID
    max_input_candidates: int = DEFAULT_MAX_INPUT_CANDIDATES
    top_k: int = 5
    stage1_weight: Decimal = Decimal("0.20")
    entity_weight: Decimal = Decimal("0.30")
    token_weight: Decimal = Decimal("0.20")
    concept_weight: Decimal = Decimal("0.20")
    char_ngram_weight: Decimal = Decimal("0.10")
    stub_penalty: Decimal = Decimal("0.25")
    stub_markers: tuple[str, ...] = DEFAULT_STUB_MARKERS
    extra_stopwords: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceRerankOutcome:
    algorithm_id: str
    config_hash: str
    input_candidate_count: int
    evaluated_candidate_count: int
    results: tuple[RerankCandidateResult, ...]


@dataclass(frozen=True, slots=True)
class _ParsedQuery:
    collapsed_text: str
    entity_ids: frozenset[str]
    content_tokens: frozenset[str]
    concept_groups: frozenset[str]
    char_bigrams: frozenset[str]
    asks_metadata: bool


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)


def _canonical_decimal_str(value: Decimal) -> str:
    if value == 0:
        return "0"
    rendered = f"{value:f}"
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_valid_bounded_weight(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and Decimal("0") <= value <= _MAX_WEIGHT_VALUE


def _validate_nonempty_str_tuple(value: object, field_name: str) -> None:
    if not isinstance(value, tuple):
        raise EvidenceRerankerError(
            EvidenceRerankerErrorCode.INVALID_CONFIG,
            f"{field_name} must be a tuple of non-empty strings",
        )
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise EvidenceRerankerError(
                EvidenceRerankerErrorCode.INVALID_CONFIG,
                f"{field_name} entries must be non-empty strings",
            )


def _validate_query(query: str) -> str:
    if not isinstance(query, str):
        raise EvidenceRerankerError(
            EvidenceRerankerErrorCode.EMPTY_QUERY,
            "Query must be a string",
        )
    if any(unicodedata.category(ch) == "Cc" and ch not in ("\t", "\n", "\r") for ch in query):
        raise EvidenceRerankerError(
            EvidenceRerankerErrorCode.EMPTY_QUERY,
            "Query contains control characters",
        )
    normalized = unicodedata.normalize("NFC", query)
    if not normalized.strip():
        raise EvidenceRerankerError(
            EvidenceRerankerErrorCode.EMPTY_QUERY,
            "Query must not be blank",
        )
    return normalized


def _validate_config(config: RerankerConfig) -> None:
    if not isinstance(config.algorithm_id, str) or config.algorithm_id != RERANKER_ALGORITHM_ID:
        raise EvidenceRerankerError(
            EvidenceRerankerErrorCode.INVALID_CONFIG,
            f"Unsupported algorithm_id: {config.algorithm_id!r}",
        )
    if not _is_positive_int(config.max_input_candidates):
        raise EvidenceRerankerError(
            EvidenceRerankerErrorCode.INVALID_CONFIG,
            "max_input_candidates must be a positive integer",
        )
    if not _is_positive_int(config.top_k):
        raise EvidenceRerankerError(
            EvidenceRerankerErrorCode.INVALID_CONFIG,
            "top_k must be a positive integer",
        )
    weights = (
        ("stage1_weight", config.stage1_weight),
        ("entity_weight", config.entity_weight),
        ("token_weight", config.token_weight),
        ("concept_weight", config.concept_weight),
        ("char_ngram_weight", config.char_ngram_weight),
        ("stub_penalty", config.stub_penalty),
    )
    for field_name, field_value in weights:
        if not _is_valid_bounded_weight(field_value):
            raise EvidenceRerankerError(
                EvidenceRerankerErrorCode.INVALID_CONFIG,
                f"{field_name} must be a finite Decimal in [0, {_MAX_WEIGHT_VALUE}]",
            )
    _validate_nonempty_str_tuple(config.stub_markers, "stub_markers")
    _validate_nonempty_str_tuple(config.extra_stopwords, "extra_stopwords")


def _compute_config_hash(config: RerankerConfig) -> str:
    payload = {
        "algorithm_id": config.algorithm_id,
        "char_ngram_weight": _canonical_decimal_str(config.char_ngram_weight),
        "concept_groups": [[name, list(terms)] for name, terms in _CONCEPT_GROUPS],
        "concept_groups_version": CONCEPT_GROUPS_VERSION,
        "concept_weight": _canonical_decimal_str(config.concept_weight),
        "entity_weight": _canonical_decimal_str(config.entity_weight),
        "extra_stopwords": list(config.extra_stopwords),
        "korean_particles": list(_KOREAN_PARTICLES),
        "max_input_candidates": config.max_input_candidates,
        "metadata_query_terms": list(_METADATA_QUERY_TERMS),
        "no_entity_alignment": _canonical_decimal_str(_NO_ENTITY_ALIGNMENT),
        "stage1_weight": _canonical_decimal_str(config.stage1_weight),
        "stopwords": sorted(_STOPWORDS),
        "stub_markers": list(config.stub_markers),
        "stub_penalty": _canonical_decimal_str(config.stub_penalty),
        "token_weight": _canonical_decimal_str(config.token_weight),
        "top_k": config.top_k,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_stage1_score(stage1_score: Fraction | Decimal | None) -> None:
    if stage1_score is None:
        return
    if isinstance(stage1_score, bool) or not isinstance(stage1_score, (Fraction, Decimal)):
        raise EvidenceRerankerError(
            EvidenceRerankerErrorCode.INVALID_CANDIDATE,
            "stage1_score must be a Fraction, Decimal, or None",
        )
    if isinstance(stage1_score, Decimal) and not stage1_score.is_finite():
        raise EvidenceRerankerError(
            EvidenceRerankerErrorCode.INVALID_CANDIDATE,
            "stage1_score Decimal must be finite",
        )


def _validate_single_candidate(cand: RerankCandidateInput) -> None:
    if not isinstance(cand.coordinate, StableCoordinate):
        raise EvidenceRerankerError(
            EvidenceRerankerErrorCode.INVALID_CANDIDATE,
            "coordinate must be a StableCoordinate",
        )
    if not _is_positive_int(cand.stage1_rank):
        raise EvidenceRerankerError(
            EvidenceRerankerErrorCode.INVALID_CANDIDATE,
            f"stage1_rank must be a positive integer, got {cand.stage1_rank!r}",
        )
    if not isinstance(cand.content_hash, str) or not cand.content_hash.strip():
        raise EvidenceRerankerError(
            EvidenceRerankerErrorCode.INVALID_CANDIDATE,
            "content_hash must be a non-empty string",
        )
    if not isinstance(cand.content_text, str):
        raise EvidenceRerankerError(
            EvidenceRerankerErrorCode.INVALID_CANDIDATE,
            "content_text must be a string",
        )
    if cand.product_code is not None and not isinstance(cand.product_code, str):
        raise EvidenceRerankerError(
            EvidenceRerankerErrorCode.INVALID_CANDIDATE,
            "product_code must be a string or None",
        )
    if cand.topic is not None and not isinstance(cand.topic, str):
        raise EvidenceRerankerError(
            EvidenceRerankerErrorCode.INVALID_CANDIDATE,
            "topic must be a string or None",
        )
    _validate_stage1_score(cand.stage1_score)


def _check_unique_coordinate(
    coordinate: StableCoordinate,
    content_hash: str,
    seen_hashes: dict[StableCoordinate, str],
) -> None:
    prev_hash = seen_hashes.get(coordinate)
    if prev_hash is not None:
        if prev_hash != content_hash:
            raise EvidenceRerankerError(
                EvidenceRerankerErrorCode.CONTENT_HASH_MISMATCH,
                f"Content hash mismatch for coordinate {coordinate}",
            )
        raise EvidenceRerankerError(
            EvidenceRerankerErrorCode.DUPLICATE_COORDINATE,
            f"Duplicate coordinate {coordinate}",
        )
    seen_hashes[coordinate] = content_hash


def _validate_candidates(candidates: Sequence[RerankCandidateInput]) -> None:
    seen_hashes: dict[StableCoordinate, str] = {}
    for cand in candidates:
        _validate_single_candidate(cand)
        _check_unique_coordinate(cand.coordinate, cand.content_hash, seen_hashes)


def _validate_eligible_fusion_results(eligible: Sequence[HybridFusionResult]) -> None:
    seen_hashes: dict[StableCoordinate, str] = {}
    for item in eligible:
        if not isinstance(item.coordinate, StableCoordinate):
            raise EvidenceRerankerError(
                EvidenceRerankerErrorCode.INVALID_CANDIDATE,
                "fusion_result coordinate must be a StableCoordinate",
            )
        if not _is_positive_int(item.fusion_rank):
            raise EvidenceRerankerError(
                EvidenceRerankerErrorCode.INVALID_CANDIDATE,
                f"fusion_rank must be a positive integer, got {item.fusion_rank!r}",
            )
        if not isinstance(item.content_hash, str) or not item.content_hash.strip():
            raise EvidenceRerankerError(
                EvidenceRerankerErrorCode.INVALID_CANDIDATE,
                "fusion_result content_hash must be a non-empty string",
            )
        _check_unique_coordinate(item.coordinate, item.content_hash, seen_hashes)


def _normalize_text(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _collapse_whitespace(normalized_text: str) -> str:
    return " ".join(normalized_text.split())


def _extract_entity_ids(normalized_text: str) -> frozenset[str]:
    return frozenset(_ENTITY_CODE_RE.findall(normalized_text))


def _strip_korean_particle(token: str) -> str:
    for suffix in _KOREAN_PARTICLES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 2:
            return token[: -len(suffix)]
    return token


def _effective_stopwords(config: RerankerConfig) -> frozenset[str]:
    if not config.extra_stopwords:
        return _STOPWORDS
    extras = {_normalize_text(word).strip() for word in config.extra_stopwords}
    return _STOPWORDS | extras


def _normalized_stub_markers(config: RerankerConfig) -> tuple[str, ...]:
    return tuple(_collapse_whitespace(_normalize_text(marker)) for marker in config.stub_markers)


def _extract_content_tokens(normalized_text: str, stopwords: frozenset[str]) -> frozenset[str]:
    tokens: set[str] = set()
    for raw in _TOKEN_RE.findall(normalized_text):
        if raw in stopwords:
            continue
        stemmed = _strip_korean_particle(raw)
        if stemmed in stopwords:
            continue
        tokens.add(stemmed)
    return frozenset(tokens)


def _matches_single_char_term(raw_tokens: Sequence[str], term: str) -> bool:
    return any(token == term or (token.startswith(term) and token[1:] in _KOREAN_PARTICLES) for token in raw_tokens)


def _matches_any_concept_term(compact_text: str, raw_tokens: Sequence[str], terms: tuple[str, ...]) -> bool:
    for term in terms:
        if len(term) == 1:
            if _matches_single_char_term(raw_tokens, term):
                return True
        elif term in compact_text:
            return True
    return False


def _extract_concept_groups(normalized_text: str, raw_tokens: Sequence[str]) -> frozenset[str]:
    compact = re.sub(r"\s+", "", normalized_text)
    matched: set[str] = set()
    for group_name, terms in _CONCEPT_GROUPS:
        if _matches_any_concept_term(compact, raw_tokens, terms):
            matched.add(group_name)
    return frozenset(matched)


def _extract_char_bigrams(content_tokens: frozenset[str]) -> frozenset[str]:
    bigrams: set[str] = set()
    for token in content_tokens:
        for segment in _HANGUL_SEGMENT_RE.findall(token):
            if len(segment) >= 2:
                for idx in range(len(segment) - 1):
                    bigrams.add(segment[idx : idx + 2])
    return frozenset(bigrams)


def _parse_query(normalized_query: str, stopwords: frozenset[str]) -> _ParsedQuery:
    casefolded = normalized_query.casefold()
    collapsed = _collapse_whitespace(casefolded)
    raw_tokens = _TOKEN_RE.findall(casefolded)
    content_tokens = _extract_content_tokens(casefolded, stopwords)
    return _ParsedQuery(
        collapsed_text=collapsed,
        entity_ids=_extract_entity_ids(casefolded),
        content_tokens=content_tokens,
        concept_groups=_extract_concept_groups(casefolded, raw_tokens),
        char_bigrams=_extract_char_bigrams(content_tokens),
        asks_metadata=any(term in collapsed for term in _METADATA_QUERY_TERMS),
    )


def _compute_entity_alignment(query_ids: frozenset[str], candidate: RerankCandidateInput, cand_norm: str) -> Decimal:
    if not query_ids:
        return _ZERO
    cand_ids = set(_extract_entity_ids(cand_norm))
    if candidate.product_code is not None:
        cand_ids.update(_extract_entity_ids(_normalize_text(candidate.product_code)))
    if query_ids.issubset(cand_ids):
        return _ONE
    if not cand_ids:
        return _NO_ENTITY_ALIGNMENT
    matched = query_ids & cand_ids
    if not matched:
        return _ZERO
    return _quantize(Decimal(len(matched)) / Decimal(len(query_ids)))


def _token_matches_candidate(query_token: str, cand_tokens: frozenset[str]) -> bool:
    if query_token in cand_tokens:
        return True
    if len(query_token) < 2:
        return False
    return any(
        len(cand_token) >= 2 and (cand_token.startswith(query_token) or query_token.startswith(cand_token))
        for cand_token in cand_tokens
    )


def _compute_token_overlap(query_tokens: frozenset[str], cand_tokens: frozenset[str]) -> Decimal:
    if not query_tokens:
        return _ZERO
    matched = sum(1 for q_tok in query_tokens if _token_matches_candidate(q_tok, cand_tokens))
    return _quantize(Decimal(matched) / Decimal(len(query_tokens)))


def _compute_set_recall(query_items: frozenset[str], cand_items: frozenset[str]) -> Decimal:
    if not query_items:
        return _ZERO
    matched = len(query_items & cand_items)
    return _quantize(Decimal(matched) / Decimal(len(query_items)))


def _compute_substantive_adjustment(
    cand_collapsed: str,
    asks_metadata: bool,
    stub_markers: tuple[str, ...],
    stub_penalty: Decimal,
) -> Decimal:
    if asks_metadata:
        return _ZERO
    if any(marker in cand_collapsed for marker in stub_markers):
        return _quantize(-stub_penalty)
    return _ZERO


def _score_candidate(
    parsed_query: _ParsedQuery,
    candidate: RerankCandidateInput,
    config: RerankerConfig,
    stopwords: frozenset[str],
    stub_markers: tuple[str, ...],
) -> tuple[Decimal, RerankFeatureBreakdown]:
    cand_norm = _normalize_text(candidate.content_text)
    cand_collapsed = _collapse_whitespace(cand_norm)
    cand_raw_tokens = _TOKEN_RE.findall(cand_norm)
    cand_content_tokens = _extract_content_tokens(cand_norm, stopwords)
    cand_concepts = _extract_concept_groups(cand_norm, cand_raw_tokens)
    cand_bigrams = _extract_char_bigrams(cand_content_tokens)

    features = RerankFeatureBreakdown(
        stage1_prior=_quantize(Decimal(1) / Decimal(candidate.stage1_rank)),
        entity_alignment=_compute_entity_alignment(parsed_query.entity_ids, candidate, cand_norm),
        token_overlap=_compute_token_overlap(parsed_query.content_tokens, cand_content_tokens),
        concept_overlap=_compute_set_recall(parsed_query.concept_groups, cand_concepts),
        char_ngram_overlap=_compute_set_recall(parsed_query.char_bigrams, cand_bigrams),
        substantive_adjustment=_compute_substantive_adjustment(
            cand_collapsed,
            parsed_query.asks_metadata,
            stub_markers,
            config.stub_penalty,
        ),
    )

    total = _quantize(
        config.stage1_weight * features.stage1_prior
        + config.entity_weight * features.entity_alignment
        + config.token_weight * features.token_overlap
        + config.concept_weight * features.concept_overlap
        + config.char_ngram_weight * features.char_ngram_overlap
        + features.substantive_adjustment
    )
    return total, features


def _rerank_validated_candidates(
    normalized_query: str,
    evaluated: Sequence[RerankCandidateInput],
    *,
    input_candidate_count: int,
    config: RerankerConfig,
    config_hash: str,
) -> EvidenceRerankOutcome:
    if not evaluated:
        return EvidenceRerankOutcome(
            algorithm_id=config.algorithm_id,
            config_hash=config_hash,
            input_candidate_count=input_candidate_count,
            evaluated_candidate_count=0,
            results=(),
        )

    stopwords = _effective_stopwords(config)
    stub_markers = _normalized_stub_markers(config)
    parsed_query = _parse_query(normalized_query, stopwords)

    scored: list[tuple[Decimal, RerankCandidateInput, RerankFeatureBreakdown]] = []
    for cand in evaluated:
        score, features = _score_candidate(parsed_query, cand, config, stopwords, stub_markers)
        scored.append((score, cand, features))

    scored.sort(key=lambda item: (-item[0], stable_coordinate_sort_key(item[1].coordinate)))
    selected = scored[: config.top_k]

    results = tuple(
        RerankCandidateResult(
            coordinate=cand.coordinate,
            content_hash=cand.content_hash,
            rerank_rank=idx,
            stage1_rank=cand.stage1_rank,
            score=score,
            features=features,
        )
        for idx, (score, cand, features) in enumerate(selected, start=1)
    )

    return EvidenceRerankOutcome(
        algorithm_id=config.algorithm_id,
        config_hash=config_hash,
        input_candidate_count=input_candidate_count,
        evaluated_candidate_count=len(evaluated),
        results=results,
    )


def rerank_evidence_candidates(
    query: str,
    candidates: Sequence[RerankCandidateInput],
    *,
    config: RerankerConfig | None = None,
) -> EvidenceRerankOutcome:
    """Deterministically rerank stage-1 evidence candidates for a Korean query."""
    cfg = config if config is not None else RerankerConfig()
    _validate_config(cfg)
    normalized_query = _validate_query(query)
    _validate_candidates(candidates)

    config_hash = _compute_config_hash(cfg)
    ordered_inputs = sorted(
        candidates,
        key=lambda item: (item.stage1_rank, stable_coordinate_sort_key(item.coordinate)),
    )
    evaluated = ordered_inputs[: cfg.max_input_candidates]
    return _rerank_validated_candidates(
        normalized_query,
        evaluated,
        input_candidate_count=len(candidates),
        config=cfg,
        config_hash=config_hash,
    )


def rerank_hybrid_fusion_results(
    query: str,
    fusion_results: Sequence[HybridFusionResult],
    content_by_coordinate: Mapping[StableCoordinate, str],
    *,
    config: RerankerConfig | None = None,
    product_code_by_coordinate: Mapping[StableCoordinate, str] | None = None,
) -> EvidenceRerankOutcome:
    """Filter eligible hybrid fusion results, hydrate text, and run stage-2 reranking."""
    cfg = config if config is not None else RerankerConfig()
    _validate_config(cfg)
    normalized_query = _validate_query(query)
    config_hash = _compute_config_hash(cfg)

    eligible = [item for item in fusion_results if item.is_eligible_for_future_reranker]
    _validate_eligible_fusion_results(eligible)

    eligible.sort(key=lambda item: (item.fusion_rank, stable_coordinate_sort_key(item.coordinate)))
    capped = eligible[: cfg.max_input_candidates]

    candidate_inputs: list[RerankCandidateInput] = []
    for item in capped:
        if item.coordinate not in content_by_coordinate:
            raise EvidenceRerankerError(
                EvidenceRerankerErrorCode.MISSING_CONTENT,
                f"Missing content text for coordinate {item.coordinate}",
            )
        product_code = (
            product_code_by_coordinate.get(item.coordinate) if product_code_by_coordinate is not None else None
        )
        candidate_inputs.append(
            RerankCandidateInput(
                coordinate=item.coordinate,
                content_hash=item.content_hash,
                content_text=content_by_coordinate[item.coordinate],
                stage1_rank=item.fusion_rank,
                stage1_score=item.score,
                product_code=product_code,
            )
        )

    _validate_candidates(candidate_inputs)
    return _rerank_validated_candidates(
        normalized_query,
        candidate_inputs,
        input_candidate_count=len(eligible),
        config=cfg,
        config_hash=config_hash,
    )
