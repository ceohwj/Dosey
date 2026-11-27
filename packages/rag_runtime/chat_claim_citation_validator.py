"""Pure runtime claim-to-evidence citation validator (`rule_plus_nli`).

Zero imports from `ai_worker` or `app` so Backend and evaluation lanes can
share the deterministic Korean medication safety guard + NLI verification
boundary without violating package import boundaries.
"""

from __future__ import annotations

import asyncio
import importlib
import math
import re
import threading
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

DEFAULT_NLI_MODEL_NAME = "MoritzLaurer/mDeBERTa-v3-base-mnli-xnli"
DEFAULT_MAX_SENTENCES = 3
DEFAULT_MAX_TOKENS = 512
DEFAULT_MAX_CHARS = 512

NliTriple = tuple[float, float, float]
NliPairScorer = Callable[[str, str], NliTriple]
ClaimKindInputLiteral = Literal["critical", "non_critical"]
ClaimKindOutputLiteral = Literal["CRITICAL", "NON_CRITICAL"]
NliVerdictLiteral = Literal["support", "neutral", "contradict"]
CitationVerdictLiteral = Literal["SUPPORTED", "NEEDS_VERIFICATION"]

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=(?<!\d)\.)\s+|(?<=[!?。！？…])\s+|\n+")
_CLAUSE_SPLIT_RE = re.compile(r"(?<=(?<!\d)[.!?。！？])\s+|(?<=[,;:])\s+|\n+")
_TOKEN_ESTIMATE_RE = re.compile(r"[가-힣]|[0-9a-zA-Z]+|[^\s\w]")
_TOKEN_WORD_RE = re.compile(r"[가-힣]{1,}|[0-9]+(?:\.[0-9]+)?|[a-zA-Z]+")
_HANGUL_SEQ_RE = re.compile(r"[가-힣]+")
_WHITESPACE_RE = re.compile(r"\s+")

_DIGIT_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(회|정|캡슐|알|포|병|일|주|개월|시간|분|세|mg|mcg|ml|mL|g|%|℃)")
_DAILY_FREQ_RE = re.compile(r"(?:1일|하루(?:에)?)\s*(\d+(?:\.\d+)?)\s*(?:회|번)")
_PER_DOSE_TAB_RE = re.compile(r"(?:1회|한\s*번에)\s*(\d+(?:\.\d+)?)\s*(?:정|알|캡슐)")
_DAILY_MAX_TAB_RE = re.compile(
    r"(?:(?:1일|하루)\s*최대\s*(\d+(?:\.\d+)?)\s*(?:정|알|캡슐))"
    r"|(?:(\d+(?:\.\d+)?)\s*(?:정|알|캡슐)(?:을|를)?\s*(?:초과|넘))"
)

_NATIVE_COUNT_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"하루(?:에)?\s*한\s*번"), "1일 1회"),
    (re.compile(r"하루(?:에)?\s*두\s*번"), "1일 2회"),
    (re.compile(r"하루(?:에)?\s*세\s*번"), "1일 3회"),
    (re.compile(r"하루(?:에)?\s*네\s*번"), "1일 4회"),
    (re.compile(r"한\s*번에\s*한\s*(?:알|정)"), "1회 1정"),
    (re.compile(r"한\s*번에\s*두\s*(?:알|정)"), "1회 2정"),
    (re.compile(r"한\s*(?:알|정)씩"), "1회 1정"),
    (re.compile(r"두\s*(?:알|정)씩"), "1회 2정"),
    (re.compile(r"세\s*(?:알|정)씩"), "1회 3정"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:알|정|캡슐)씩"), r"1회 \1정"),
    (re.compile(r"반\s*(?:알|정)"), "0.5정"),
)

_CRITICAL_SAFETY_HINTS: tuple[str, ...] = (
    "금기",
    "투여하지",
    "복용하지",
    "복용 금지",
    "병용 금지",
    "중복",
    "초과하지",
    "최대",
    "임부",
    "임산부",
    "수유부",
    "소아",
    "어린이",
    "식후",
    "식전",
    "취침 전",
    "공복",
    "빈속",
    "간격",
    "상의",
    "피하",
    "운전",
    "음주",
)

_PROHIBITION_MARKERS: tuple[str, ...] = (
    "금기",
    "금지",
    "투여하지 않",
    "투여하지 마",
    "복용하지 않",
    "복용하지 마",
    "먹지 않",
    "먹지 마",
    "닿지 않",
    "피하",
    "피해야",
    "피한다",
    "삼가",
    "권장하지 않",
    "해서는 안",
    "하면 안",
    "초과하지 마",
    "넘기지 마",
    "옮겨 담지 마",
    "버리지 마",
    "복용하지 말고",
    "포함되어 있지 않",
    "전혀 포함되어 있지 않",
    "아닙니다",
    "불가",
)

_CONSULT_MARKERS: tuple[str, ...] = (
    "상의하",
    "상의해",
    "상담하",
    "상담을",
    "상담이",
)

_PERMISSIVE_MARKERS: tuple[str, ...] = (
    "안전하",
    "무방",
    "가능하",
    "복용 가능",
    "사용 가능",
    "투여 가능",
    "복용할 수 있",
    "복용해도",
    "먹어도",
    "사용해도",
    "지장이 없",
    "문제없",
    "부담이 없",
    "괜찮",
    "상담 없이",
    "진료 없이",
    "임의로 복용",
    "함께 복용해도",
    "동시에 중복 복용해도",
)

_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "pregnancy": ("임부", "임산부", "임신"),
    "lactation": ("수유부", "수유"),
    "pediatric": ("소아", "어린이", "아이"),
    "duplicate": ("중복", "병용", "함께 복용", "다른 제품", "동일 성분"),
    "alcohol_drive": ("음주", "술", "운전", "기계 조작"),
    "fasting": ("공복", "빈속"),
    "meal_timing": ("식전", "식후", "식사 전", "식사 후", "식사 직후", "취침 전", "취침전", "자기 전"),
    "expiry": ("유효기간", "사용기한"),
    "storage_temp": ("실온", "냉동", "얼려", "고온"),
    "storage_light": ("직사광선", "햇빛", "창가"),
    "storage_container": ("원래의 포장", "다른 용기", "옮겨 담"),
    "storage_humidity": ("습기", "욕실", "건조"),
    "disposal": ("폐기", "폐의약품", "하수구", "세면대", "수거함", "버리"),
    "appearance_defect": ("변색", "균열", "붕해", "색이 변"),
    "lactose": ("유당",),
}

_UNSAFE_DIRECT_ADVICE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "unsafe_stop_medication",
        re.compile(
            r"(?:복용을|약을)\s*(?:중단|멈추|멈춰|끊)\s*(?:하세요|하십시오|세요|십시오|해도|하셔도|해도\s*됩니다|할\s*수\s*있|으세요|으십시오|어도|도|으셔도|을\s*수\s*있|면\s*됩니다)"
        ),
    ),
    (
        "unsafe_stop_medication",
        re.compile(r"(?:건너뛰|복용하지\s*않)\s*(?:세요|셔도|어도|면\s*됩니다|으셔도)"),
    ),
    (
        "unsafe_dose_increase",
        re.compile(
            r"용량을\s*(?:늘리(?:세요|십시오|셔도|어도|면)|늘려(?:도|서|야)|증량\s*(?:하세요|해도|하셔도|할\s*수))"
        ),
    ),
    (
        "unsafe_dose_decrease",
        re.compile(
            r"(?:용량을|한\s*알씩|반으로)\s*(?:줄이(?:세요|십시오|셔도|어도|면)|줄여(?:도|서|야)|감량\s*(?:하세요|해도|하셔도|할\s*수))"
        ),
    ),
    (
        "unsafe_double_dose",
        re.compile(
            r"(?:두\s*배|2배)(?:로)?\s*(?:드세요|드셔도|복용하세요|복용하십시오|복용해도|복용하셔도|먹어도|먹으세요)"
        ),
    ),
)

_UNSAFE_MOD_PHRASES: tuple[str, ...] = (
    "임의로 중단",
    "임의 중단",
    "임의 증량",
    "임의 감량",
    "임의로 증량",
    "임의로 감량",
    "임의로 늘",
    "임의로 줄",
)

_ADVICE_NEGATION_MARKERS: tuple[str, ...] = (
    "하지 마",
    "하지 않",
    "하면 안",
    "해서는 안",
    "금지",
    "피하",
    "삼가",
    "않아야",
    "안 됩니다",
    "말아야",
    "말고",
    "마세요",
    "마십시오",
    "않도록",
)


class CriticalClaimContradictionError(RuntimeError):
    """Raised fail-closed when a critical claim contradicts evidence or gives unsafe advice."""


class NliScorerUnavailableError(RuntimeError):
    """Raised fail-closed when local mDeBERTa dependencies are missing."""


@dataclass(frozen=True, slots=True)
class ClaimEvidenceRecord:
    """Hydrated evidence chunk passed into the runtime citation validator."""

    chunk_id: str
    knowledge_chunk_id: UUID | None
    source_code: str
    source_version: str
    locator: str
    content: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class ClaimCandidate:
    """Structured claim emitted by the chat generator prior to verification."""

    claim_key: str
    claim_text: str
    claim_kind: ClaimKindInputLiteral
    evidence_chunk_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ValidatedClaimRecord:
    """Fail-closed validated citation ready for backend DTO/repository mapping."""

    claim_key: str
    claim_text: str
    claim_kind: ClaimKindOutputLiteral
    verdict: CitationVerdictLiteral
    source_code: str
    source_version: str
    locator: str
    display_order: int
    knowledge_chunk_id: UUID | None = None
    content_sha256: str | None = None
    cited_text: str | None = None


@dataclass(frozen=True, slots=True)
class _ExtractedFacts:
    daily_freq: frozenset[float]
    per_dose_tab: frozenset[float]
    daily_max_tab: frozenset[float]
    unit_values: dict[str, frozenset[float]]
    has_before_meal: bool
    has_after_meal: bool
    has_bedtime: bool
    has_fasting_allow: bool
    has_fasting_forbid: bool
    has_prohibition: bool
    has_permissive: bool
    has_consultation_required: bool
    has_presence_affirmative: bool
    topics: frozenset[str]


def _normalize_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFC", text)).strip()


def _normalize_korean_text(text: str) -> str:
    normalized = unicodedata.normalize("NFC", text).strip()
    for pattern, replacement in _NATIVE_COUNT_REPLACEMENTS:
        normalized = pattern.sub(replacement, normalized)
    return normalized


def _estimate_token_count(text: str) -> int:
    if not text or not text.strip():
        return 0
    return len(_TOKEN_ESTIMATE_RE.findall(text))


def _split_sentences(text: str) -> tuple[str, ...]:
    stripped = unicodedata.normalize("NFC", text).strip()
    if not stripped:
        return ()
    return tuple(part.strip() for part in _SENTENCE_SPLIT_RE.split(stripped) if part.strip())


def _lexical_features(text: str) -> set[str]:
    normalized = _normalize_korean_text(text).lower()
    features: set[str] = set()
    for token in _TOKEN_WORD_RE.findall(normalized):
        features.add(token)
    for num, unit in _DIGIT_UNIT_RE.findall(normalized):
        features.add(f"{float(num):g}{unit}")
    for hangul_word in _HANGUL_SEQ_RE.findall(normalized):
        if len(hangul_word) >= 2:
            for idx in range(len(hangul_word) - 1):
                features.add(hangul_word[idx : idx + 2])
    return features


def _truncate_text_to_budget(text: str, *, max_tokens: int, max_chars: int) -> str:
    if max_tokens <= 0 or max_chars <= 0:
        return ""
    capped = text[:max_chars].strip()
    if _estimate_token_count(capped) <= max_tokens:
        return capped
    lo, hi = 1, len(capped)
    best = capped[:1]
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = capped[:mid].strip()
        if _estimate_token_count(candidate) <= max_tokens:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _score_sentence_overlap(
    *,
    sentence: str,
    claim: str,
    claim_features: set[str],
    claim_digits: set[str],
) -> float:
    if not claim_features:
        return 0.0
    sent_features = _lexical_features(sentence)
    overlap = len(claim_features & sent_features)
    jaccard_like = overlap / math.sqrt(max(1, len(claim_features)) * max(1, len(sent_features)))
    sent_digits = {f"{float(num):g}{unit}" for num, unit in _DIGIT_UNIT_RE.findall(_normalize_korean_text(sentence))}
    digit_bonus = 0.5 * len(claim_digits & sent_digits)
    safety_bonus = 0.15 if any(hint in sentence and hint in claim for hint in _CRITICAL_SAFETY_HINTS) else 0.0
    return jaccard_like + digit_bonus + safety_bonus


def _pack_sentences_within_budget(
    sentences: Sequence[str],
    top_indices: Sequence[int],
    *,
    token_budget: int,
    char_budget: int,
) -> tuple[str, ...]:
    selected: list[str] = []
    used_tokens = 0
    used_chars = 0
    for idx in top_indices:
        sentence = sentences[idx]
        sent_tokens = _estimate_token_count(sentence)
        separator = 1 if selected else 0
        if used_tokens + sent_tokens <= token_budget and used_chars + separator + len(sentence) <= char_budget:
            selected.append(sentence)
            used_tokens += sent_tokens
            used_chars += separator + len(sentence)
        elif not selected:
            trimmed = _truncate_text_to_budget(sentence, max_tokens=token_budget, max_chars=char_budget)
            if trimmed:
                selected.append(trimmed)
            break

    if not selected and top_indices:
        trimmed = _truncate_text_to_budget(sentences[top_indices[0]], max_tokens=token_budget, max_chars=char_budget)
        if trimmed:
            selected.append(trimmed)
    return tuple(selected)


def select_top_k_evidence_sentences(
    *,
    claim: str,
    evidence: str,
    max_sentences: int = DEFAULT_MAX_SENTENCES,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> tuple[str, ...]:
    """Select up to `max_sentences` overlapping sentences within token/char limits."""
    if max_sentences <= 0:
        raise ValueError("max_sentences must be a positive integer")
    if max_tokens <= 0 or max_chars <= 0:
        raise ValueError("max_tokens and max_chars must be positive integers")

    sentences = _split_sentences(evidence)
    if not sentences:
        return ()

    claim_tokens = _estimate_token_count(claim)
    token_budget = max(16, max_tokens - claim_tokens - 8)
    joined = " ".join(sentences)
    if len(sentences) <= max_sentences and _estimate_token_count(joined) <= token_budget and len(joined) <= max_chars:
        return sentences

    claim_features = _lexical_features(claim)
    claim_digits = {f"{float(num):g}{unit}" for num, unit in _DIGIT_UNIT_RE.findall(_normalize_korean_text(claim))}
    scored = [
        (
            _score_sentence_overlap(
                sentence=sentence,
                claim=claim,
                claim_features=claim_features,
                claim_digits=claim_digits,
            ),
            -idx,
        )
        for idx, sentence in enumerate(sentences)
    ]
    top_indices = sorted(sorted(range(len(sentences)), key=lambda i: scored[i], reverse=True)[:max_sentences])
    return _pack_sentences_within_budget(sentences, top_indices, token_budget=token_budget, char_budget=max_chars)


def _contains_unsafe_modification_advice(text: str) -> str | None:
    """Detect unsafe instructions telling a patient to stop, double, or alter dose."""
    normalized = _normalize_whitespace(text)
    if not normalized:
        return None
    for reason, pattern in _UNSAFE_DIRECT_ADVICE_PATTERNS:
        if pattern.search(normalized):
            return reason
    for clause in _CLAUSE_SPLIT_RE.split(normalized):
        if any(phrase in clause for phrase in _UNSAFE_MOD_PHRASES) and not any(
            neg in clause for neg in _ADVICE_NEGATION_MARKERS
        ):
            return "unsafe_arbitrary_medication_modification"
    return None


def _extract_facts(text: str) -> _ExtractedFacts:
    normalized = _normalize_korean_text(text)
    daily_freq = frozenset(float(m) for m in _DAILY_FREQ_RE.findall(normalized))
    per_dose_tab = frozenset(float(m) for m in _PER_DOSE_TAB_RE.findall(normalized))
    daily_max_tab = frozenset(float(g1 or g2) for g1, g2 in _DAILY_MAX_TAB_RE.findall(normalized) if (g1 or g2))

    unit_buckets: dict[str, set[float]] = {}
    for num_str, unit in _DIGIT_UNIT_RE.findall(normalized):
        if unit in {"회", "정", "분"}:
            continue
        bucket_key = "temp_c" if unit == "℃" else ("dosage_form" if unit in {"캡슐", "알"} else unit.lower())
        unit_buckets.setdefault(bucket_key, set()).add(float(num_str))

    has_fasting_word = "공복" in normalized or "빈속" in normalized
    fasting_negated = has_fasting_word and any(
        marker in normalized
        for marker in (
            "공복 복용은 피하",
            "공복 시 복용을 피하",
            "공복에 복용하지",
            "빈속에 먹지 않",
            "빈속에 복용하지",
            "피하십시오",
            "피한다",
        )
    )
    topics = frozenset(
        topic for topic, keywords in _TOPIC_KEYWORDS.items() if any(keyword in normalized for keyword in keywords)
    )
    has_prohibition = any(marker in normalized for marker in _PROHIBITION_MARKERS)
    has_consultation = any(marker in normalized for marker in _CONSULT_MARKERS) and "상담 없이" not in normalized
    has_presence_affirmative = (
        ("함유하고 있" in normalized or "포함되어 있" in normalized or "들어 있" in normalized)
        and "포함되어 있지 않" not in normalized
        and "함유하고 있지 않" not in normalized
    )

    return _ExtractedFacts(
        daily_freq=daily_freq,
        per_dose_tab=per_dose_tab,
        daily_max_tab=daily_max_tab,
        unit_values={k: frozenset(v) for k, v in unit_buckets.items()},
        has_before_meal="식전" in normalized or "식사 전" in normalized,
        has_after_meal="식후" in normalized or "식사 후" in normalized or "식사 직후" in normalized,
        has_bedtime="취침 전" in normalized or "취침전" in normalized or "자기 전" in normalized,
        has_fasting_allow=has_fasting_word and not fasting_negated,
        has_fasting_forbid=has_fasting_word and fasting_negated,
        has_prohibition=has_prohibition,
        has_permissive=any(marker in normalized for marker in _PERMISSIVE_MARKERS),
        has_consultation_required=has_consultation,
        has_presence_affirmative=has_presence_affirmative,
        topics=topics,
    )


def _sets_conflict(left: frozenset[float], right: frozenset[float]) -> bool:
    return bool(left) and bool(right) and left.isdisjoint(right)


def _check_timing_conflict(claim_facts: _ExtractedFacts, ev_facts: _ExtractedFacts) -> str | None:
    if (claim_facts.has_before_meal and ev_facts.has_after_meal) or (
        claim_facts.has_after_meal and ev_facts.has_before_meal
    ):
        return "rule:meal_timing_conflict"
    if (
        claim_facts.has_bedtime and (ev_facts.has_before_meal or ev_facts.has_after_meal) and not ev_facts.has_bedtime
    ) or (
        ev_facts.has_bedtime
        and (claim_facts.has_before_meal or claim_facts.has_after_meal)
        and not claim_facts.has_bedtime
    ):
        return "rule:bedtime_timing_conflict"
    if (claim_facts.has_fasting_allow and ev_facts.has_fasting_forbid) or (
        claim_facts.has_fasting_forbid and ev_facts.has_fasting_allow
    ):
        return "rule:fasting_polarity_conflict"
    if claim_facts.has_fasting_allow and ev_facts.has_after_meal and not ev_facts.has_fasting_allow:
        return "rule:fasting_vs_after_meal_conflict"
    return None


def _check_numeric_conflict(claim_facts: _ExtractedFacts, ev_facts: _ExtractedFacts) -> str | None:
    if _sets_conflict(claim_facts.daily_freq, ev_facts.daily_freq):
        return "rule:daily_frequency_conflict"
    if _sets_conflict(claim_facts.per_dose_tab, ev_facts.per_dose_tab):
        return "rule:per_dose_tablet_conflict"
    if _sets_conflict(claim_facts.daily_max_tab, ev_facts.daily_max_tab):
        return "rule:daily_max_tablet_conflict"
    for unit, claim_vals in claim_facts.unit_values.items():
        ev_vals = ev_facts.unit_values.get(unit)
        if ev_vals and claim_vals.isdisjoint(ev_vals):
            return f"rule:numeric_unit_conflict({unit})"
    return None


def _check_polarity_conflict(claim_facts: _ExtractedFacts, ev_facts: _ExtractedFacts) -> str | None:
    shared_topics = claim_facts.topics & ev_facts.topics
    if not shared_topics:
        return None
    topics_tag = ",".join(sorted(shared_topics))
    ev_restrictive = ev_facts.has_prohibition or ev_facts.has_consultation_required
    claim_restrictive = claim_facts.has_prohibition or claim_facts.has_consultation_required
    if ev_restrictive and claim_facts.has_permissive and not claim_restrictive:
        return f"rule:prohibition_flipped_to_permissive({topics_tag})"
    if (
        claim_facts.has_prohibition
        and (ev_facts.has_permissive or ev_facts.has_presence_affirmative)
        and not ev_facts.has_prohibition
    ):
        return f"rule:presence_or_permissive_flipped_to_prohibition({topics_tag})"
    return None


def _check_unverified_critical_support(claim_facts: _ExtractedFacts, ev_facts: _ExtractedFacts) -> str | None:
    if claim_facts.daily_freq and not ev_facts.daily_freq:
        return "rule:unverified_critical_daily_frequency"
    if claim_facts.per_dose_tab and not ev_facts.per_dose_tab:
        return "rule:unverified_critical_per_dose"
    if claim_facts.daily_max_tab and not ev_facts.daily_max_tab:
        return "rule:unverified_critical_daily_max"
    for unit in claim_facts.unit_values:
        if unit not in ev_facts.unit_values:
            return f"rule:unverified_critical_unit({unit})"
    if ev_facts.has_prohibition and not claim_facts.has_prohibition:
        return "rule:critical_prohibition_omitted_in_claim"
    if ev_facts.has_consultation_required and not (
        claim_facts.has_consultation_required or claim_facts.has_prohibition
    ):
        return "rule:critical_consultation_omitted_in_claim"
    return None


def _apply_softmax(values: Sequence[float]) -> list[float]:
    max_val = max(values)
    exps = [math.exp(val - max_val) for val in values]
    total = sum(exps)
    return [val / total for val in exps] if total > 0.0 else [0.0 for _ in values]


def _normalize_nli_triple(raw_scores: NliTriple) -> NliTriple:
    if len(raw_scores) != 3:
        raise ValueError(f"NLI scorer must return 3 scores (support, neutral, contradict), got {len(raw_scores)}")
    values = [float(score) for score in raw_scores]
    if any(not math.isfinite(score) for score in values):
        raise ValueError("NLI scorer returned non-finite score")
    if any(score < 0.0 or score > 1.0 for score in values) or abs(sum(values) - 1.0) > 1e-2:
        values = _apply_softmax(values)
    return values[0], values[1], values[2]


def _select_nli_verdict(
    scores: NliTriple,
    *,
    support_threshold: float,
    contradict_threshold: float,
) -> NliVerdictLiteral:
    support_p, neutral_p, contradict_p = scores
    if contradict_p >= contradict_threshold:
        return "contradict"
    if support_p >= support_threshold and support_p > neutral_p and support_p > contradict_p:
        return "support"
    return "neutral"


def _resolve_id2label_indices(raw_id2label: Mapping[Any, Any] | None) -> tuple[int, int, int]:
    if not raw_id2label:
        return 0, 1, 2
    role_indices: dict[str, int] = {}
    for raw_idx, raw_label in raw_id2label.items():
        idx = int(str(raw_idx).lower().removeprefix("label_"))
        label = str(raw_label).strip().lower()
        if label in {"entailment", "entails", "entailed", "support", "supports", "supported"}:
            role_indices["support"] = idx
        elif label in {"neutral", "neither"}:
            role_indices["neutral"] = idx
        elif label in {"contradiction", "contradict", "contradicts", "contradicted"}:
            role_indices["contradict"] = idx
    if set(role_indices) != {"support", "neutral", "contradict"}:
        return 0, 1, 2
    return role_indices["support"], role_indices["neutral"], role_indices["contradict"]


def load_mdeberta_nli_scorer(
    *,
    model_name: str = DEFAULT_NLI_MODEL_NAME,
    device: str = "cpu",
) -> NliPairScorer:
    """Lazily load local HuggingFace `CrossEncoder` NLI scorer or fail closed."""
    try:
        st_module = importlib.import_module("sentence_transformers")
        cross_encoder_cls = st_module.CrossEncoder
    except (ImportError, AttributeError) as exc:
        raise NliScorerUnavailableError(
            "Missing `sentence-transformers` for mDeBERTa NLI citation verification."
        ) from exc

    try:
        model = cross_encoder_cls(model_name, device=device)
    except Exception as exc:
        raise NliScorerUnavailableError(f"Failed to load local mDeBERTa NLI scorer ({model_name}).") from exc
    config = getattr(getattr(model, "model", None), "config", None) or getattr(model, "config", None)
    raw_id2label = getattr(config, "id2label", None)
    support_idx, neutral_idx, contradict_idx = _resolve_id2label_indices(
        raw_id2label if isinstance(raw_id2label, Mapping) else None
    )

    def _score_pair(premise: str, hypothesis: str) -> NliTriple:
        try:
            rows = model.predict([(premise, hypothesis)], show_progress_bar=False)
            row = rows[0]
            values = [float(val) for val in (row.tolist() if hasattr(row, "tolist") else list(row))]
        except Exception as exc:
            raise NliScorerUnavailableError("Local mDeBERTa NLI scorer inference failed.") from exc
        probs = (
            _apply_softmax(values)
            if any(v < 0.0 or v > 1.0 for v in values) or abs(sum(values) - 1.0) > 1e-2
            else values
        )
        return probs[support_idx], probs[neutral_idx], probs[contradict_idx]

    return _score_pair


def _is_valid_persisted_evidence(ev: ClaimEvidenceRecord) -> bool:
    return ev.knowledge_chunk_id is not None and bool(_SHA256_HEX_RE.fullmatch(ev.content_sha256))


class RulePlusNliClaimValidator:
    """Fail-closed `rule_plus_nli` claim-to-evidence citation validator."""

    def __init__(
        self,
        scorer: NliPairScorer | None = None,
        *,
        support_threshold: float = 0.50,
        contradict_threshold: float = 0.45,
        max_sentences: int = DEFAULT_MAX_SENTENCES,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        if not math.isfinite(support_threshold) or not 0.0 < support_threshold <= 1.0:
            raise ValueError("support_threshold must be in (0, 1]")
        if not math.isfinite(contradict_threshold) or not 0.0 < contradict_threshold <= 1.0:
            raise ValueError("contradict_threshold must be in (0, 1]")
        if max_sentences <= 0 or max_tokens <= 0 or max_chars <= 0:
            raise ValueError("max_sentences, max_tokens, and max_chars must be positive integers")

        self._scorer = scorer
        self._scorer_lock = threading.Lock()
        self._support_threshold = support_threshold
        self._contradict_threshold = contradict_threshold
        self._max_sentences = max_sentences
        self._max_tokens = max_tokens
        self._max_chars = max_chars

    def _ensure_scorer(self) -> NliPairScorer:
        if self._scorer is None:
            with self._scorer_lock:
                if self._scorer is None:
                    self._scorer = load_mdeberta_nli_scorer()
        return self._scorer

    def _evaluate_pair(
        self,
        *,
        claim_text: str,
        claim_kind: ClaimKindInputLiteral,
        evidence_text: str,
    ) -> tuple[NliVerdictLiteral, float]:
        selected = select_top_k_evidence_sentences(
            claim=claim_text,
            evidence=evidence_text,
            max_sentences=self._max_sentences,
            max_tokens=self._max_tokens,
            max_chars=self._max_chars,
        )
        if not selected:
            return "neutral", 0.0

        premise = " ".join(selected)
        claim_facts = _extract_facts(claim_text)
        ev_facts = _extract_facts(premise)

        for conflict in (
            _check_timing_conflict(claim_facts, ev_facts),
            _check_numeric_conflict(claim_facts, ev_facts),
            _check_polarity_conflict(claim_facts, ev_facts),
        ):
            if conflict is not None:
                return "contradict", 0.0

        scorer = self._ensure_scorer()
        # NLI expects `(premise, hypothesis)` = `(evidence_snippet, claim_text)`.
        scores = _normalize_nli_triple(scorer(premise, claim_text))
        verdict = _select_nli_verdict(
            scores,
            support_threshold=self._support_threshold,
            contradict_threshold=self._contradict_threshold,
        )
        if verdict == "support" and claim_kind == "critical":
            if _check_unverified_critical_support(claim_facts, ev_facts) is not None:
                return "neutral", scores[0]
        return verdict, scores[0]

    def _evaluate_claim_against_evidence(
        self,
        *,
        claim: ClaimCandidate,
        evidence_by_id: Mapping[str, ClaimEvidenceRecord],
    ) -> tuple[NliVerdictLiteral, ClaimEvidenceRecord | None]:
        matched_records = [
            evidence_by_id[chunk_id] for chunk_id in claim.evidence_chunk_ids if chunk_id in evidence_by_id
        ]
        if not matched_records:
            return "neutral", None

        best_supported: tuple[tuple[bool, float], ClaimEvidenceRecord] | None = None
        for ev in matched_records:
            verdict, support_score = self._evaluate_pair(
                claim_text=claim.claim_text,
                claim_kind=claim.claim_kind,
                evidence_text=ev.content,
            )
            if verdict == "contradict":
                return "contradict", ev
            if verdict == "support":
                rank = (_is_valid_persisted_evidence(ev), support_score)
                if best_supported is None or rank > best_supported[0]:
                    best_supported = (rank, ev)

        if best_supported is not None:
            return "support", best_supported[1]
        return "neutral", matched_records[0]

    def _validate_single_claim(
        self,
        *,
        claim: ClaimCandidate,
        evidence_by_id: Mapping[str, ClaimEvidenceRecord],
        display_order: int,
    ) -> ValidatedClaimRecord | None:
        unsafe_claim = _contains_unsafe_modification_advice(claim.claim_text)
        if unsafe_claim is not None:
            raise CriticalClaimContradictionError(
                f"Unsafe medication advice in claim {claim.claim_key}: {unsafe_claim}"
            )

        verdict, anchor_ev = self._evaluate_claim_against_evidence(claim=claim, evidence_by_id=evidence_by_id)
        if verdict == "contradict":
            if claim.claim_kind == "critical":
                raise CriticalClaimContradictionError(f"Critical claim contradicted by evidence: {claim.claim_key}")
            return None
        if verdict == "support":
            if anchor_ev is not None and _is_valid_persisted_evidence(anchor_ev):
                return _build_supported_record(claim=claim, ev=anchor_ev, display_order=display_order)
            return None
        if claim.claim_kind == "non_critical" and anchor_ev is not None:
            return _build_needs_verification_record(claim=claim, ev=anchor_ev, display_order=display_order)
        return None

    def validate(
        self,
        *,
        claims: Sequence[ClaimCandidate],
        evidence: Sequence[ClaimEvidenceRecord],
        answer_text: str | None = None,
    ) -> list[ValidatedClaimRecord]:
        """Validate structured claims against hydrated evidence fail-closed (A1 policy)."""
        if answer_text is not None:
            unsafe_answer = _contains_unsafe_modification_advice(answer_text)
            if unsafe_answer is not None:
                raise CriticalClaimContradictionError(f"Unsafe medication advice in answer: {unsafe_answer}")

        evidence_by_id = {record.chunk_id: record for record in evidence}
        validated: list[ValidatedClaimRecord] = []
        for claim in claims:
            record = self._validate_single_claim(
                claim=claim,
                evidence_by_id=evidence_by_id,
                display_order=len(validated) + 1,
            )
            if record is not None:
                validated.append(record)
        return validated

    async def validate_async(
        self,
        *,
        claims: Sequence[ClaimCandidate],
        evidence: Sequence[ClaimEvidenceRecord],
        answer_text: str | None = None,
    ) -> list[ValidatedClaimRecord]:
        """Run CPU-bound NLI + rule validation in a worker thread without blocking the event loop."""
        return await asyncio.to_thread(
            self.validate,
            claims=claims,
            evidence=evidence,
            answer_text=answer_text,
        )


def _build_supported_record(
    *,
    claim: ClaimCandidate,
    ev: ClaimEvidenceRecord,
    display_order: int,
) -> ValidatedClaimRecord:
    cited_text = _normalize_whitespace(ev.content)[:2000] or None
    return ValidatedClaimRecord(
        claim_key=claim.claim_key,
        claim_text=claim.claim_text,
        claim_kind="CRITICAL" if claim.claim_kind == "critical" else "NON_CRITICAL",
        verdict="SUPPORTED",
        source_code=ev.source_code,
        source_version=ev.source_version,
        locator=ev.locator,
        display_order=display_order,
        knowledge_chunk_id=ev.knowledge_chunk_id,
        content_sha256=ev.content_sha256,
        cited_text=cited_text,
    )


def _build_needs_verification_record(
    *,
    claim: ClaimCandidate,
    ev: ClaimEvidenceRecord,
    display_order: int,
) -> ValidatedClaimRecord:
    return ValidatedClaimRecord(
        claim_key=claim.claim_key,
        claim_text=claim.claim_text,
        claim_kind="NON_CRITICAL",
        verdict="NEEDS_VERIFICATION",
        source_code=ev.source_code,
        source_version=ev.source_version,
        locator=ev.locator,
        display_order=display_order,
        knowledge_chunk_id=None,
        content_sha256=None,
        cited_text=None,
    )
