"""Retrieval ablation runner for local MFDS Korean medication evaluation corpus.

Compares `bm25_lexical`, `dense_vector` (OpenAI `text-embedding-3-large`, 1536d with
persistent JSON cache outside the git repository), and `rrf_hybrid` (k=60) over a
deterministically built Korean evaluation query set grounded to `corpus_chunks.jsonl`
(`CRM-01`–`CRM-04`, `CRM-12`).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import math
import os
import random
import re
import statistics
import subprocess
import sys
import time
import unicodedata
from collections import Counter
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ai_worker.tasks.evaluation.mfds_eval_corpus import (
    VALID_SECTIONS,
    EvalCorpusChunk,
    SectionCode,
    discover_repo_root,
)

EVAL_SCOPE = "LOCAL_EVAL_CORPUS_ONLY"
RELEASE_ELIGIBLE = False
QUERY_SET_VERSION = "chat-rag-eval-queries@1"

DEFAULT_CORPUS_PATH = Path("/tmp/dosey-mfds-eval-corpus/corpus_chunks.jsonl")
DEFAULT_CACHE_PATH = Path("/tmp/dosey-mfds-eval-corpus/embeddings_cache.json")
CACHE_SCHEMA_VERSION = "chat-rag-embedding-cache@1"

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-large"
DEFAULT_EMBEDDING_DIMENSIONS = 1536
EMBEDDING_COST_PER_MILLION_TOKENS_USD = 0.13
ADR0004_BUDGET_CAP_USD = 30.0
ADR0004_STOP_THRESHOLD_USD = 24.0
DEFAULT_MAX_COST_USD = 2.0
DEFAULT_EMBED_BATCH_SIZE = 64
DEFAULT_MAX_BATCH_ESTIMATED_TOKENS = 50_000

DEFAULT_BM25_K1 = 1.5
DEFAULT_BM25_B = 0.75
DEFAULT_RRF_K = 60
DEFAULT_TOP_K = 5
DEFAULT_POOL_SIZE = 20
DEFAULT_MIN_QUERIES = 60

_WORD_TOKEN_RE = re.compile(r"[0-9a-zA-Z가-힣]+")
_WHITESPACE_RE = re.compile(r"\s+")
_PAREN_CONTENT_RE = re.compile(r"\(([^)]+)\)")
_STRENGTH_SUFFIX_RE = re.compile(
    r"(?:\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)*(?:밀리그램|밀리그람|마이크로그램|그램|mg|mcg|ug|g|ml|%|IU))+$",
    re.IGNORECASE,
)

# Grounded semantic cue rules: (query_cue_phrase, tuple_of_chunk_substrings)
_SECTION_TOPIC_CUES: dict[SectionCode, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "EE": (
        ("혈압 조절이나 고혈압", ("고혈압", "혈압")),
        ("협심증이나 심장 증상", ("협심증", "심부전", "관상동맥")),
        ("혈당 조절과 당뇨", ("당뇨", "혈당")),
        ("콜레스테롤이나 고지혈증", ("고지혈", "이상지질", "콜레스테롤", "트리글리세라이드")),
        ("갑상선 기능 이상", ("갑상선", "갑상샘")),
        ("속쓰림이나 위식도역류", ("위식도역류", "식도염", "위궤양", "십이지장궤양", "위염", "속쓰림")),
        ("혈전 예방이나 혈액 순환", ("혈전", "색전", "뇌졸중", "심근경색", "혈소판")),
        ("통증이나 해열·소염", ("해열", "진통", "두통", "치통", "관절염", "통증", "염증")),
        ("알레르기 비염이나 두드러기·기침", ("알레르기", "비염", "두드러기", "가려움", "기침", "기관지", "천식")),
    ),
    "UD": (
        ("하루 복용 횟수와 초기 용량", ("1일 1회", "1일 2회", "1일 3회", "초회", "유지량")),
        ("식전 또는 식후 복용 타이밍", ("식후", "식전", "공복", "식사")),
        ("최대 복용량 상한", ("최대", "초과하지", "넘지")),
        ("신장애나 간장애 환자 용량 조절", ("신장애", "신기능", "간장애", "간기능")),
        ("소아나 고령자 용량 기준", ("소아", "어린이", "고령자", "노인")),
    ),
    "NB": (
        ("임부나 수유부 복용 금기 여부", ("임부", "임신", "수유부", "태아")),
        ("다른 약과의 병용투여 및 상호작용", ("병용", "상호작용", "함께 투여", "병용금기")),
        ("복용 중 음주나 알코올 주의사항", ("음주", "알코올", "술")),
        ("운전이나 졸음 관련 주의사항", ("운전", "기계조작", "졸음", "어지러움")),
        ("출혈이나 위장관 이상반응 위험", ("출혈", "위장관", "궤양", "천공")),
        ("간기능·신기능 이상 및 과민반응 경고", ("간독성", "간장애", "신장애", "과민반응", "발진", "아나필락시스")),
    ),
}

_NEGATIVE_OUT_OF_CORPUS_QUERIES: tuple[tuple[str, str], ...] = (
    ("아스트라페낙서방정75mg은 하루에 몇 번 식후에 복용하나요?", "아스트라페낙"),
    ("네오클라빈듀오시럽 소아 복용 시 부작용과 주의사항 알려주세요.", "네오클라빈"),
    ("레보스타틴오디정20mg 효능과 고지혈증 치료 적응증이 궁금해요.", "레보스타틴"),
    ("벨라모르핀패치 붙이는 위치와 교체 주기는 어떻게 되나요?", "벨라모르핀"),
    ("플루메타졸점안액 사용 중 콘택트렌즈 착용해도 되나요?", "플루메타졸"),
    ("트리벡사민캡슐 임산부가 복용해도 안전한가요?", "트리벡사민"),
    ("크로노피린정과 혈압약을 같이 먹으면 상호작용이 있나요?", "크로노피린"),
    ("제노카인겔 바른 뒤 운전할 때 주의해야 하나요?", "제노카인"),
    ("가정용 혈압계 커프 크기 선택과 정기 보정 주기는 어떻게 되나요?", "혈압계 커프"),
    ("국민건강보험 피부양자 자격 상실 기준과 신고 기한을 알려주세요.", "피부양자 자격"),
    ("CT 조영제 촬영 검사 전에 물도 마시면 안 되는 금식 시간이 몇 시간인가요?", "조영제"),
    ("독감 예방접종 당일에 가벼운 운동이나 샤워를 해도 괜찮나요?", "독감 예방접종"),
    ("치과 스케일링 건강보험 적용 횟수는 매년 언제 초기화되나요?", "스케일링"),
    ("당화혈색소 검사 정상 수치 범위와 병원에서 재는 주기가 궁금합니다.", "당화혈색소"),
    ("수면다원검사 실손보험 청구할 때 필요한 병원 서류가 무엇인가요?", "수면다원검사"),
    ("비타민D 주사 맞은 날 사우나나 음주를 피해야 하나요?", "비타민D 주사"),
)


@dataclass(frozen=True, slots=True)
class EvalQuery:
    """Single grounded evaluation query (positive or out-of-corpus negative)."""

    query_id: str
    query: str
    query_type: str
    is_negative: bool
    expected_item_seq: str | None
    expected_item_name: str | None
    expected_section: SectionCode | None
    relevant_chunk_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    """Single ranked candidate chunk produced by a retrieval arm."""

    chunk_id: str
    score: float
    rank: int


@dataclass(frozen=True, slots=True)
class EmbeddingBatchResult:
    """Normalized return container from an `Embedder` call."""

    vectors: tuple[tuple[float, ...], ...]
    prompt_tokens: int
    model: str = DEFAULT_EMBEDDING_MODEL
    dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS


@runtime_checkable
class Embedder(Protocol):
    """Injectable sync or async batch text embedder protocol."""

    def embed_texts(
        self,
        texts: Sequence[str],
        *,
        model: str = DEFAULT_EMBEDDING_MODEL,
        dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
    ) -> Sequence[Sequence[float]] | EmbeddingBatchResult | Awaitable[Sequence[Sequence[float]] | EmbeddingBatchResult]:
        """Embed `texts` into float vectors of length `dimensions`."""


@dataclass(frozen=True, slots=True)
class AbstentionThresholdCandidate:
    """Evidence Gate abstention tradeoff at a candidate top-1 score threshold."""

    threshold: float
    positive_no_hit_rate: float
    negative_false_pass_rate: float
    youden_j: float


@dataclass(frozen=True, slots=True)
class ArmMetrics:
    """Evaluated retrieval quality, latency, and abstention separation for one arm."""

    arm_name: str
    executed: bool
    positive_query_count: int
    negative_query_count: int
    recall_at_5: float | None
    mrr_at_5: float | None
    ndcg_at_5: float | None
    no_hit_rate_at_5: float | None
    section_hit_at_5: float | None
    pos_top1_mean: float | None
    neg_top1_mean: float | None
    pos_neg_top1_gap: float | None
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    abstention_candidates: tuple[AbstentionThresholdCandidate, ...] = ()


@dataclass(frozen=True, slots=True)
class PairedBootstrapComparison:
    """Paired bootstrap 95% confidence interval for Recall@5 difference."""

    candidate_arm: str
    baseline_arm: str
    mean_diff: float
    ci95_low: float
    ci95_high: float
    sample_count: int


@dataclass(frozen=True, slots=True)
class DenseEmbeddingSummary:
    """Token, cost, and cache accounting for the dense embedding stage."""

    executed: bool
    model: str
    dimensions: int
    cache_path: str
    cache_hits: int
    cache_misses: int
    api_calls: int
    prompt_tokens: int
    estimated_tokens: int
    estimated_cost_usd: float
    max_cost_usd: float


@dataclass(frozen=True, slots=True)
class RetrievalAblationReport:
    """Complete reproducible report for `chat_rag_retrieval_ablation`."""

    eval_scope: str
    release_eligible: bool
    mode: str
    corpus_path: str
    corpus_sha256: str
    corpus_chunk_count: int
    corpus_item_count: int
    query_set_version: str
    query_set_sha256: str
    total_queries: int
    positive_queries: int
    negative_queries: int
    query_type_counts: dict[str, int]
    config_hash: str
    top_k: int
    pool_size: int
    bm25_k1: float
    bm25_b: float
    rrf_k: int
    dense_summary: DenseEmbeddingSummary
    arms: dict[str, ArmMetrics]
    paired_comparisons: tuple[PairedBootstrapComparison, ...]
    git_commit: str
    command: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_text(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_json(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(encoded)


def _validate_positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def validate_cache_path_outside_repo(
    cache_path: Path,
    *,
    repo_root: Path | None = None,
) -> Path:
    """Fail closed if `cache_path` resolves inside the git repository root."""
    if repo_root is None:
        try:
            resolved_repo = discover_repo_root().resolve()
        except RuntimeError:
            return cache_path.expanduser().resolve()
    else:
        resolved_repo = repo_root.resolve()

    resolved_cache = cache_path.expanduser().resolve()
    if resolved_cache == resolved_repo or resolved_cache.is_relative_to(resolved_repo):
        raise ValueError(
            "Embedding cache path must be strictly outside the git repository root so caches are never tracked."
        )
    return resolved_cache


def read_openai_api_key(env: Mapping[str, str] | None = None) -> str:
    """Read `OPENAI_API_KEY` from environment without ever logging or persisting it."""
    source = env if env is not None else os.environ
    key = source.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY is missing or empty; set it in the environment or pass --offline-bm25-only / --dry-run."
        )
    return key


def load_eval_corpus(corpus_path: Path) -> tuple[tuple[EvalCorpusChunk, ...], str]:
    """Load and validate `corpus_chunks.jsonl`; fail closed on missing or empty input."""
    resolved = corpus_path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Evaluation corpus JSONL not found: {resolved}")
    if not resolved.is_file():
        raise FileNotFoundError(f"Evaluation corpus path is not a file: {resolved}")

    raw_bytes = resolved.read_bytes()
    if not raw_bytes.strip():
        raise ValueError(f"Evaluation corpus JSONL is empty: {resolved}")

    corpus_sha256 = _sha256_bytes(raw_bytes)
    chunks: list[EvalCorpusChunk] = []
    seen_chunk_ids: set[str] = set()

    for line_no, raw_line in enumerate(raw_bytes.decode("utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON on line {line_no} of {resolved}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"Corpus line {line_no} must be a JSON object")

        chunk = _parse_corpus_record(record, line_no=line_no)
        if chunk.chunk_id in seen_chunk_ids:
            raise ValueError(f"Duplicate chunk_id {chunk.chunk_id!r} on line {line_no}")
        seen_chunk_ids.add(chunk.chunk_id)
        chunks.append(chunk)

    if not chunks:
        raise ValueError(f"Evaluation corpus JSONL contains zero chunks: {resolved}")

    return tuple(chunks), corpus_sha256


def _parse_corpus_record(record: Mapping[str, Any], *, line_no: int) -> EvalCorpusChunk:
    required_keys = ("chunk_id", "item_seq", "item_name", "section", "content")
    for key in required_keys:
        val = record.get(key)
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"Missing or empty {key!r} on corpus line {line_no}")

    section = record["section"].strip()
    if section not in VALID_SECTIONS:
        raise ValueError(f"Invalid section {section!r} on corpus line {line_no}")

    content = _normalize_text(record["content"])
    category_raw = record.get("category")
    category = str(category_raw).strip() if isinstance(category_raw, str) and category_raw.strip() else None

    return EvalCorpusChunk(
        chunk_id=record["chunk_id"].strip(),
        item_seq=record["item_seq"].strip(),
        item_name=_normalize_text(record["item_name"]),
        section=section,  # type: ignore[arg-type]
        locator=str(record.get("locator") or f"{section}:p0").strip(),
        source_code=str(record.get("source_code") or "MFDS_DATA_GO_KR_15095677_EVAL").strip(),
        source_version=str(record.get("source_version") or "unknown").strip(),
        content=content,
        content_sha256=str(record.get("content_sha256") or _sha256_bytes(content.encode("utf-8"))).strip(),
        category=category,
    )


def _extract_name_variants(item_name: str) -> tuple[str, str, str | None]:
    """Return `(short_name, root_name, ingredient_hint)` from an MFDS `ITEM_NAME`."""
    normalized = _normalize_text(item_name)
    paren_match = _PAREN_CONTENT_RE.search(normalized)
    ingredient = paren_match.group(1).strip() if paren_match else None
    short_name = _PAREN_CONTENT_RE.sub("", normalized).strip() or normalized
    root_name = _STRENGTH_SUFFIX_RE.sub("", short_name).strip() or short_name
    return short_name, root_name, ingredient


def _find_topic_cue_and_chunks(
    section: SectionCode,
    section_chunks: Sequence[EvalCorpusChunk],
) -> tuple[str | None, tuple[str, ...]]:
    """Find a grounded semantic cue and matching subset of chunk IDs within a section."""
    for cue_phrase, keywords in _SECTION_TOPIC_CUES[section]:
        matched = [c.chunk_id for c in section_chunks if any(kw in c.content for kw in keywords)]
        if matched:
            return cue_phrase, tuple(matched[:3])
    return None, tuple(c.chunk_id for c in section_chunks[:3])


def _select_chunk_relevant_ids(
    section: SectionCode,
    section_chunks: Sequence[EvalCorpusChunk],
    variant_index: int,
) -> tuple[str | None, tuple[str, ...]]:
    """Select grounded target chunk IDs (1..3) and an optional content-grounded cue."""
    cue_phrase, cue_chunks = _find_topic_cue_and_chunks(section, section_chunks)
    if len(section_chunks) <= 3:
        if cue_phrase is not None and variant_index % 2 == 1:
            return cue_phrase, cue_chunks
        return cue_phrase, tuple(c.chunk_id for c in section_chunks)

    # For multi-chunk sections (e.g. long NB), bind to matched topic chunks or cycle cleanly.
    if cue_phrase is not None:
        return cue_phrase, cue_chunks
    target = section_chunks[variant_index % len(section_chunks)]
    return None, (target.chunk_id,)


def _format_positive_query(
    *,
    section: SectionCode,
    short_name: str,
    root_name: str,
    ingredient: str | None,
    cue_phrase: str | None,
    variant_index: int,
) -> tuple[str, str]:
    """Build `(query_text, query_type)` spanning section intents and colloquial Korean."""
    ing_ref = f"{root_name}({ingredient})" if ingredient and variant_index % 3 == 2 else root_name
    cue_tail = f" 특히 {cue_phrase} 관련해서 알려주세요." if cue_phrase else ""

    if section == "UD":
        templates = (
            f"{short_name} 용법과 하루 복용량, 복용 시간대를 알려주세요.{cue_tail}",
            f"{root_name}은 하루에 몇 번, 식전이나 식후 언제 먹어야 하나요?",
            f"{ing_ref} 복용할 때 한 번에 몇 알씩 먹으면 되나요?{cue_tail}",
            f"약국에서 받은 {root_name} 먹는 법이랑 복용 타이밍이 헷갈려요.",
            f"{short_name}을 신장이나 간이 약한 사람이 먹을 때 용량 조절이 필요한가요?",
            f"{root_name} 깜빡하고 못 먹었을 때나 최대 복용량 기준을 알려줘.",
        )
        q_type = "colloquial_natural" if variant_index % 4 == 3 else "dosage_ud"
        return templates[variant_index % len(templates)], q_type

    if section == "EE":
        templates = (
            f"{short_name}의 주요 효능과 효과(적응증)는 무엇인가요?{cue_tail}",
            f"{root_name}은 어떤 증상이나 병을 치료할 때 처방되는 약인가요?",
            f"{ing_ref} 먹으면 어디에 효과가 있는지 쉽게 설명해 주세요.{cue_tail}",
            f"처방받은 {root_name}이 정확히 어떤 질환 치료용 약인지 궁금해요.",
            f"{short_name}이 {cue_phrase or '만성 증상'} 개선에 쓰이는 약이 맞나요?",
            f"{root_name} 복용하면 기대할 수 있는 치료 효과가 뭔가요?",
        )
        q_type = "colloquial_natural" if variant_index % 4 == 3 else "efficacy_ee"
        return templates[variant_index % len(templates)], q_type

    templates = (
        f"{short_name} 복용 시 사용상의 주의사항과 금기 대상자를 알려주세요.{cue_tail}",
        f"{root_name} 먹을 때 조심해야 할 부작용이나 이상반응이 있나요?",
        f"{ing_ref} 복용 중에 다른 약이랑 같이 먹거나 술 마시면 안 되나요?{cue_tail}",
        f"임신 중이거나 수유 중인데 {root_name} 먹어도 괜찮은지 걱정돼요.",
        f"{short_name} 투여 전후로 꼭 확인해야 하는 경고나 신중투여 대상은 누구인가요?",
        f"{root_name} 먹고 어지럽거나 특이 반응이 나타날 수 있나요?{cue_tail}",
    )
    q_type = "colloquial_natural" if variant_index % 4 == 3 else "warning_nb"
    return templates[variant_index % len(templates)], q_type


def _disambiguate_query_text(base_text: str, *, variant_index: int, used_texts: set[str]) -> str:
    if base_text not in used_texts:
        return base_text
    suffixes = (
        " (복약 상담 질문)",
        " 자세히 확인하고 싶습니다.",
        " 핵심 안내만 부탁드려요.",
        " 복용 전 확인용입니다.",
    )
    for suffix in suffixes:
        candidate = f"{base_text}{suffix}"
        if candidate not in used_texts:
            return candidate
    return f"{base_text} [질의-{variant_index}]"


def build_eval_queries(
    chunks: Sequence[EvalCorpusChunk],
    *,
    min_queries: int = DEFAULT_MIN_QUERIES,
) -> tuple[EvalQuery, ...]:
    """Deterministically build grounded positive and out-of-corpus negative Korean queries."""
    _validate_positive_int("min_queries", min_queries)
    if not chunks:
        raise ValueError("Cannot build evaluation queries from an empty chunk corpus.")

    grouped: dict[str, dict[SectionCode, list[EvalCorpusChunk]]] = {}
    for chunk in chunks:
        by_sec = grouped.setdefault(chunk.item_seq, {})
        by_sec.setdefault(chunk.section, []).append(chunk)

    targets: list[tuple[str, SectionCode, list[EvalCorpusChunk]]] = []
    for item_seq in sorted(grouped):
        for section in VALID_SECTIONS:
            sec_chunks = grouped[item_seq].get(section)
            if sec_chunks:
                ordered_chunks = sorted(sec_chunks, key=lambda c: c.chunk_id)
                targets.append((item_seq, section, ordered_chunks))

    negative_queries = _select_out_of_corpus_negatives(chunks, min_queries=min_queries)
    min_positive = max(1, min_queries - len(negative_queries))

    queries: list[EvalQuery] = []
    used_texts: set[str] = set()
    round_idx = 0

    while len(queries) < min_positive or round_idx == 0:
        for item_seq, section, sec_chunks in targets:
            if round_idx > 0 and len(queries) >= min_positive:
                break
            item_name = sec_chunks[0].item_name
            short_name, root_name, ingredient = _extract_name_variants(item_name)
            cue_phrase, relevant_ids = _select_chunk_relevant_ids(section, sec_chunks, round_idx)
            raw_text, q_type = _format_positive_query(
                section=section,
                short_name=short_name,
                root_name=root_name,
                ingredient=ingredient,
                cue_phrase=cue_phrase,
                variant_index=round_idx,
            )
            query_text = _disambiguate_query_text(raw_text, variant_index=len(queries), used_texts=used_texts)
            used_texts.add(query_text)
            queries.append(
                EvalQuery(
                    query_id=f"q-{len(queries) + 1:04d}-{section}",
                    query=query_text,
                    query_type=q_type,
                    is_negative=False,
                    expected_item_seq=item_seq,
                    expected_item_name=item_name,
                    expected_section=section,
                    relevant_chunk_ids=relevant_ids,
                )
            )
        round_idx += 1

    for neg_text in negative_queries:
        unique_neg = _disambiguate_query_text(neg_text, variant_index=len(queries), used_texts=used_texts)
        used_texts.add(unique_neg)
        queries.append(
            EvalQuery(
                query_id=f"q-{len(queries) + 1:04d}-NEG",
                query=unique_neg,
                query_type="out_of_corpus_negative",
                is_negative=True,
                expected_item_seq=None,
                expected_item_name=None,
                expected_section=None,
                relevant_chunk_ids=(),
            )
        )

    return tuple(queries)


def _select_out_of_corpus_negatives(
    chunks: Sequence[EvalCorpusChunk],
    *,
    min_queries: int,
) -> tuple[str, ...]:
    target_neg_count = max(4, min(len(_NEGATIVE_OUT_OF_CORPUS_QUERIES), max(8, min_queries // 5)))
    corpus_haystack = " ".join(f"{c.item_name} {c.content}" for c in chunks).lower()

    selected: list[str] = []
    for query_text, guard_token in _NEGATIVE_OUT_OF_CORPUS_QUERIES:
        if guard_token.lower() in corpus_haystack:
            continue
        selected.append(query_text)
        if len(selected) >= target_neg_count:
            break

    idx = 0
    while len(selected) < target_neg_count:
        idx += 1
        selected.append(f"미등재가상의약품{idx:02d}호정의 복용법과 주의사항을 알려주세요.")

    return tuple(selected)


def tokenize_korean_bm25(text: str) -> tuple[str, ...]:
    """Tokenize Korean/English text into word tokens + character 2-grams (`char_bigram`)."""
    normalized = unicodedata.normalize("NFKC", text).lower()
    tokens: list[str] = []
    for word in _WORD_TOKEN_RE.findall(normalized):
        tokens.append(f"w:{word}")
        if len(word) >= 2:
            for idx in range(len(word) - 1):
                tokens.append(f"bg:{word[idx : idx + 2]}")
    return tuple(tokens)


def chunk_index_text(chunk: EvalCorpusChunk) -> str:
    """Canonical chunk text indexed by BM25 and dense embeddings."""
    return f"{chunk.item_name} [{chunk.section}] {chunk.content}"


class BM25Index:
    """Pure Python Okapi BM25 index (`k1=1.5`, `b=0.75`) over word + char 2-gram tokens."""

    def __init__(
        self,
        chunks: Sequence[EvalCorpusChunk],
        *,
        k1: float = DEFAULT_BM25_K1,
        b: float = DEFAULT_BM25_B,
    ) -> None:
        if not chunks:
            raise ValueError("BM25Index requires at least one chunk.")
        self.k1 = k1
        self.b = b
        self._chunk_ids: tuple[str, ...] = tuple(c.chunk_id for c in chunks)
        self._doc_tf: list[Counter[str]] = []
        self._doc_len: list[int] = []
        df: Counter[str] = Counter()
        self._postings: dict[str, list[int]] = {}

        for doc_idx, chunk in enumerate(chunks):
            tokens = tokenize_korean_bm25(chunk_index_text(chunk))
            tf = Counter(tokens)
            self._doc_tf.append(tf)
            self._doc_len.append(len(tokens))
            for term in tf:
                df[term] += 1
                self._postings.setdefault(term, []).append(doc_idx)

        num_docs = len(chunks)
        total_len = sum(self._doc_len)
        self._avgdl = (total_len / num_docs) if num_docs > 0 and total_len > 0 else 1.0
        self._idf: dict[str, float] = {
            term: math.log((num_docs - doc_freq + 0.5) / (doc_freq + 0.5) + 1.0) for term, doc_freq in df.items()
        }

    def search(self, query: str, *, top_k: int = DEFAULT_TOP_K) -> tuple[ScoredChunk, ...]:
        """Return up to `top_k` non-zero BM25 matches deterministically sorted by `(-score, chunk_id)`."""
        _validate_positive_int("top_k", top_k)
        query_tf = Counter(tokenize_korean_bm25(query))
        if not query_tf:
            return ()

        scores: dict[int, float] = {}
        for term, q_count in query_tf.items():
            idf = self._idf.get(term)
            if idf is None:
                continue
            for doc_idx in self._postings.get(term, ()):
                tf = self._doc_tf[doc_idx][term]
                dl = self._doc_len[doc_idx]
                denom = tf + self.k1 * (1.0 - self.b + self.b * (dl / self._avgdl))
                term_score = idf * ((tf * (self.k1 + 1.0)) / denom) * q_count
                scores[doc_idx] = scores.get(doc_idx, 0.0) + term_score

        ranked = sorted(
            ((self._chunk_ids[idx], score) for idx, score in scores.items() if score > 0.0),
            key=lambda item: (-item[1], item[0]),
        )[:top_k]
        return tuple(
            ScoredChunk(chunk_id=chunk_id, score=round(score, 8), rank=rank)
            for rank, (chunk_id, score) in enumerate(ranked, start=1)
        )


def estimate_embedding_tokens(text: str) -> int:
    """Conservative upper-bound token estimate for Korean/mixed text before OpenAI embedding."""
    if not text:
        return 1
    weighted = sum(2 if "\uac00" <= ch <= "\ud7a3" else 1 for ch in text)
    return max(1, (weighted // 2) + 2)


def _l2_normalize(vector: Sequence[float], *, expected_dim: int) -> tuple[float, ...]:
    if len(vector) != expected_dim:
        raise ValueError(f"Expected embedding vector dimension {expected_dim}, got {len(vector)}")
    floats = [float(val) for val in vector]
    if any(not math.isfinite(val) for val in floats):
        raise ValueError("Embedding vector contains non-finite values.")
    norm = math.sqrt(sum(val * val for val in floats))
    if norm == 0.0:
        return tuple(0.0 for _ in floats)
    return tuple(val / norm for val in floats)


def _cache_key(text: str, *, model: str, dimensions: int) -> str:
    return _sha256_bytes(f"{model}:{dimensions}:{text}".encode())


def load_embedding_cache(
    cache_path: Path,
    *,
    model: str,
    dimensions: int,
) -> dict[str, tuple[float, ...]]:
    """Load persistent JSON embedding cache if present; validate schema and dimensions."""
    if not cache_path.exists():
        return {}
    raw = json.loads(cache_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Embedding cache must be a JSON object: {cache_path}")
    if raw.get("schema_version") != CACHE_SCHEMA_VERSION:
        raise ValueError(f"Unsupported embedding cache schema_version in {cache_path}")
    if raw.get("model") != model or raw.get("dimensions") != dimensions:
        raise ValueError(
            f"Embedding cache model/dimensions mismatch in {cache_path}: "
            f"expected ({model}, {dimensions}), got ({raw.get('model')}, {raw.get('dimensions')})"
        )
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        raise ValueError(f"Embedding cache entries must be a JSON object: {cache_path}")

    loaded: dict[str, tuple[float, ...]] = {}
    for key, vec in entries.items():
        if not isinstance(key, str) or not isinstance(vec, list):
            raise ValueError(f"Invalid cache entry for key {key!r} in {cache_path}")
        loaded[key] = _l2_normalize(vec, expected_dim=dimensions)
    return loaded


def save_embedding_cache(
    cache_path: Path,
    entries: Mapping[str, tuple[float, ...]],
    *,
    model: str,
    dimensions: int,
) -> None:
    """Atomically persist embedding cache JSON outside the git repository."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "model": model,
        "dimensions": dimensions,
        "entry_count": len(entries),
        "entries": {key: [round(x, 8) for x in vec] for key, vec in sorted(entries.items())},
    }
    tmp_path = cache_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, cache_path)


class OpenAITextBatchEmbedder:
    """Live OpenAI `text-embedding-3-large` batch embedder using `AsyncOpenAI`."""

    def __init__(self, *, api_key: str) -> None:
        if not api_key.strip():
            raise ValueError("api_key must be non-empty")
        self._api_key = api_key

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        model: str = DEFAULT_EMBEDDING_MODEL,
        dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
    ) -> EmbeddingBatchResult:
        if not texts:
            return EmbeddingBatchResult(vectors=(), prompt_tokens=0, model=model, dimensions=dimensions)
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self._api_key)
        try:
            response = await client.embeddings.create(
                input=list(texts),
                model=model,
                dimensions=dimensions,
                encoding_format="float",
            )
        finally:
            await client.close()

        items = sorted(response.data, key=lambda item: item.index)
        if len(items) != len(texts):
            raise RuntimeError(f"OpenAI embeddings returned {len(items)} vectors for {len(texts)} inputs.")
        vectors = tuple(_l2_normalize(item.embedding, expected_dim=dimensions) for item in items)
        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        return EmbeddingBatchResult(
            vectors=vectors,
            prompt_tokens=prompt_tokens,
            model=model,
            dimensions=dimensions,
        )


def _chunk_text_batches(
    texts: Sequence[str],
    *,
    max_batch_size: int = DEFAULT_EMBED_BATCH_SIZE,
    max_batch_tokens: int = DEFAULT_MAX_BATCH_ESTIMATED_TOKENS,
) -> list[list[str]]:
    batches: list[list[str]] = []
    current_batch: list[str] = []
    current_tokens = 0

    for text in texts:
        est = estimate_embedding_tokens(text)
        if est > max_batch_tokens:
            raise ValueError(
                f"Single text estimated tokens ({est}) exceeds max_batch_tokens ({max_batch_tokens}); aborting safely."
            )
        if current_batch and (len(current_batch) >= max_batch_size or current_tokens + est > max_batch_tokens):
            batches.append(current_batch)
            current_batch = []
            current_tokens = 0
        current_batch.append(text)
        current_tokens += est

    if current_batch:
        batches.append(current_batch)
    return batches


async def _invoke_embedder_async(
    embedder: Embedder,
    texts: Sequence[str],
    *,
    model: str,
    dimensions: int,
) -> EmbeddingBatchResult:
    raw = embedder.embed_texts(texts, model=model, dimensions=dimensions)
    if inspect.isawaitable(raw):
        resolved = await raw
    else:
        resolved = raw
    return _normalize_embedder_output(resolved, texts=texts, model=model, dimensions=dimensions)


def _normalize_embedder_output(
    output: Sequence[Sequence[float]] | EmbeddingBatchResult,
    *,
    texts: Sequence[str],
    model: str,
    dimensions: int,
) -> EmbeddingBatchResult:
    if isinstance(output, EmbeddingBatchResult):
        vectors = tuple(_l2_normalize(vec, expected_dim=dimensions) for vec in output.vectors)
        if len(vectors) != len(texts):
            raise ValueError(f"Embedder returned {len(vectors)} vectors for {len(texts)} texts.")
        return EmbeddingBatchResult(
            vectors=vectors,
            prompt_tokens=output.prompt_tokens,
            model=output.model,
            dimensions=output.dimensions,
        )

    vectors = tuple(_l2_normalize(vec, expected_dim=dimensions) for vec in output)
    if len(vectors) != len(texts):
        raise ValueError(f"Embedder returned {len(vectors)} vectors for {len(texts)} texts.")
    estimated = sum(estimate_embedding_tokens(text) for text in texts)
    return EmbeddingBatchResult(
        vectors=vectors,
        prompt_tokens=estimated,
        model=model,
        dimensions=dimensions,
    )


def _resolve_embeddings_with_cache(
    texts: Sequence[str],
    *,
    cache_path: Path,
    embedder: Embedder,
    model: str,
    dimensions: int,
    max_cost_usd: float,
    batch_size: int = DEFAULT_EMBED_BATCH_SIZE,
) -> tuple[dict[str, tuple[float, ...]], DenseEmbeddingSummary]:
    if max_cost_usd <= 0.0 or max_cost_usd > ADR0004_STOP_THRESHOLD_USD:
        raise ValueError(f"max_cost_usd must be in (0, {ADR0004_STOP_THRESHOLD_USD}] per ADR 0004 80% stop threshold.")

    cache_entries = load_embedding_cache(cache_path, model=model, dimensions=dimensions)
    unique_texts = list(dict.fromkeys(texts))
    text_to_key = {text: _cache_key(text, model=model, dimensions=dimensions) for text in unique_texts}

    missing_texts = [text for text in unique_texts if text_to_key[text] not in cache_entries]
    cache_hits = len(unique_texts) - len(missing_texts)
    cache_misses = len(missing_texts)

    estimated_tokens = sum(estimate_embedding_tokens(text) for text in missing_texts)
    estimated_preflight_cost = (estimated_tokens / 1_000_000.0) * EMBEDDING_COST_PER_MILLION_TOKENS_USD
    if estimated_preflight_cost > max_cost_usd:
        raise ValueError(
            f"Estimated embedding cost ${estimated_preflight_cost:.4f} exceeds safety threshold "
            f"${max_cost_usd:.4f} (ADR 0004 budget guard)."
        )

    prompt_tokens = 0
    api_calls = 0
    if missing_texts:
        batches = _chunk_text_batches(missing_texts, max_batch_size=batch_size)
        for batch in batches:
            batch_result = _run_batch_embed(embedder, batch, model=model, dimensions=dimensions)
            api_calls += 1
            prompt_tokens += batch_result.prompt_tokens
            for text, vec in zip(batch, batch_result.vectors, strict=True):
                cache_entries[text_to_key[text]] = vec
        save_embedding_cache(cache_path, cache_entries, model=model, dimensions=dimensions)

    actual_cost_usd = round((prompt_tokens / 1_000_000.0) * EMBEDDING_COST_PER_MILLION_TOKENS_USD, 8)
    resolved_vectors = {text: cache_entries[text_to_key[text]] for text in unique_texts}
    summary = DenseEmbeddingSummary(
        executed=True,
        model=model,
        dimensions=dimensions,
        cache_path=str(cache_path),
        cache_hits=cache_hits,
        cache_misses=cache_misses,
        api_calls=api_calls,
        prompt_tokens=prompt_tokens,
        estimated_tokens=estimated_tokens,
        estimated_cost_usd=actual_cost_usd,
        max_cost_usd=max_cost_usd,
    )
    return resolved_vectors, summary


def _run_batch_embed(
    embedder: Embedder,
    batch: Sequence[str],
    *,
    model: str,
    dimensions: int,
) -> EmbeddingBatchResult:
    raw = embedder.embed_texts(batch, model=model, dimensions=dimensions)
    if inspect.isawaitable(raw):

        async def _await_raw() -> EmbeddingBatchResult:
            resolved = await raw
            return _normalize_embedder_output(resolved, texts=batch, model=model, dimensions=dimensions)

        return asyncio.run(_await_raw())
    return _normalize_embedder_output(raw, texts=batch, model=model, dimensions=dimensions)


class DenseVectorIndex:
    """In-memory normalized cosine-similarity index over precomputed chunk embeddings."""

    def __init__(
        self,
        chunks: Sequence[EvalCorpusChunk],
        vectors_by_text: Mapping[str, tuple[float, ...]],
    ) -> None:
        if not chunks:
            raise ValueError("DenseVectorIndex requires at least one chunk.")
        self._chunk_ids: tuple[str, ...] = tuple(c.chunk_id for c in chunks)
        self._vectors: tuple[tuple[float, ...], ...] = tuple(vectors_by_text[chunk_index_text(c)] for c in chunks)

    def search_vector(
        self,
        query_vector: Sequence[float],
        *,
        top_k: int = DEFAULT_TOP_K,
    ) -> tuple[ScoredChunk, ...]:
        """Score normalized cosine similarity against all corpus chunks."""
        _validate_positive_int("top_k", top_k)
        scored: list[tuple[str, float]] = []
        for chunk_id, doc_vec in zip(self._chunk_ids, self._vectors, strict=True):
            sim = sum(q * d for q, d in zip(query_vector, doc_vec, strict=True))
            scored.append((chunk_id, sim))

        ranked = sorted(scored, key=lambda item: (-item[1], item[0]))[:top_k]
        return tuple(
            ScoredChunk(chunk_id=chunk_id, score=round(score, 8), rank=rank)
            for rank, (chunk_id, score) in enumerate(ranked, start=1)
        )


def rrf_fuse(
    bm25_candidates: Sequence[ScoredChunk],
    dense_candidates: Sequence[ScoredChunk],
    *,
    rrf_k: int = DEFAULT_RRF_K,
    top_k: int = DEFAULT_TOP_K,
) -> tuple[ScoredChunk, ...]:
    """Deterministic Reciprocal Rank Fusion (`1/(rrf_k+rank_bm25) + 1/(rrf_k+rank_dense)`)."""
    _validate_positive_int("rrf_k", rrf_k)
    _validate_positive_int("top_k", top_k)

    fused_scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}

    for rank_idx, candidate in enumerate(bm25_candidates, start=1):
        rank = candidate.rank if candidate.rank > 0 else rank_idx
        fused_scores[candidate.chunk_id] = fused_scores.get(candidate.chunk_id, 0.0) + (1.0 / (rrf_k + rank))
        best_rank[candidate.chunk_id] = min(best_rank.get(candidate.chunk_id, rank), rank)

    for rank_idx, candidate in enumerate(dense_candidates, start=1):
        rank = candidate.rank if candidate.rank > 0 else rank_idx
        fused_scores[candidate.chunk_id] = fused_scores.get(candidate.chunk_id, 0.0) + (1.0 / (rrf_k + rank))
        best_rank[candidate.chunk_id] = min(best_rank.get(candidate.chunk_id, rank), rank)

    ordered = sorted(
        fused_scores.items(),
        key=lambda item: (-round(item[1], 12), best_rank[item[0]], item[0]),
    )[:top_k]

    return tuple(
        ScoredChunk(chunk_id=chunk_id, score=round(score, 8), rank=rank)
        for rank, (chunk_id, score) in enumerate(ordered, start=1)
    )


def compute_query_retrieval_metrics(
    ranked_chunk_ids: Sequence[str],
    relevant_chunk_ids: Sequence[str],
    *,
    k: int = DEFAULT_TOP_K,
) -> dict[str, float]:
    """Compute `recall_at_k`, `mrr_at_k`, `ndcg_at_k`, and `no_hit` for one positive query."""
    _validate_positive_int("k", k)
    relevant_set = set(relevant_chunk_ids)
    if not relevant_set:
        raise ValueError("Positive query must have at least one relevant chunk_id.")

    top_ids = list(ranked_chunk_ids[:k])
    hits = [cid for cid in top_ids if cid in relevant_set]
    recall = len(hits) / len(relevant_set)
    no_hit = 0.0 if hits else 1.0

    mrr = 0.0
    for rank, cid in enumerate(top_ids, start=1):
        if cid in relevant_set:
            mrr = 1.0 / rank
            break

    dcg = sum(1.0 / math.log2(rank + 1) for rank, cid in enumerate(top_ids, start=1) if cid in relevant_set)
    ideal_hits = min(len(relevant_set), k)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    ndcg = (dcg / idcg) if idcg > 0.0 else 0.0

    return {
        "recall_at_k": recall,
        "mrr_at_k": mrr,
        "ndcg_at_k": ndcg,
        "no_hit": no_hit,
    }


def summarize_latencies_ms(latencies_ms: Sequence[float]) -> tuple[float, float]:
    """Return `(p50_ms, p95_ms)` rounded to 3 decimal places."""
    if not latencies_ms:
        return 0.0, 0.0
    ordered = sorted(float(val) for val in latencies_ms)
    p50 = round(statistics.median(ordered), 3)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    p95 = round(ordered[p95_index], 3)
    return p50, p95


def compute_abstention_candidates(
    pos_top1_scores: Sequence[float],
    neg_top1_scores: Sequence[float],
) -> tuple[AbstentionThresholdCandidate, ...]:
    """Sweep candidate Evidence Gate top-1 thresholds across observed positive/negative scores."""
    if not pos_top1_scores or not neg_top1_scores:
        return ()
    combined = sorted({round(s, 4) for s in (*pos_top1_scores, *neg_top1_scores)})
    if len(combined) > 7:
        step = max(1, len(combined) // 6)
        grid = combined[::step]
        if combined[-1] not in grid:
            grid.append(combined[-1])
    else:
        grid = combined

    candidates: list[AbstentionThresholdCandidate] = []
    pos_n = len(pos_top1_scores)
    neg_n = len(neg_top1_scores)
    for thresh in grid:
        pos_no_hit = sum(1 for s in pos_top1_scores if s < thresh) / pos_n
        neg_false_pass = sum(1 for s in neg_top1_scores if s >= thresh) / neg_n
        youden_j = (1.0 - pos_no_hit) - neg_false_pass
        candidates.append(
            AbstentionThresholdCandidate(
                threshold=round(thresh, 4),
                positive_no_hit_rate=round(pos_no_hit, 4),
                negative_false_pass_rate=round(neg_false_pass, 4),
                youden_j=round(youden_j, 4),
            )
        )
    candidates.sort(key=lambda c: (-c.youden_j, c.negative_false_pass_rate, c.threshold))
    return tuple(candidates[:5])


def _aggregate_arm_metrics(
    *,
    arm_name: str,
    queries: Sequence[EvalQuery],
    rankings_by_qid: Mapping[str, Sequence[ScoredChunk]],
    latencies_ms: Sequence[float],
    chunk_by_id: Mapping[str, EvalCorpusChunk],
    top_k: int,
) -> tuple[ArmMetrics, dict[str, float]]:
    recalls: list[float] = []
    mrrs: list[float] = []
    ndcgs: list[float] = []
    no_hits: list[float] = []
    sec_hits: list[float] = []
    pos_top1: list[float] = []
    neg_top1: list[float] = []
    per_query_recall: dict[str, float] = {}

    for query in queries:
        ranked = rankings_by_qid.get(query.query_id, ())
        top1_score = ranked[0].score if ranked else 0.0

        if query.is_negative:
            neg_top1.append(top1_score)
            continue

        pos_top1.append(top1_score)
        ranked_ids = [item.chunk_id for item in ranked[:top_k]]
        scores = compute_query_retrieval_metrics(ranked_ids, query.relevant_chunk_ids, k=top_k)
        recalls.append(scores["recall_at_k"])
        mrrs.append(scores["mrr_at_k"])
        ndcgs.append(scores["ndcg_at_k"])
        no_hits.append(scores["no_hit"])
        per_query_recall[query.query_id] = scores["recall_at_k"]

        sec_hit = any(
            chunk_by_id[cid].item_seq == query.expected_item_seq and chunk_by_id[cid].section == query.expected_section
            for cid in ranked_ids
            if cid in chunk_by_id
        )
        sec_hits.append(1.0 if sec_hit else 0.0)

    p50_ms, p95_ms = summarize_latencies_ms(latencies_ms)
    pos_mean = round(sum(pos_top1) / len(pos_top1), 6) if pos_top1 else 0.0
    neg_mean = round(sum(neg_top1) / len(neg_top1), 6) if neg_top1 else 0.0
    gap = round(pos_mean - neg_mean, 6)

    metrics = ArmMetrics(
        arm_name=arm_name,
        executed=True,
        positive_query_count=len(recalls),
        negative_query_count=len(neg_top1),
        recall_at_5=round(sum(recalls) / len(recalls), 6) if recalls else None,
        mrr_at_5=round(sum(mrrs) / len(mrrs), 6) if mrrs else None,
        ndcg_at_5=round(sum(ndcgs) / len(ndcgs), 6) if ndcgs else None,
        no_hit_rate_at_5=round(sum(no_hits) / len(no_hits), 6) if no_hits else None,
        section_hit_at_5=round(sum(sec_hits) / len(sec_hits), 6) if sec_hits else None,
        pos_top1_mean=pos_mean,
        neg_top1_mean=neg_mean,
        pos_neg_top1_gap=gap,
        latency_p50_ms=p50_ms,
        latency_p95_ms=p95_ms,
        abstention_candidates=compute_abstention_candidates(pos_top1, neg_top1),
    )
    return metrics, per_query_recall


def _not_run_arm(arm_name: str, queries: Sequence[EvalQuery]) -> ArmMetrics:
    pos_count = sum(1 for q in queries if not q.is_negative)
    neg_count = sum(1 for q in queries if q.is_negative)
    return ArmMetrics(
        arm_name=arm_name,
        executed=False,
        positive_query_count=pos_count,
        negative_query_count=neg_count,
        recall_at_5=None,
        mrr_at_5=None,
        ndcg_at_5=None,
        no_hit_rate_at_5=None,
        section_hit_at_5=None,
        pos_top1_mean=None,
        neg_top1_mean=None,
        pos_neg_top1_gap=None,
        latency_p50_ms=None,
        latency_p95_ms=None,
        abstention_candidates=(),
    )


def paired_bootstrap_recall_ci(
    candidate_recalls: Mapping[str, float],
    baseline_recalls: Mapping[str, float],
    *,
    candidate_arm: str,
    baseline_arm: str,
    seed: int = 42,
    iterations: int = 1000,
) -> PairedBootstrapComparison:
    """Compute paired bootstrap 95% CI for `Recall@5(candidate) - Recall@5(baseline)`."""
    qids = sorted(set(candidate_recalls) & set(baseline_recalls))
    if not qids:
        raise ValueError("Cannot compute paired bootstrap with zero shared positive queries.")

    diffs = [candidate_recalls[qid] - baseline_recalls[qid] for qid in qids]
    n = len(diffs)
    rng = random.Random(seed)
    boot_means: list[float] = []
    for _ in range(iterations):
        sample_sum = sum(diffs[rng.randrange(n)] for _ in range(n))
        boot_means.append(sample_sum / n)
    boot_means.sort()
    low_idx = max(0, math.floor(0.025 * iterations))
    high_idx = min(iterations - 1, math.ceil(0.975 * iterations) - 1)
    mean_diff = sum(diffs) / n
    return PairedBootstrapComparison(
        candidate_arm=candidate_arm,
        baseline_arm=baseline_arm,
        mean_diff=round(mean_diff, 6),
        ci95_low=round(boot_means[low_idx], 6),
        ci95_high=round(boot_means[high_idx], 6),
        sample_count=n,
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
    base = "python -m ai_worker.tasks.evaluation.chat_rag_retrieval_ablation"
    if len(sys.argv) <= 1:
        return base
    return " ".join([base, *sys.argv[1:]])


def run_retrieval_ablation(
    *,
    corpus_path: Path = DEFAULT_CORPUS_PATH,
    cache_path: Path = DEFAULT_CACHE_PATH,
    embedder: Embedder | None = None,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    embedding_dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
    min_queries: int = DEFAULT_MIN_QUERIES,
    top_k: int = DEFAULT_TOP_K,
    pool_size: int = DEFAULT_POOL_SIZE,
    bm25_k1: float = DEFAULT_BM25_K1,
    bm25_b: float = DEFAULT_BM25_B,
    rrf_k: int = DEFAULT_RRF_K,
    max_cost_usd: float = DEFAULT_MAX_COST_USD,
    offline_bm25_only: bool = False,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
    command: str | None = None,
) -> RetrievalAblationReport:
    """Execute the 3-arm retrieval ablation (or dry-run / BM25-only) and return report."""
    _validate_positive_int("min_queries", min_queries)
    _validate_positive_int("top_k", top_k)
    _validate_positive_int("pool_size", pool_size)
    _validate_positive_int("rrf_k", rrf_k)
    _validate_positive_int("embedding_dimensions", embedding_dimensions)

    safe_cache_path = validate_cache_path_outside_repo(cache_path, repo_root=repo_root)
    chunks, corpus_sha256 = load_eval_corpus(corpus_path)
    queries = build_eval_queries(chunks, min_queries=min_queries)
    chunk_by_id = {c.chunk_id: c for c in chunks}

    config_hash = _sha256_json(
        {
            "embedding_model": embedding_model,
            "embedding_dimensions": embedding_dimensions,
            "min_queries": min_queries,
            "top_k": top_k,
            "pool_size": pool_size,
            "bm25_k1": bm25_k1,
            "bm25_b": bm25_b,
            "rrf_k": rrf_k,
        }
    )
    query_set_sha256 = _sha256_json([asdict(q) for q in queries])
    query_type_counts = dict(sorted(Counter(q.query_type for q in queries).items()))
    item_count = len({c.item_seq for c in chunks})
    effective_pool = max(top_k, pool_size)

    if dry_run:
        all_texts = list(dict.fromkeys([*(chunk_index_text(c) for c in chunks), *(q.query for q in queries)]))
        est_tokens = sum(estimate_embedding_tokens(t) for t in all_texts)
        est_cost = round((est_tokens / 1_000_000.0) * EMBEDDING_COST_PER_MILLION_TOKENS_USD, 8)
        return RetrievalAblationReport(
            eval_scope=EVAL_SCOPE,
            release_eligible=RELEASE_ELIGIBLE,
            mode="dry_run",
            corpus_path=str(corpus_path.expanduser().resolve()),
            corpus_sha256=corpus_sha256,
            corpus_chunk_count=len(chunks),
            corpus_item_count=item_count,
            query_set_version=QUERY_SET_VERSION,
            query_set_sha256=query_set_sha256,
            total_queries=len(queries),
            positive_queries=sum(1 for q in queries if not q.is_negative),
            negative_queries=sum(1 for q in queries if q.is_negative),
            query_type_counts=query_type_counts,
            config_hash=config_hash,
            top_k=top_k,
            pool_size=effective_pool,
            bm25_k1=bm25_k1,
            bm25_b=bm25_b,
            rrf_k=rrf_k,
            dense_summary=DenseEmbeddingSummary(
                executed=False,
                model=embedding_model,
                dimensions=embedding_dimensions,
                cache_path=str(safe_cache_path),
                cache_hits=0,
                cache_misses=len(all_texts),
                api_calls=0,
                prompt_tokens=0,
                estimated_tokens=est_tokens,
                estimated_cost_usd=est_cost,
                max_cost_usd=max_cost_usd,
            ),
            arms={
                "bm25_lexical": _not_run_arm("bm25_lexical", queries),
                "dense_vector": _not_run_arm("dense_vector", queries),
                "rrf_hybrid": _not_run_arm("rrf_hybrid", queries),
            },
            paired_comparisons=(),
            git_commit=_resolve_git_commit(),
            command=command if command is not None else _default_command_string(),
        )

    bm25_index = BM25Index(chunks, k1=bm25_k1, b=bm25_b)
    bm25_pools: dict[str, tuple[ScoredChunk, ...]] = {}
    bm25_latencies: list[float] = []

    for q in queries:
        t0 = time.perf_counter()
        bm25_pools[q.query_id] = bm25_index.search(q.query, top_k=effective_pool)
        bm25_latencies.append((time.perf_counter() - t0) * 1000.0)

    bm25_metrics, bm25_recalls = _aggregate_arm_metrics(
        arm_name="bm25_lexical",
        queries=queries,
        rankings_by_qid=bm25_pools,
        latencies_ms=bm25_latencies,
        chunk_by_id=chunk_by_id,
        top_k=top_k,
    )

    if offline_bm25_only:
        return RetrievalAblationReport(
            eval_scope=EVAL_SCOPE,
            release_eligible=RELEASE_ELIGIBLE,
            mode="offline_bm25_only",
            corpus_path=str(corpus_path.expanduser().resolve()),
            corpus_sha256=corpus_sha256,
            corpus_chunk_count=len(chunks),
            corpus_item_count=item_count,
            query_set_version=QUERY_SET_VERSION,
            query_set_sha256=query_set_sha256,
            total_queries=len(queries),
            positive_queries=bm25_metrics.positive_query_count,
            negative_queries=bm25_metrics.negative_query_count,
            query_type_counts=query_type_counts,
            config_hash=config_hash,
            top_k=top_k,
            pool_size=effective_pool,
            bm25_k1=bm25_k1,
            bm25_b=bm25_b,
            rrf_k=rrf_k,
            dense_summary=DenseEmbeddingSummary(
                executed=False,
                model=embedding_model,
                dimensions=embedding_dimensions,
                cache_path=str(safe_cache_path),
                cache_hits=0,
                cache_misses=0,
                api_calls=0,
                prompt_tokens=0,
                estimated_tokens=0,
                estimated_cost_usd=0.0,
                max_cost_usd=max_cost_usd,
            ),
            arms={
                "bm25_lexical": bm25_metrics,
                "dense_vector": _not_run_arm("dense_vector", queries),
                "rrf_hybrid": _not_run_arm("rrf_hybrid", queries),
            },
            paired_comparisons=(),
            git_commit=_resolve_git_commit(),
            command=command if command is not None else _default_command_string(),
        )

    active_embedder: Embedder
    if embedder is not None:
        active_embedder = embedder
        mode = "full_ablation_injected"
    else:
        api_key = read_openai_api_key(env=env)
        active_embedder = OpenAITextBatchEmbedder(api_key=api_key)
        mode = "full_ablation_live"

    corpus_texts = [chunk_index_text(c) for c in chunks]
    query_texts = [q.query for q in queries]
    vectors_by_text, dense_summary = _resolve_embeddings_with_cache(
        [*corpus_texts, *query_texts],
        cache_path=safe_cache_path,
        embedder=active_embedder,
        model=embedding_model,
        dimensions=embedding_dimensions,
        max_cost_usd=max_cost_usd,
    )

    dense_index = DenseVectorIndex(chunks, vectors_by_text)
    dense_pools: dict[str, tuple[ScoredChunk, ...]] = {}
    dense_latencies: list[float] = []
    rrf_pools: dict[str, tuple[ScoredChunk, ...]] = {}
    rrf_latencies: list[float] = []

    for idx, q in enumerate(queries):
        t0 = time.perf_counter()
        dense_pools[q.query_id] = dense_index.search_vector(vectors_by_text[q.query], top_k=effective_pool)
        dense_ms = (time.perf_counter() - t0) * 1000.0
        dense_latencies.append(dense_ms)

        t1 = time.perf_counter()
        rrf_pools[q.query_id] = rrf_fuse(
            bm25_pools[q.query_id],
            dense_pools[q.query_id],
            rrf_k=rrf_k,
            top_k=top_k,
        )
        fusion_ms = (time.perf_counter() - t1) * 1000.0
        rrf_latencies.append(bm25_latencies[idx] + dense_ms + fusion_ms)

    dense_metrics, dense_recalls = _aggregate_arm_metrics(
        arm_name="dense_vector",
        queries=queries,
        rankings_by_qid=dense_pools,
        latencies_ms=dense_latencies,
        chunk_by_id=chunk_by_id,
        top_k=top_k,
    )
    rrf_metrics, rrf_recalls = _aggregate_arm_metrics(
        arm_name="rrf_hybrid",
        queries=queries,
        rankings_by_qid=rrf_pools,
        latencies_ms=rrf_latencies,
        chunk_by_id=chunk_by_id,
        top_k=top_k,
    )

    paired = (
        paired_bootstrap_recall_ci(
            rrf_recalls,
            bm25_recalls,
            candidate_arm="rrf_hybrid",
            baseline_arm="bm25_lexical",
        ),
        paired_bootstrap_recall_ci(
            rrf_recalls,
            dense_recalls,
            candidate_arm="rrf_hybrid",
            baseline_arm="dense_vector",
        ),
        paired_bootstrap_recall_ci(
            dense_recalls,
            bm25_recalls,
            candidate_arm="dense_vector",
            baseline_arm="bm25_lexical",
        ),
    )

    return RetrievalAblationReport(
        eval_scope=EVAL_SCOPE,
        release_eligible=RELEASE_ELIGIBLE,
        mode=mode,
        corpus_path=str(corpus_path.expanduser().resolve()),
        corpus_sha256=corpus_sha256,
        corpus_chunk_count=len(chunks),
        corpus_item_count=item_count,
        query_set_version=QUERY_SET_VERSION,
        query_set_sha256=query_set_sha256,
        total_queries=len(queries),
        positive_queries=bm25_metrics.positive_query_count,
        negative_queries=bm25_metrics.negative_query_count,
        query_type_counts=query_type_counts,
        config_hash=config_hash,
        top_k=top_k,
        pool_size=effective_pool,
        bm25_k1=bm25_k1,
        bm25_b=bm25_b,
        rrf_k=rrf_k,
        dense_summary=dense_summary,
        arms={
            "bm25_lexical": bm25_metrics,
            "dense_vector": dense_metrics,
            "rrf_hybrid": rrf_metrics,
        },
        paired_comparisons=paired,
        git_commit=_resolve_git_commit(),
        command=command if command is not None else _default_command_string(),
    )


def format_report_json(report: RetrievalAblationReport) -> str:
    """Serialize `RetrievalAblationReport` as formatted UTF-8 JSON."""
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n"


def _fmt_opt(value: float | None, digits: int = 4) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def format_report_markdown(report: RetrievalAblationReport) -> str:
    """Render `RetrievalAblationReport` as a concise Markdown table report."""
    ds = report.dense_summary
    lines: list[str] = [
        "# Chat RAG Retrieval Ablation Report",
        "",
        f"- **Eval Scope**: `{report.eval_scope}` (`release_eligible={report.release_eligible}`)",
        f"- **Mode**: `{report.mode}`",
        f"- **Corpus**: `{report.corpus_path}` (`chunks={report.corpus_chunk_count}`, `items={report.corpus_item_count}`, `sha256={report.corpus_sha256[:16]}...`)",
        f"- **Query Set**: `{report.query_set_version}` (`total={report.total_queries}`, `pos={report.positive_queries}`, `neg={report.negative_queries}`, `sha256={report.query_set_sha256[:16]}...`)",
        f"- **Config Hash**: `{report.config_hash[:16]}...` (`top_k={report.top_k}`, `pool_size={report.pool_size}`, `bm25=({report.bm25_k1}, {report.bm25_b})`, `rrf_k={report.rrf_k}`)",
        f"- **Dense Embeddings**: `model={ds.model}`, `dims={ds.dimensions}`, `cache_hits={ds.cache_hits}`, `cache_misses={ds.cache_misses}`, `prompt_tokens={ds.prompt_tokens}`, `cost_usd=${ds.estimated_cost_usd:.6f}`",
        f"- **Git Commit**: `{report.git_commit}`",
        f"- **Command**: `{report.command}`",
        "",
        "## Retrieval Arms Comparison",
        "",
        "| Arm | Executed | Recall@5 | MRR@5 | nDCG@5 | No-hit@5 | SecHit@5 | PosTop1 | NegTop1 | Gap | P50 (ms) | P95 (ms) |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for arm_key in ("bm25_lexical", "dense_vector", "rrf_hybrid"):
        arm = report.arms[arm_key]
        lines.append(
            f"| `{arm.arm_name}` | `{arm.executed}` | {_fmt_opt(arm.recall_at_5)} | "
            f"{_fmt_opt(arm.mrr_at_5)} | {_fmt_opt(arm.ndcg_at_5)} | {_fmt_opt(arm.no_hit_rate_at_5)} | "
            f"{_fmt_opt(arm.section_hit_at_5)} | {_fmt_opt(arm.pos_top1_mean)} | "
            f"{_fmt_opt(arm.neg_top1_mean)} | {_fmt_opt(arm.pos_neg_top1_gap)} | "
            f"{_fmt_opt(arm.latency_p50_ms, 3)} | {_fmt_opt(arm.latency_p95_ms, 3)} |"
        )

    if report.paired_comparisons:
        lines.extend(
            [
                "",
                "## Paired Bootstrap 95% CI (Recall@5 Difference)",
                "",
                "| Candidate | Baseline | Mean Diff | 95% CI Low | 95% CI High | N |",
                "| --- | --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for comp in report.paired_comparisons:
            lines.append(
                f"| `{comp.candidate_arm}` | `{comp.baseline_arm}` | {comp.mean_diff:+.4f} | "
                f"{comp.ci95_low:+.4f} | {comp.ci95_high:+.4f} | {comp.sample_count} |"
            )

    lines.append("")
    return "\n".join(lines)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run BM25 vs Dense vs RRF Hybrid retrieval ablation over local MFDS corpus."
    )
    parser.add_argument(
        "--corpus-path",
        type=Path,
        default=DEFAULT_CORPUS_PATH,
        help=f"Path to MFDS corpus_chunks.jsonl (default: {DEFAULT_CORPUS_PATH}).",
    )
    parser.add_argument(
        "--cache-path",
        type=Path,
        default=DEFAULT_CACHE_PATH,
        help=f"Persistent JSON embedding cache path outside git repo (default: {DEFAULT_CACHE_PATH}).",
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="markdown",
        dest="output_format",
        help="Report output format (json or markdown).",
    )
    parser.add_argument(
        "--offline-bm25-only",
        action="store_true",
        help="Run only BM25 lexical arm without calling OpenAI embeddings.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate corpus, build queries, estimate embedding tokens/cost, and exit without API calls.",
    )
    parser.add_argument(
        "--min-queries",
        type=int,
        default=DEFAULT_MIN_QUERIES,
        help=f"Minimum total evaluation queries to construct (default: {DEFAULT_MIN_QUERIES}).",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=DEFAULT_MAX_COST_USD,
        help=f"Maximum allowed embedding cost USD for this run (default: {DEFAULT_MAX_COST_USD}).",
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
    embedder: Embedder | None = None,
) -> int:
    """CLI entrypoint for `ai_worker.tasks.evaluation.chat_rag_retrieval_ablation`."""
    args = _build_arg_parser().parse_args(list(argv) if argv is not None else None)
    cli_command = (
        " ".join(["python -m ai_worker.tasks.evaluation.chat_rag_retrieval_ablation", *argv])
        if argv is not None
        else _default_command_string()
    )
    try:
        report = run_retrieval_ablation(
            corpus_path=args.corpus_path,
            cache_path=args.cache_path,
            embedder=embedder,
            min_queries=args.min_queries,
            max_cost_usd=args.max_cost_usd,
            offline_bm25_only=args.offline_bm25_only,
            dry_run=args.dry_run,
            command=cli_command,
        )
        rendered = format_report_json(report) if args.output_format == "json" else format_report_markdown(report)
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
    except (FileNotFoundError, OSError, ValueError, RuntimeError, TypeError) as exc:
        sys.stderr.write(f"chat_rag_retrieval_ablation error: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
