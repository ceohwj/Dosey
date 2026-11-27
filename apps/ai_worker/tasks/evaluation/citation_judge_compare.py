"""Compare citation verification judges on curated Korean medication Claim-Evidence pairs.

Compares three judges on a 60-case synthetic Korean MFDS-style benchmark:
1. `local_nli`: `MoritzLaurer/mDeBERTa-v3-base-mnli-xnli` with Korean lexical-overlap
   top-k sentence pre-filtering, corrected `(premise, hypothesis)` = `(evidence, claim)`
   ordering, and verified `id2label` / threshold handling.
2. `rule_plus_nli`: Deterministic Korean medication safety rules (numbers/units,
   meal/fasting timing conflicts, contraindication/negation flips) layered on `local_nli`.
3. `openai_llm_judge`: Structured JSON 3-class judge using OpenAI (`gpt-4o-mini` default)
   with strict Pydantic validation, exact USD cost tracking, and fail-closed error handling.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ai_worker.tasks.evaluation.citation_nli_smoke import (
    DEFAULT_DEVICE,
    DEFAULT_MAX_TOKENS,
    DEFAULT_NLI_MODEL_NAME,
    CanonicalNliLabel,
    Id2LabelMap,
    LabelMapping,
    NliPairScorer,
    NliScorerInput,
    RawScoreMap,
    estimate_token_count,
    load_huggingface_nli_scorer,
    normalize_score_map,
    split_sentences,
    summarize_latencies_ms,
)

ClaimVerdictLiteral = Literal["support", "neutral", "contradict"]
ClaimKindLiteral = Literal["critical", "non_critical"]
JudgeNameLiteral = Literal["local_nli", "rule_plus_nli", "openai_llm_judge"]

VERDICT_LABELS: tuple[ClaimVerdictLiteral, ...] = ("support", "neutral", "contradict")
DEFAULT_JUDGES: tuple[JudgeNameLiteral, ...] = ("local_nli", "rule_plus_nli", "openai_llm_judge")
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_MAX_SENTENCES = 3
ADR_0004_CRITICAL_FSR_LIMIT = 0.02
ADR_0004_MACRO_F1_LIMIT = 0.80

# USD per 1M tokens: (input_usd_per_1m, output_usd_per_1m)
MODEL_PRICING_USD_PER_1M: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
}

_TOKEN_WORD_RE = re.compile(r"[가-힣]{1,}|[0-9]+(?:\.[0-9]+)?|[a-zA-Z]+")
_HANGUL_SEQ_RE = re.compile(r"[가-힣]+")
_DIGIT_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(회|정|캡슐|포|병|일|주|개월|시간|분|세|mg|mcg|ml|mL|g|%|℃)")
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
    "옮겨 담아 보관하는 것이 좋",
    "흘려 버리면 됩니다",
    "씻어 복용하면 됩니다",
    "식탁 위에 두세요",
    "얼려 보관해야",
    "창가에 두어",
    "욕실 약장에 보관하면",
    "연속 복용해야",
)

_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "pregnancy": ("임부", "임산부", "임신"),
    "lactation": ("수유부", "수유"),
    "pediatric": ("소아", "어린이", "아이"),
    "duplicate": ("중복", "병용", "함께 복용", "다른 제품", "동일 성분"),
    "alcohol_drive": ("음주", "술", "운전", "기계 조작"),
    "fasting": ("공복", "빈속"),
    "meal_timing": ("식전", "식후", "식사 전", "식사 후", "식사 직후"),
    "expiry": ("유효기간", "사용기한"),
    "storage_temp": ("실온", "냉동", "얼려", "고온"),
    "storage_light": ("직사광선", "햇빛", "창가"),
    "storage_container": ("원래의 포장", "다른 용기", "옮겨 담"),
    "storage_humidity": ("습기", "욕실", "건조"),
    "disposal": ("폐기", "폐의약품", "하수구", "세면대", "수거함", "버리"),
    "appearance_defect": ("변색", "균열", "붕해", "색이 변"),
    "lactose": ("유당",),
}

_OPENAI_SYSTEM_PROMPT = (
    "You are a strict Korean medication citation verifier for MFDS drug label evidence. "
    "Given a Claim and an Evidence snippet, classify whether the Evidence supports, is neutral to, "
    "or contradicts the Claim.\n"
    "Rules:\n"
    "- Return `support` ONLY when the Evidence clearly entails or paraphrases every key fact in the Claim "
    "(dose, frequency, timing, population, contraindication, warning, storage, or efficacy).\n"
    "- Return `contradict` when the Evidence conflicts with the Claim on numbers/units, timing "
    "(e.g. 식전 vs 식후, 공복 금지 vs 허용), contraindications/warnings, or opposite polarity.\n"
    "- Return `neutral` when the Evidence neither entails nor contradicts the Claim, or omits critical details.\n"
    '- Respond ONLY with a valid JSON object: {"verdict": "support" | "neutral" | "contradict", "reason": "..."}.'
)


@dataclass(frozen=True, slots=True)
class CitationBenchmarkCase:
    """One labeled synthetic Korean Claim-Evidence pair for judge comparison."""

    case_id: str
    claim: str
    evidence: str
    gold_verdict: ClaimVerdictLiteral
    claim_kind: ClaimKindLiteral
    category: str
    notes: str = ""


def _build_benchmark_cases() -> tuple[CitationBenchmarkCase, ...]:
    """Build 60 balanced synthetic Korean MFDS-style Claim-Evidence cases (20/20/20)."""
    filler_a = (
        "이 약은 흰색의 원형 필름코팅정제로서 직사광선을 피하여 실온의 건조한 장소에 밀폐 보관하십시오. "
        "어린이의 손이 닿지 않는 곳에 원래의 포장 용기 그대로 보관하고 다른 용기에 옮겨 담지 마십시오."
    )
    filler_b = (
        "사용기한이 지난 의약품은 복용하지 말고 약국 또는 보건소 폐의약품 수거함을 통해 폐기하십시오. "
        "첨가제로 유당수화물, 미결정셀룰로오스, 스테아르산마그네슘 등이 함유되어 있습니다."
    )

    cases: list[CitationBenchmarkCase] = [
        # --- SUPPORT: CRITICAL (10) ---
        CitationBenchmarkCase(
            case_id="cmp-sup-crit-01",
            claim="성인은 하루 한 번 1정을 복용합니다.",
            evidence="성인 기준 1일 1회, 1회 1정을 충분한 물과 함께 복용하십시오.",
            gold_verdict="support",
            claim_kind="critical",
            category="frequency_synonym",
        ),
        CitationBenchmarkCase(
            case_id="cmp-sup-crit-02",
            claim="이 약은 식사 후 30분에 물과 함께 복용하는 것이 좋습니다.",
            evidence="성인은 1일 3회, 1회 1정을 식후 30분에 충분한 물과 함께 복용하십시오.",
            gold_verdict="support",
            claim_kind="critical",
            category="timing_paraphrase",
        ),
        CitationBenchmarkCase(
            case_id="cmp-sup-crit-03",
            claim="위장 자극을 줄이기 위해 빈속에 먹지 않는 것이 좋습니다.",
            evidence=f"{filler_a} 성인은 식사 직후 복용하며 위장 부담을 줄이기 위해 공복 복용은 피하십시오. {filler_b}",
            gold_verdict="support",
            claim_kind="critical",
            category="long_context_fasting_warning",
        ),
        CitationBenchmarkCase(
            case_id="cmp-sup-crit-04",
            claim="임신 중이거나 임신 가능성이 있는 여성은 이 약을 복용하면 안 됩니다.",
            evidence="사용상의 주의사항: 임부 또는 임신하고 있을 가능성이 있는 여성에게는 이 약을 투여하지 않는다.",
            gold_verdict="support",
            claim_kind="critical",
            category="pregnancy_contraindication",
        ),
        CitationBenchmarkCase(
            case_id="cmp-sup-crit-05",
            claim="아세트아미노펜이 들어 있는 다른 약과 중복으로 복용하지 마세요.",
            evidence="경고: 이 약은 아세트아미노펜을 함유하고 있으므로 아세트아미노펜이 포함된 다른 제품과 함께 복용하지 마십시오.",
            gold_verdict="support",
            claim_kind="critical",
            category="duplicate_ingredient_warning",
        ),
        CitationBenchmarkCase(
            case_id="cmp-sup-crit-06",
            claim="만 12세 미만 어린이는 이 약을 복용해서는 안 됩니다.",
            evidence="만 12세 미만 소아에게는 투여하지 마십시오. 12세 이상 소아 및 성인만 용법에 따라 복용합니다.",
            gold_verdict="support",
            claim_kind="critical",
            category="pediatric_contraindication",
        ),
        CitationBenchmarkCase(
            case_id="cmp-sup-crit-07",
            claim="성인은 한 번에 1정씩 먹되 하루 최대 4정을 넘기지 마세요.",
            evidence="용법용량: 성인은 1회 1정(500mg)을 복용하며 1일 최대 4정을 초과하지 마십시오.",
            gold_verdict="support",
            claim_kind="critical",
            category="max_daily_dose",
        ),
        CitationBenchmarkCase(
            case_id="cmp-sup-crit-08",
            claim="복용 간격은 최소 4시간 이상 두어야 합니다.",
            evidence="필요 시 1회 1정을 복용하되 최소 4시간 이상의 간격을 두고 1일 3회까지 복용하십시오.",
            gold_verdict="support",
            claim_kind="critical",
            category="dosing_interval",
        ),
        CitationBenchmarkCase(
            case_id="cmp-sup-crit-09",
            claim="이 약을 먹는 동안에는 술을 마시거나 운전을 피해야 합니다.",
            evidence=f"{filler_a} 졸음이 올 수 있으므로 복용 중에는 음주 및 자동차 운전이나 위험한 기계 조작을 피하십시오.",
            gold_verdict="support",
            claim_kind="critical",
            category="interaction_alcohol_driving",
        ),
        CitationBenchmarkCase(
            case_id="cmp-sup-crit-10",
            claim="수유 중인 여성은 복용 전에 의사나 약사와 먼저 상의해야 합니다.",
            evidence="수유부는 모유 중으로 약물이 이행될 수 있으므로 복용 전 반드시 의사 또는 약사와 상의하십시오.",
            gold_verdict="support",
            claim_kind="critical",
            category="lactation_caution",
        ),
        # --- SUPPORT: NON_CRITICAL (10) ---
        CitationBenchmarkCase(
            case_id="cmp-sup-noncrit-01",
            claim="햇빛이 닿지 않는 건조한 실온에 밀폐해서 보관하세요.",
            evidence="보관방법: 직사광선을 피하여 실온(1~30℃)의 건조한 장소에 밀폐용기에 넣어 보관하십시오.",
            gold_verdict="support",
            claim_kind="non_critical",
            category="storage_paraphrase",
        ),
        CitationBenchmarkCase(
            case_id="cmp-sup-noncrit-02",
            claim="어린이의 손이 닿지 않는 곳에 두어야 합니다.",
            evidence=f"{filler_b} 소아의 손이 닿지 않는 안전한 장소에 보관하십시오.",
            gold_verdict="support",
            claim_kind="non_critical",
            category="storage_child_safety",
        ),
        CitationBenchmarkCase(
            case_id="cmp-sup-noncrit-03",
            claim="유효기간이 지난 약은 먹지 말고 버려야 합니다.",
            evidence="포장 표면에 표시된 사용기한이 지난 의약품은 복용하지 말고 폐의약품 수거함을 통해 폐기하십시오.",
            gold_verdict="support",
            claim_kind="non_critical",
            category="expiry_disposal",
        ),
        CitationBenchmarkCase(
            case_id="cmp-sup-noncrit-04",
            claim="이 약은 하얀색 둥근 필름코팅 알약입니다.",
            evidence="성상: 이 약은 흰색의 원형 필름코팅정제입니다.",
            gold_verdict="support",
            claim_kind="non_critical",
            category="appearance_synonym",
        ),
        CitationBenchmarkCase(
            case_id="cmp-sup-noncrit-05",
            claim="열이 나거나 머리, 치아가 아플 때 증상을 가라앉히는 데 사용합니다.",
            evidence="효능효과: 감기로 인한 발열 및 동통, 두통, 치통, 근육통의 해열·진통에 사용합니다.",
            gold_verdict="support",
            claim_kind="non_critical",
            category="indication_paraphrase",
        ),
        CitationBenchmarkCase(
            case_id="cmp-sup-noncrit-06",
            claim="원래 포장 용기 그대로 두고 다른 통에 옮겨 담지 마세요.",
            evidence="품질 유지를 위해 원래의 포장 용기 그대로 밀폐하여 보관하고 다른 용기에 옮겨 담지 마십시오.",
            gold_verdict="support",
            claim_kind="non_critical",
            category="original_container",
        ),
        CitationBenchmarkCase(
            case_id="cmp-sup-noncrit-07",
            claim="알약 색이 변했거나 금이 갔으면 먹지 말고 전문가에게 확인받으세요.",
            evidence="외관상 변색, 균열 또는 붕해가 관찰되는 경우에는 복용하지 말고 의사나 약사에게 문의하십시오.",
            gold_verdict="support",
            claim_kind="non_critical",
            category="defect_check",
        ),
        CitationBenchmarkCase(
            case_id="cmp-sup-noncrit-08",
            claim="이 약에는 유당 성분이 첨가제로 들어 있습니다.",
            evidence="원료약품 및 분량: 첨가제로 유당수화물, 히프로멜로오스, 산화티탄 등이 포함되어 있습니다.",
            gold_verdict="support",
            claim_kind="non_critical",
            category="excipient_info",
        ),
        CitationBenchmarkCase(
            case_id="cmp-sup-noncrit-09",
            claim="습기가 많은 욕실이나 뜨거운 차 안에 오래 두면 약이 변질될 수 있습니다.",
            evidence="습기가 많은 욕실이나 고온의 차량 내부에 장기간 보관할 경우 정제의 성상이 변질될 우려가 있습니다.",
            gold_verdict="support",
            claim_kind="non_critical",
            category="storage_humidity_heat",
        ),
        CitationBenchmarkCase(
            case_id="cmp-sup-noncrit-10",
            claim="알레르기 비염으로 인한 콧물과 재채기 완화에 쓰입니다.",
            evidence=f"{filler_a} 효능효과: 알레르기성 비염에 의한 재채기, 콧물, 코막힘 증상의 완화에 사용합니다.",
            gold_verdict="support",
            claim_kind="non_critical",
            category="indication_allergy",
        ),
        # --- NEUTRAL: CRITICAL (10) ---
        CitationBenchmarkCase(
            case_id="cmp-neu-crit-01",
            claim="이 약은 하루 2회 식후에 복용해야 합니다.",
            evidence="보관방법: 직사광선을 피하고 습기가 적은 실온에 밀폐하여 보관하십시오.",
            gold_verdict="neutral",
            claim_kind="critical",
            category="ungrounded_dosing",
        ),
        CitationBenchmarkCase(
            case_id="cmp-neu-crit-02",
            claim="만 6세 어린이는 성인 용량의 절반인 0.5정을 복용합니다.",
            evidence="효능효과: 두통, 치통, 생리통 및 발열 시 진통·해열에 사용합니다.",
            gold_verdict="neutral",
            claim_kind="critical",
            category="ungrounded_pediatric_dose",
        ),
        CitationBenchmarkCase(
            case_id="cmp-neu-crit-03",
            claim="항생제와 함께 복용하면 치료 효과가 더 빨리 나타납니다.",
            evidence="이 약은 흰색의 장방형 정제로서 실온에서 보관하며 유효기간은 제조일로부터 36개월입니다.",
            gold_verdict="neutral",
            claim_kind="critical",
            category="ungrounded_interaction",
        ),
        CitationBenchmarkCase(
            case_id="cmp-neu-crit-04",
            claim="복용 중에는 카페인 음료 섭취를 제한하는 편이 안전합니다.",
            evidence="직사광선을 피하고 습기가 적은 서늘한 곳에 원래 포장 용기 그대로 밀폐 보관하십시오.",
            gold_verdict="neutral",
            claim_kind="critical",
            category="ungrounded_caffeine_warning",
        ),
        CitationBenchmarkCase(
            case_id="cmp-neu-crit-05",
            claim="임산부도 1일 1정 범위 내에서는 별도 확인 없이 복용할 수 있습니다.",
            evidence="성상: 연한 노란색의 타원형 필름코팅정제이며 PTP 포장으로 공급됩니다.",
            gold_verdict="neutral",
            claim_kind="critical",
            category="ungrounded_pregnancy_safety",
        ),
        CitationBenchmarkCase(
            case_id="cmp-neu-crit-06",
            claim="혈압약과 함께 복용해도 약물 상호작용이 발생하지 않습니다.",
            evidence="복용 후 졸음이 나타날 수 있으므로 운전이나 기계 조작 시 주의하십시오.",
            gold_verdict="neutral",
            claim_kind="critical",
            category="ungrounded_drug_interaction",
        ),
        CitationBenchmarkCase(
            case_id="cmp-neu-crit-07",
            claim="증상이 가벼우면 1회 250mg으로 줄여 1일 2회 복용할 수 있습니다.",
            evidence="성분명: 이부프로펜 정제. 해열 및 진통 목적으로 사용하며 어린이의 손이 닿지 않는 곳에 두십시오.",
            gold_verdict="neutral",
            claim_kind="critical",
            category="ungrounded_numeric_dose",
        ),
        CitationBenchmarkCase(
            case_id="cmp-neu-crit-08",
            claim="신장 기능이 정상이라면 8시간 간격으로 복용해도 됩니다.",
            evidence="간 질환이 있는 환자는 복용 전 의사 또는 약사와 상의하시기 바랍니다.",
            gold_verdict="neutral",
            claim_kind="critical",
            category="ungrounded_renal_interval",
        ),
        CitationBenchmarkCase(
            case_id="cmp-neu-crit-09",
            claim="아침 기상 직후 공복에 복용하면 위장장애가 예방됩니다.",
            evidence="드물게 속쓰림, 구역, 구토 등의 위장관계 이상반응이 나타날 수 있습니다.",
            gold_verdict="neutral",
            claim_kind="critical",
            category="ungrounded_timing",
        ),
        CitationBenchmarkCase(
            case_id="cmp-neu-crit-10",
            claim="이 약은 최소 7일 이상 연속 복용해야 증상이 재발하지 않습니다.",
            evidence="습기가 많은 욕실에 보관하지 말고 건조한 실온에 밀폐 보관하십시오.",
            gold_verdict="neutral",
            claim_kind="critical",
            category="ungrounded_duration",
        ),
        # --- NEUTRAL: NON_CRITICAL (10) ---
        CitationBenchmarkCase(
            case_id="cmp-neu-noncrit-01",
            claim="이 약의 코팅에는 식물성 셀룰로오스만 사용되었습니다.",
            evidence="보관방법: 직사광선을 피하여 1~30℃의 실온에 보관하십시오.",
            gold_verdict="neutral",
            claim_kind="non_critical",
            category="ungrounded_excipient",
        ),
        CitationBenchmarkCase(
            case_id="cmp-neu-noncrit-02",
            claim="개봉 후에는 냉장고 문 쪽 선반에 보관하는 것이 권장됩니다.",
            evidence="효능효과: 급성 기관지염으로 인한 기침과 가래 증상의 완화에 사용합니다.",
            gold_verdict="neutral",
            claim_kind="non_critical",
            category="ungrounded_storage",
        ),
        CitationBenchmarkCase(
            case_id="cmp-neu-noncrit-03",
            claim="정제 표면에 딸기 향 코팅이 되어 있어 삼키기 쉽습니다.",
            evidence="성상: 이 약은 분홍색의 원형 필름코팅정제입니다.",
            gold_verdict="neutral",
            claim_kind="non_critical",
            category="ungrounded_flavor",
        ),
        CitationBenchmarkCase(
            case_id="cmp-neu-noncrit-04",
            claim="국내 제조소에서 생산된 건강보험 급여 적용 품목입니다.",
            evidence="효능효과: 소화불량, 식욕감퇴, 과식, 체함에 의한 소화 촉진에 사용합니다.",
            gold_verdict="neutral",
            claim_kind="non_critical",
            category="ungrounded_reimbursement",
        ),
        CitationBenchmarkCase(
            case_id="cmp-neu-noncrit-05",
            claim="이 제품은 글루텐 프리 인증을 받은 원료만 사용했습니다.",
            evidence="첨가제로 유당수화물, 미결정셀룰로오스, 스테아르산마그네슘이 들어 있습니다.",
            gold_verdict="neutral",
            claim_kind="non_critical",
            category="ungrounded_certification",
        ),
        CitationBenchmarkCase(
            case_id="cmp-neu-noncrit-06",
            claim="종이 외포장 상자는 일반 종이류로 분리배출하면 됩니다.",
            evidence="유효기간이 지난 남은 약은 약국이나 보건소 폐의약품 수거함에 배출하십시오.",
            gold_verdict="neutral",
            claim_kind="non_critical",
            category="ungrounded_packaging",
        ),
        CitationBenchmarkCase(
            case_id="cmp-neu-noncrit-07",
            claim="병 내부에는 습기 제거용 실리카겔이 함께 동봉되어 있습니다.",
            evidence="어린이의 손이 닿지 않는 곳에 원래의 포장 용기 그대로 밀폐 보관하십시오.",
            gold_verdict="neutral",
            claim_kind="non_critical",
            category="ungrounded_desiccant",
        ),
        CitationBenchmarkCase(
            case_id="cmp-neu-noncrit-08",
            claim="정제 가운데에 분할선이 새겨져 있어 반으로 나누기 쉽습니다.",
            evidence="이 약은 흰색의 원형 정제로서 직사광선을 피해 보관하십시오.",
            gold_verdict="neutral",
            claim_kind="non_critical",
            category="ungrounded_score_line",
        ),
        CitationBenchmarkCase(
            case_id="cmp-neu-noncrit-09",
            claim="복용 후 30분 이내에 효과가 최고조에 달합니다.",
            evidence="성인은 필요 시 1회 1정을 충분한 물과 함께 복용하십시오.",
            gold_verdict="neutral",
            claim_kind="non_critical",
            category="ungrounded_pk",
        ),
        CitationBenchmarkCase(
            case_id="cmp-neu-noncrit-10",
            claim="여행 시 기내 반입이 가능하도록 낱개 블리스터로 포장되어 있습니다.",
            evidence="고온의 차량 내부나 습기가 많은 곳에 장기간 보관하지 마십시오.",
            gold_verdict="neutral",
            claim_kind="non_critical",
            category="ungrounded_travel_pack",
        ),
        # --- CONTRADICT: CRITICAL (10) ---
        CitationBenchmarkCase(
            case_id="cmp-con-crit-01",
            claim="성인은 하루에 2회, 1일 2회 1정씩 복용합니다.",
            evidence="성인은 1일 1회 1정을 매일 같은 시간에 복용하십시오.",
            gold_verdict="contradict",
            claim_kind="critical",
            category="frequency_conflict",
        ),
        CitationBenchmarkCase(
            case_id="cmp-con-crit-02",
            claim="증상이 심할 때는 1회 2정(1000mg)씩 하루 최대 6정까지 늘려 복용해도 됩니다.",
            evidence="성인은 1회 1정(500mg)을 복용하며 1일 최대 3정을 초과하지 마십시오.",
            gold_verdict="contradict",
            claim_kind="critical",
            category="dose_numeric_conflict",
        ),
        CitationBenchmarkCase(
            case_id="cmp-con-crit-03",
            claim="반드시 식전에 복용해야 약효가 제대로 나타납니다.",
            evidence="위장 부담을 줄이기 위해 이 약은 식후 30분에 물과 함께 복용하십시오.",
            gold_verdict="contradict",
            claim_kind="critical",
            category="meal_timing_conflict",
        ),
        CitationBenchmarkCase(
            case_id="cmp-con-crit-04",
            claim="빈속에 공복으로 복용해도 위장에 부담이 없어 괜찮습니다.",
            evidence="위장관 자극이 발생할 수 있으므로 공복 시 복용을 피하십시오.",
            gold_verdict="contradict",
            claim_kind="critical",
            category="fasting_prohibition_flip",
        ),
        CitationBenchmarkCase(
            case_id="cmp-con-crit-05",
            claim="임산부도 의사나 약사 상담 없이 안전하게 복용할 수 있습니다.",
            evidence="임부 또는 임신하고 있을 가능성이 있는 여성은 이 약을 투여하지 않는다. 반드시 의사 또는 약사와 상의하십시오.",
            gold_verdict="contradict",
            claim_kind="critical",
            category="pregnancy_contraindication_flip",
        ),
        CitationBenchmarkCase(
            case_id="cmp-con-crit-06",
            claim="다른 아세트아미노펜 성분 감기약과 동시에 중복 복용해도 무방합니다.",
            evidence="아세트아미노펜을 포함하는 다른 제품과 함께 중복 복용하지 마십시오.",
            gold_verdict="contradict",
            claim_kind="critical",
            category="duplicate_prohibition_flip",
        ),
        CitationBenchmarkCase(
            case_id="cmp-con-crit-07",
            claim="만 8세 어린이에게도 성인과 동일하게 1정을 복용시킬 수 있습니다.",
            evidence="만 12세 미만 소아에게는 투여하지 마십시오.",
            gold_verdict="contradict",
            claim_kind="critical",
            category="age_contraindication_conflict",
        ),
        CitationBenchmarkCase(
            case_id="cmp-con-crit-08",
            claim="통증이 계속되면 2시간 간격으로 연이어 복용해도 됩니다.",
            evidence="복용 간격은 최소 4시간 이상으로 유지하며 1일 3회를 초과하지 마십시오.",
            gold_verdict="contradict",
            claim_kind="critical",
            category="interval_numeric_conflict",
        ),
        CitationBenchmarkCase(
            case_id="cmp-con-crit-09",
            claim="이 약을 복용하는 동안에도 가벼운 음주나 자동차 운전은 지장이 없습니다.",
            evidence="졸음이 올 수 있으므로 복용 중에는 음주 및 자동차 운전이나 위험한 기계 조작을 피하십시오.",
            gold_verdict="contradict",
            claim_kind="critical",
            category="alcohol_driving_flip",
        ),
        CitationBenchmarkCase(
            case_id="cmp-con-crit-10",
            claim="별도 진료 없이 최소 14일간 꾸준히 복용해야 합니다.",
            evidence="의사 또는 약사의 지시 없이 5일 이상 연속하여 복용하지 마십시오.",
            gold_verdict="contradict",
            claim_kind="critical",
            category="duration_numeric_conflict",
        ),
        # --- CONTRADICT: NON_CRITICAL (10) ---
        CitationBenchmarkCase(
            case_id="cmp-con-noncrit-01",
            claim="이 약은 반드시 냉동실에 얼려 보관해야 변질되지 않습니다.",
            evidence="보관방법: 얼리지 말고 직사광선을 피하여 실온(1~30℃)에서 보관하십시오.",
            gold_verdict="contradict",
            claim_kind="non_critical",
            category="storage_temp_conflict",
        ),
        CitationBenchmarkCase(
            case_id="cmp-con-noncrit-02",
            claim="햇빛이 잘 드는 창가에 두어 건조하게 보관하세요.",
            evidence="직사광선을 피하여 서늘하고 건조한 곳에 보관하십시오.",
            gold_verdict="contradict",
            claim_kind="non_critical",
            category="storage_light_conflict",
        ),
        CitationBenchmarkCase(
            case_id="cmp-con-noncrit-03",
            claim="휴대용 작은 통에 낱알을 꺼내 옮겨 담아 보관하는 것이 좋습니다.",
            evidence="원래의 포장 용기 그대로 밀폐하여 보관하고 다른 용기에 옮겨 담지 마십시오.",
            gold_verdict="contradict",
            claim_kind="non_critical",
            category="container_transfer_flip",
        ),
        CitationBenchmarkCase(
            case_id="cmp-con-noncrit-04",
            claim="유효기간이 몇 달 지나더라도 겉보기에 멀쩡하면 복용해도 무방합니다.",
            evidence="포장에 표시된 유효기간이 지난 제품은 절대 복용하지 말고 폐기하십시오.",
            gold_verdict="contradict",
            claim_kind="non_critical",
            category="expiry_flip",
        ),
        CitationBenchmarkCase(
            case_id="cmp-con-noncrit-05",
            claim="이 약은 갈색의 액상 연질캡슐 형태입니다.",
            evidence="성상: 이 약은 흰색의 원형 필름코팅정제입니다.",
            gold_verdict="contradict",
            claim_kind="non_critical",
            category="formulation_conflict",
        ),
        CitationBenchmarkCase(
            case_id="cmp-con-noncrit-06",
            claim="습기가 있는 욕실 약장에 보관하면 온도 변화가 적어 가장 좋습니다.",
            evidence="습기가 많은 욕실이나 고온의 차량 내부에 보관하지 마십시오.",
            gold_verdict="contradict",
            claim_kind="non_critical",
            category="bathroom_storage_flip",
        ),
        CitationBenchmarkCase(
            case_id="cmp-con-noncrit-07",
            claim="남은 약은 세면대 하수구에 흘려 버리면 됩니다.",
            evidence="남은 약은 일반 쓰레기나 하수구에 버리지 말고 폐의약품 수거함에 배출하십시오.",
            gold_verdict="contradict",
            claim_kind="non_critical",
            category="disposal_flip",
        ),
        CitationBenchmarkCase(
            case_id="cmp-con-noncrit-08",
            claim="알약 색이 변했더라도 물로 씻어 복용하면 됩니다.",
            evidence="변색이나 균열이 관찰되는 경우 복용하지 말고 전문가에게 문의하십시오.",
            gold_verdict="contradict",
            claim_kind="non_critical",
            category="defect_flip",
        ),
        CitationBenchmarkCase(
            case_id="cmp-con-noncrit-09",
            claim="아이가 쉽게 꺼낼 수 있도록 거실 식탁 위에 두세요.",
            evidence="어린이의 손이 닿지 않는 곳에 보관하십시오.",
            gold_verdict="contradict",
            claim_kind="non_critical",
            category="child_reach_flip",
        ),
        CitationBenchmarkCase(
            case_id="cmp-con-noncrit-10",
            claim="이 약에는 유당 성분이 전혀 포함되어 있지 않습니다.",
            evidence="이 약은 첨가제로 유당수화물을 함유하고 있습니다.",
            gold_verdict="contradict",
            claim_kind="non_critical",
            category="excipient_negation_flip",
        ),
    ]
    return tuple(cases)


BENCHMARK_CASES: tuple[CitationBenchmarkCase, ...] = _build_benchmark_cases()


@dataclass(frozen=True, slots=True)
class JudgePrediction:
    """Single Claim-Evidence evaluation output from a citation judge."""

    verdict: ClaimVerdictLiteral
    latency_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    selected_evidence: str = ""
    reason: str = ""
    raw_scores: dict[str, float] | None = None


@dataclass(frozen=True, slots=True)
class CaseJudgeRecord:
    """Per-case evaluation record tying benchmark ground truth to a judge prediction."""

    case_id: str
    claim_kind: ClaimKindLiteral
    gold_verdict: ClaimVerdictLiteral
    predicted_verdict: ClaimVerdictLiteral
    latency_ms: float
    input_tokens: int
    output_tokens: int
    cost_usd: float
    error: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class JudgeMetricsReport:
    """Comparison metrics for one judge across the benchmark dataset."""

    judge_name: str
    total_cases: int
    correct_count: int
    accuracy: float
    macro_f1: float
    support_recall: float
    neutral_recall: float
    contradict_recall: float
    critical_case_count: int
    critical_contradict_count: int
    critical_contradict_recall: float
    critical_non_support_count: int
    critical_false_support_count: int
    critical_false_support_rate: float
    passes_adr_0004_critical_fsr: bool
    passes_adr_0004_macro_f1: bool
    latency_ms_p50: float
    latency_ms_p95: float
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    cost_per_100_claims_usd: float
    error_count: int
    confusion_matrix: dict[str, dict[str, int]]


@dataclass(frozen=True, slots=True)
class CitationJudgeCompareReport:
    """Full comparison report across selected judges."""

    benchmark_size: int
    judges: tuple[JudgeMetricsReport, ...]
    case_records: dict[str, tuple[CaseJudgeRecord, ...]]
    offline_smoke: bool
    openai_model: str
    nli_model: str
    git_commit: str
    command: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class CitationJudge(Protocol):
    """Unified async/sync citation judge interface."""

    judge_name: str

    async def evaluate(
        self,
        *,
        claim: str,
        evidence: str,
        claim_kind: ClaimKindLiteral = "non_critical",
    ) -> JudgePrediction:
        """Evaluate a `(claim, evidence)` pair and return a `JudgePrediction`."""


def _normalize_korean_text(text: str) -> str:
    normalized = text.strip()
    for pattern, replacement in _NATIVE_COUNT_REPLACEMENTS:
        normalized = pattern.sub(replacement, normalized)
    return normalized


def _lexical_features(text: str) -> set[str]:
    """Extract normalized word, numeric+unit, and Korean bigram features."""
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


def _truncate_text_to_token_budget(text: str, *, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    if estimate_token_count(text) <= max_tokens:
        return text
    lo, hi = 1, len(text)
    best = text[:1]
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = text[:mid].strip()
        if estimate_token_count(candidate) <= max_tokens:
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
    evidence_budget: int,
) -> tuple[str, ...]:
    selected: list[str] = []
    used_tokens = 0
    for idx in top_indices:
        sentence = sentences[idx]
        sent_tokens = estimate_token_count(sentence)
        if used_tokens + sent_tokens <= evidence_budget:
            selected.append(sentence)
            used_tokens += sent_tokens
        elif not selected:
            trimmed = _truncate_text_to_token_budget(sentence, max_tokens=evidence_budget)
            if trimmed:
                selected.append(trimmed)
            break

    if not selected and top_indices:
        trimmed = _truncate_text_to_token_budget(sentences[top_indices[0]], max_tokens=evidence_budget)
        if trimmed:
            selected.append(trimmed)
    return tuple(selected)


def select_top_k_evidence_sentences(
    *,
    claim: str,
    evidence: str,
    max_sentences: int = DEFAULT_MAX_SENTENCES,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> tuple[str, ...]:
    """Pre-filter evidence to top-k Korean lexical-overlap sentences within `max_tokens`.

    Preserves original sentence order so premise reading coherence stays intact.
    """
    if max_sentences <= 0:
        raise ValueError("max_sentences must be a positive integer")
    if max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")

    sentences = split_sentences(evidence)
    if not sentences:
        return ()

    claim_tokens = estimate_token_count(claim)
    # Reserve margin for claim tokens and NLI special tokens ([CLS], [SEP], etc.).
    evidence_budget = max(16, max_tokens - claim_tokens - 8)

    if len(sentences) <= max_sentences and estimate_token_count(" ".join(sentences)) <= evidence_budget:
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
    return _pack_sentences_within_budget(sentences, top_indices, evidence_budget=evidence_budget)


def _select_nli_verdict(
    probs: Mapping[CanonicalNliLabel, float],
    *,
    support_threshold: float,
    contradict_threshold: float,
) -> ClaimVerdictLiteral:
    support_p = probs[CanonicalNliLabel.SUPPORT]
    contradict_p = probs[CanonicalNliLabel.CONTRADICT]
    neutral_p = probs[CanonicalNliLabel.NEUTRAL]

    if contradict_p >= contradict_threshold and contradict_p >= support_p and contradict_p >= neutral_p:
        return "contradict"
    if support_p >= support_threshold and support_p > contradict_p and support_p >= neutral_p:
        return "support"
    if contradict_p > support_p and contradict_p > neutral_p:
        return "contradict"
    return "neutral"


class LocalNliCitationJudge:
    """Local HuggingFace NLI judge (`mDeBERTa-v3-base-mnli-xnli`) with top-k pre-filtering."""

    judge_name = "local_nli"

    def __init__(
        self,
        *,
        scorer: NliScorerInput | None = None,
        model_name: str = DEFAULT_NLI_MODEL_NAME,
        device: str = DEFAULT_DEVICE,
        max_sentences: int = DEFAULT_MAX_SENTENCES,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        support_threshold: float = 0.50,
        contradict_threshold: float = 0.45,
        id2label: Id2LabelMap | None = None,
        label_mapping: LabelMapping | None = None,
    ) -> None:
        if max_sentences <= 0:
            raise ValueError("max_sentences must be positive")
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")

        self.model_name = model_name
        self.device = device
        self.max_sentences = max_sentences
        self.max_tokens = max_tokens
        self.support_threshold = support_threshold
        self.contradict_threshold = contradict_threshold
        self.id2label = id2label
        self.label_mapping = label_mapping
        self._scorer: NliScorerInput | None = scorer

    def _ensure_scorer(self) -> NliPairScorer:
        if self._scorer is None:
            self._scorer = load_huggingface_nli_scorer(model_name=self.model_name, device=self.device)
        if isinstance(self._scorer, NliPairScorer):
            return self._scorer
        callable_fn = self._scorer

        class _FnAdapter:
            def __init__(self, fn: Callable[[str, str], RawScoreMap]) -> None:
                self._fn = fn
                self.id2label: Id2LabelMap | None = getattr(fn, "id2label", None)

            def score_pairs(self, pairs: Sequence[tuple[str, str]]) -> Sequence[RawScoreMap]:
                return [self._fn(premise, hypothesis) for premise, hypothesis in pairs]

        return _FnAdapter(callable_fn)

    def evaluate_sync(
        self,
        *,
        claim: str,
        evidence: str,
        claim_kind: ClaimKindLiteral = "non_critical",
    ) -> JudgePrediction:
        del claim_kind
        selected_sentences = select_top_k_evidence_sentences(
            claim=claim,
            evidence=evidence,
            max_sentences=self.max_sentences,
            max_tokens=self.max_tokens,
        )
        if not selected_sentences:
            return JudgePrediction(
                verdict="neutral",
                latency_ms=0.0,
                error="empty_evidence",
                reason="Evidence text is empty.",
            )

        premise = " ".join(selected_sentences)
        pair_scorer = self._ensure_scorer()
        effective_id2label = self.id2label if self.id2label is not None else getattr(pair_scorer, "id2label", None)
        if effective_id2label is None and self._scorer is not None:
            effective_id2label = {0: "entailment", 1: "neutral", 2: "contradiction"}

        started = time.perf_counter()
        # IMPORTANT: CrossEncoder / mDeBERTa NLI expects `(premise, hypothesis)` = `(evidence, claim)`.
        # Reversing to `(claim, evidence)` causes Korean support pairs to collapse into `neutral`.
        raw_rows = pair_scorer.score_pairs([(premise, claim)])
        latency_ms = round((time.perf_counter() - started) * 1000.0, 3)

        if len(raw_rows) != 1:
            raise ValueError(f"Expected 1 NLI score row, got {len(raw_rows)}")

        raw_map = raw_rows[0]
        raw_values = [float(v) for v in raw_map.values()]
        force_softmax = abs(sum(raw_values) - 1.0) > 1e-2 or any(v < 0.0 or v > 1.0 for v in raw_values)
        probs = normalize_score_map(
            raw_map,
            id2label=effective_id2label,
            label_mapping=self.label_mapping,
            apply_softmax=force_softmax,
        )
        verdict = _select_nli_verdict(
            probs,
            support_threshold=self.support_threshold,
            contradict_threshold=self.contradict_threshold,
        )
        return JudgePrediction(
            verdict=verdict,
            latency_ms=latency_ms,
            selected_evidence=premise,
            reason=f"nli_probs(support={probs[CanonicalNliLabel.SUPPORT]:.3f},"
            f"neutral={probs[CanonicalNliLabel.NEUTRAL]:.3f},"
            f"contradict={probs[CanonicalNliLabel.CONTRADICT]:.3f})",
            raw_scores={label.value: round(probs[label], 6) for label in CanonicalNliLabel},
        )

    async def evaluate(
        self,
        *,
        claim: str,
        evidence: str,
        claim_kind: ClaimKindLiteral = "non_critical",
    ) -> JudgePrediction:
        return self.evaluate_sync(claim=claim, evidence=evidence, claim_kind=claim_kind)


@dataclass(frozen=True, slots=True)
class _ExtractedFacts:
    daily_freq: frozenset[float]
    per_dose_tab: frozenset[float]
    daily_max_tab: frozenset[float]
    unit_values: dict[str, frozenset[float]]
    has_before_meal: bool
    has_after_meal: bool
    has_fasting_allow: bool
    has_fasting_forbid: bool
    has_prohibition: bool
    has_permissive: bool
    has_consultation_required: bool
    has_presence_affirmative: bool
    topics: frozenset[str]


def _has_any_marker(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _extract_facts(text: str) -> _ExtractedFacts:
    normalized = _normalize_korean_text(text)
    daily_freq = frozenset(float(m) for m in _DAILY_FREQ_RE.findall(normalized))
    per_dose_tab = frozenset(float(m) for m in _PER_DOSE_TAB_RE.findall(normalized))

    daily_max_vals: set[float] = set()
    for g1, g2 in _DAILY_MAX_TAB_RE.findall(normalized):
        val = g1 or g2
        if val:
            daily_max_vals.add(float(val))
    daily_max_tab = frozenset(daily_max_vals)

    unit_buckets: dict[str, set[float]] = {}
    for num_str, unit in _DIGIT_UNIT_RE.findall(normalized):
        if unit in {"회", "정", "캡슐", "분", "℃"}:
            # `회`/`정` are tracked in finer daily/per-dose buckets; `분` (식후 30분) and `℃` handled carefully.
            if unit == "℃":
                unit_buckets.setdefault("temp_c", set()).add(float(num_str))
            continue
        unit_buckets.setdefault(unit.lower(), set()).add(float(num_str))

    has_before_meal = "식전" in normalized or "식사 전" in normalized
    has_after_meal = "식후" in normalized or "식사 후" in normalized or "식사 직후" in normalized

    has_fasting_word = "공복" in normalized or "빈속" in normalized
    fasting_negated = has_fasting_word and (
        "공복 복용은 피하" in normalized
        or "공복 시 복용을 피하" in normalized
        or "공복에 복용하지" in normalized
        or "빈속에 먹지 않" in normalized
        or "빈속에 복용하지" in normalized
        or "피하십시오" in normalized
        or "피한다" in normalized
    )
    has_fasting_allow = has_fasting_word and not fasting_negated
    has_fasting_forbid = has_fasting_word and fasting_negated

    topics = frozenset(
        topic for topic, keywords in _TOPIC_KEYWORDS.items() if any(keyword in normalized for keyword in keywords)
    )
    has_prohibition = _has_any_marker(normalized, _PROHIBITION_MARKERS)
    has_consultation = _has_any_marker(normalized, _CONSULT_MARKERS) and "상담 없이" not in normalized
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
        has_before_meal=has_before_meal,
        has_after_meal=has_after_meal,
        has_fasting_allow=has_fasting_allow,
        has_fasting_forbid=has_fasting_forbid,
        has_prohibition=has_prohibition,
        has_permissive=_has_any_marker(normalized, _PERMISSIVE_MARKERS),
        has_consultation_required=has_consultation,
        has_presence_affirmative=has_presence_affirmative,
        topics=topics,
    )


def _sets_conflict(a: frozenset[float], b: frozenset[float]) -> bool:
    return bool(a) and bool(b) and a.isdisjoint(b)


def _check_timing_conflict(claim_facts: _ExtractedFacts, ev_facts: _ExtractedFacts) -> str | None:
    if (claim_facts.has_before_meal and ev_facts.has_after_meal) or (
        claim_facts.has_after_meal and ev_facts.has_before_meal
    ):
        return "rule:meal_timing_conflict"
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
        and not (ev_facts.has_prohibition)
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


def _apply_medication_safety_rules(
    *,
    claim: str,
    evidence: str,
    claim_kind: ClaimKindLiteral,
    nli_prediction: JudgePrediction,
) -> tuple[ClaimVerdictLiteral, str]:
    """Apply deterministic Korean medication safety rules on top of `local_nli`."""
    claim_facts = _extract_facts(claim)
    ev_facts = _extract_facts(evidence)

    for conflict_reason in (
        _check_timing_conflict(claim_facts, ev_facts),
        _check_numeric_conflict(claim_facts, ev_facts),
        _check_polarity_conflict(claim_facts, ev_facts),
    ):
        if conflict_reason is not None:
            return "contradict", conflict_reason

    if nli_prediction.verdict == "support" and claim_kind == "critical":
        downgrade_reason = _check_unverified_critical_support(claim_facts, ev_facts)
        if downgrade_reason is not None:
            return "neutral", downgrade_reason

    return nli_prediction.verdict, "rule:nli_passthrough"


class RulePlusNliCitationJudge:
    """Deterministic Korean medication safety guard layered on top of `LocalNliCitationJudge`."""

    judge_name = "rule_plus_nli"

    def __init__(self, nli_judge: LocalNliCitationJudge | None = None, **nli_kwargs: Any) -> None:
        self._nli_judge = nli_judge if nli_judge is not None else LocalNliCitationJudge(**nli_kwargs)

    def evaluate_sync(
        self,
        *,
        claim: str,
        evidence: str,
        claim_kind: ClaimKindLiteral = "non_critical",
    ) -> JudgePrediction:
        nli_pred = self._nli_judge.evaluate_sync(claim=claim, evidence=evidence, claim_kind=claim_kind)
        rule_start = time.perf_counter()
        target_evidence = nli_pred.selected_evidence or evidence
        final_verdict, rule_reason = _apply_medication_safety_rules(
            claim=claim,
            evidence=target_evidence,
            claim_kind=claim_kind,
            nli_prediction=nli_pred,
        )
        rule_ms = (time.perf_counter() - rule_start) * 1000.0
        return JudgePrediction(
            verdict=final_verdict,
            latency_ms=round(nli_pred.latency_ms + rule_ms, 3),
            selected_evidence=target_evidence,
            reason=f"{rule_reason} | {nli_pred.reason}",
            raw_scores=nli_pred.raw_scores,
            error=nli_pred.error,
        )

    async def evaluate(
        self,
        *,
        claim: str,
        evidence: str,
        claim_kind: ClaimKindLiteral = "non_critical",
    ) -> JudgePrediction:
        return self.evaluate_sync(claim=claim, evidence=evidence, claim_kind=claim_kind)


class LlmJudgeResponse(BaseModel):
    """Strict Pydantic schema for OpenAI 3-class citation judge JSON output."""

    model_config = ConfigDict(extra="forbid")

    verdict: ClaimVerdictLiteral
    reason: str = Field(min_length=1)


def compute_openai_cost_usd(model: str, *, input_tokens: int, output_tokens: int) -> float:
    """Calculate exact USD cost from token counts using configured model pricing."""
    pricing = MODEL_PRICING_USD_PER_1M.get(model)
    if pricing is None:
        for prefix, candidate in MODEL_PRICING_USD_PER_1M.items():
            if model.startswith(prefix):
                pricing = candidate
                break
    if pricing is None:
        pricing = MODEL_PRICING_USD_PER_1M[DEFAULT_OPENAI_MODEL]
    in_rate, out_rate = pricing
    return (max(0, input_tokens) * in_rate + max(0, output_tokens) * out_rate) / 1_000_000.0


def _extract_token_usage(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, Mapping):
        usage = response.get("usage")
    if usage is None:
        return 0, 0
    if isinstance(usage, Mapping):
        in_tok = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        return in_tok, out_tok
    in_tok = int(getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", None) or 0)
    out_tok = int(getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", None) or 0)
    return in_tok, out_tok


def _extract_response_content(response: Any) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, Mapping):
        if "content" in response and isinstance(response["content"], str):
            return response["content"]
        choices = response.get("choices")
        if isinstance(choices, Sequence) and choices:
            first = choices[0]
            if isinstance(first, Mapping):
                message = first.get("message", {})
                if isinstance(message, Mapping) and isinstance(message.get("content"), str):
                    return str(message["content"])
    choices_attr = getattr(response, "choices", None)
    if choices_attr:
        message_attr = getattr(choices_attr[0], "message", None)
        content_attr = getattr(message_attr, "content", None)
        if isinstance(content_attr, str):
            return content_attr
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str):
        return output_text
    raise ValueError("LLM response did not contain textual content")


class OpenAILlmCitationJudge:
    """Structured JSON 3-class citation judge backed by OpenAI (`gpt-4o-mini` default)."""

    judge_name = "openai_llm_judge"

    def __init__(
        self,
        *,
        model: str = DEFAULT_OPENAI_MODEL,
        client: Any | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.model = model
        self._client = client
        self.timeout_seconds = timeout_seconds

    def _get_or_create_client(self) -> Any:
        if self._client is not None:
            return self._client
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY environment variable is required for live `openai_llm_judge` execution.")
        try:
            openai_module = importlib.import_module("openai")
            async_openai_cls = openai_module.AsyncOpenAI
        except (ImportError, AttributeError) as exc:
            raise RuntimeError("Missing `openai` package for live `openai_llm_judge` execution.") from exc
        self._client = async_openai_cls(api_key=api_key, timeout=self.timeout_seconds, max_retries=0)
        return self._client

    async def evaluate(
        self,
        *,
        claim: str,
        evidence: str,
        claim_kind: ClaimKindLiteral = "non_critical",
    ) -> JudgePrediction:
        client = self._get_or_create_client()
        user_prompt = (
            f"ClaimKind: {claim_kind}\n"
            f"Claim: {claim}\n"
            f"Evidence: {evidence}\n"
            'Return JSON: {"verdict": "support" | "neutral" | "contradict", "reason": "..."}'
        )
        messages = [
            {"role": "system", "content": _OPENAI_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        started = time.perf_counter()
        input_tokens = 0
        output_tokens = 0
        try:
            if callable(client):
                maybe_res = client(model=self.model, messages=messages)
            else:
                maybe_res = client.chat.completions.create(
                    model=self.model,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                    messages=messages,
                )
            response = await maybe_res if inspect.isawaitable(maybe_res) else maybe_res
            latency_ms = round((time.perf_counter() - started) * 1000.0, 3)

            input_tokens, output_tokens = _extract_token_usage(response)
            cost_usd = compute_openai_cost_usd(
                self.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            raw_content = _extract_response_content(response)
            parsed_json = json.loads(raw_content)
            validated = LlmJudgeResponse.model_validate(parsed_json)
            return JudgePrediction(
                verdict=validated.verdict,
                latency_ms=latency_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                selected_evidence=evidence,
                reason=validated.reason,
            )
        except (ValueError, TypeError, ValidationError, json.JSONDecodeError, RuntimeError, OSError) as exc:
            latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
            cost_usd = compute_openai_cost_usd(
                self.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            # Fail-closed: never emit `support` when structured verification fails.
            return JudgePrediction(
                verdict="neutral",
                latency_ms=latency_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                error=f"{type(exc).__name__}: {exc}",
                selected_evidence=evidence,
                reason="fail_closed_invalid_llm_response",
            )
        except Exception as exc:
            latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
            cost_usd = compute_openai_cost_usd(
                self.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            return JudgePrediction(
                verdict="neutral",
                latency_ms=latency_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                error=f"{type(exc).__name__}: {exc}",
                selected_evidence=evidence,
                reason="fail_closed_llm_runtime_error",
            )


def compute_judge_metrics(
    *,
    judge_name: str,
    records: Sequence[CaseJudgeRecord],
) -> JudgeMetricsReport:
    """Compute Macro-F1, recalls, Critical FSR, latency P50/P95, and cost metrics."""
    total_cases = len(records)
    confusion: dict[str, dict[str, int]] = {gold: {pred: 0 for pred in VERDICT_LABELS} for gold in VERDICT_LABELS}
    for rec in records:
        confusion[rec.gold_verdict][rec.predicted_verdict] += 1

    f1_scores: list[float] = []
    recalls: dict[ClaimVerdictLiteral, float] = {}
    for label in VERDICT_LABELS:
        tp = confusion[label][label]
        fp = sum(confusion[other][label] for other in VERDICT_LABELS if other != label)
        fn = sum(confusion[label][other] for other in VERDICT_LABELS if other != label)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
        recalls[label] = recall
        f1_scores.append(f1)

    macro_f1 = round(sum(f1_scores) / len(VERDICT_LABELS), 4)
    correct_count = sum(1 for rec in records if rec.gold_verdict == rec.predicted_verdict)
    accuracy = round(correct_count / total_cases, 4) if total_cases > 0 else 0.0

    critical_records = [rec for rec in records if rec.claim_kind == "critical"]
    critical_contradict = [rec for rec in critical_records if rec.gold_verdict == "contradict"]
    critical_contradict_hits = sum(1 for rec in critical_contradict if rec.predicted_verdict == "contradict")
    critical_contradict_recall = (
        round(critical_contradict_hits / len(critical_contradict), 4) if critical_contradict else 0.0
    )

    critical_non_support = [rec for rec in critical_records if rec.gold_verdict in ("neutral", "contradict")]
    critical_false_support_count = sum(1 for rec in critical_non_support if rec.predicted_verdict == "support")
    critical_false_support_rate = (
        round(critical_false_support_count / len(critical_non_support), 4) if critical_non_support else 0.0
    )

    p50_ms, p95_ms = summarize_latencies_ms([rec.latency_ms for rec in records])
    total_input_tokens = sum(rec.input_tokens for rec in records)
    total_output_tokens = sum(rec.output_tokens for rec in records)
    total_cost_usd = round(sum(rec.cost_usd for rec in records), 6)
    cost_per_100 = round((total_cost_usd / total_cases) * 100.0, 6) if total_cases > 0 else 0.0
    error_count = sum(1 for rec in records if rec.error is not None)

    return JudgeMetricsReport(
        judge_name=judge_name,
        total_cases=total_cases,
        correct_count=correct_count,
        accuracy=accuracy,
        macro_f1=macro_f1,
        support_recall=round(recalls["support"], 4),
        neutral_recall=round(recalls["neutral"], 4),
        contradict_recall=round(recalls["contradict"], 4),
        critical_case_count=len(critical_records),
        critical_contradict_count=len(critical_contradict),
        critical_contradict_recall=critical_contradict_recall,
        critical_non_support_count=len(critical_non_support),
        critical_false_support_count=critical_false_support_count,
        critical_false_support_rate=critical_false_support_rate,
        passes_adr_0004_critical_fsr=critical_false_support_rate <= ADR_0004_CRITICAL_FSR_LIMIT,
        passes_adr_0004_macro_f1=macro_f1 >= ADR_0004_MACRO_F1_LIMIT,
        latency_ms_p50=p50_ms,
        latency_ms_p95=p95_ms,
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        total_cost_usd=total_cost_usd,
        cost_per_100_claims_usd=cost_per_100,
        error_count=error_count,
        confusion_matrix=confusion,
    )


class OfflineSmokeNliScorer:
    """Deterministic zero-network NLI scorer for `--offline-smoke` CLI execution."""

    device = "offline-smoke-cpu"
    id2label = {0: "entailment", 1: "neutral", 2: "contradiction"}

    def score_pairs(self, pairs: Sequence[tuple[str, str]]) -> Sequence[RawScoreMap]:
        results: list[dict[int, float]] = []
        for premise, hypothesis in pairs:
            claim_features = _lexical_features(hypothesis)
            prem_features = _lexical_features(premise)
            overlap_ratio = len(claim_features & prem_features) / max(1, len(claim_features))
            prem_facts = _extract_facts(premise)
            hyp_facts = _extract_facts(hypothesis)
            has_polarity_conflict = (
                (prem_facts.has_prohibition and hyp_facts.has_permissive)
                or (hyp_facts.has_prohibition and prem_facts.has_permissive)
                or (prem_facts.has_after_meal and hyp_facts.has_before_meal)
                or (prem_facts.has_before_meal and hyp_facts.has_after_meal)
                or (prem_facts.has_fasting_forbid and hyp_facts.has_fasting_allow)
            )
            if has_polarity_conflict or (
                _sets_conflict(prem_facts.daily_freq, hyp_facts.daily_freq)
                or _sets_conflict(prem_facts.per_dose_tab, hyp_facts.per_dose_tab)
                or _sets_conflict(prem_facts.daily_max_tab, hyp_facts.daily_max_tab)
            ):
                results.append({0: 0.03, 1: 0.07, 2: 0.90})
            elif overlap_ratio >= 0.35:
                results.append({0: 0.88, 1: 0.09, 2: 0.03})
            else:
                results.append({0: 0.06, 1: 0.89, 2: 0.05})
        return results


class OfflineSmokeOpenAIClient:
    """Deterministic zero-network OpenAI client stub for `--offline-smoke` CLI execution."""

    class _Chat:
        class _Completions:
            async def create(
                self,
                *,
                model: str,
                messages: Sequence[Mapping[str, str]],
                **kwargs: Any,
            ) -> dict[str, Any]:
                del model, kwargs
                user_text = messages[-1]["content"]
                claim_match = re.search(r"Claim:\s*(.+)\nEvidence:", user_text)
                ev_match = re.search(r"Evidence:\s*(.+)\nReturn JSON:", user_text, re.DOTALL)
                claim = claim_match.group(1).strip() if claim_match else ""
                evidence = ev_match.group(1).strip() if ev_match else ""
                claim_facts = _extract_facts(claim)
                ev_facts = _extract_facts(evidence)

                verdict: ClaimVerdictLiteral = "neutral"
                reason = "근거에서 주장의 핵심 내용을 확인할 수 없습니다."

                conflict = (
                    (ev_facts.has_before_meal and claim_facts.has_after_meal)
                    or (ev_facts.has_after_meal and claim_facts.has_before_meal)
                    or (ev_facts.has_fasting_forbid and claim_facts.has_fasting_allow)
                    or _sets_conflict(ev_facts.daily_freq, claim_facts.daily_freq)
                    or _sets_conflict(ev_facts.per_dose_tab, claim_facts.per_dose_tab)
                    or _sets_conflict(ev_facts.daily_max_tab, claim_facts.daily_max_tab)
                    or (
                        bool(claim_facts.topics & ev_facts.topics)
                        and (
                            (ev_facts.has_prohibition and claim_facts.has_permissive)
                            or (claim_facts.has_prohibition and ev_facts.has_permissive)
                        )
                    )
                )
                if conflict:
                    verdict = "contradict"
                    reason = "근거의 용법·주의사항 또는 극성과 모순됩니다."
                elif (
                    len(_lexical_features(claim) & _lexical_features(evidence)) / max(1, len(_lexical_features(claim)))
                    >= 0.35
                ):
                    verdict = "support"
                    reason = "근거 문장이 주장의 내용을 지지합니다."

                payload = json.dumps({"verdict": verdict, "reason": reason}, ensure_ascii=False)
                prompt_tokens = max(1, estimate_token_count(user_text))
                completion_tokens = max(1, estimate_token_count(payload))
                return {
                    "choices": [{"message": {"content": payload}}],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                    },
                }

        def __init__(self) -> None:
            self.completions = self._Completions()

    def __init__(self) -> None:
        self.chat = self._Chat()


async def evaluate_judge_on_cases(
    *,
    judge: CitationJudge,
    cases: Sequence[CitationBenchmarkCase],
) -> tuple[JudgeMetricsReport, tuple[CaseJudgeRecord, ...]]:
    """Run one judge sequentially across all benchmark cases and aggregate metrics."""
    records: list[CaseJudgeRecord] = []
    for case in cases:
        prediction = await judge.evaluate(
            claim=case.claim,
            evidence=case.evidence,
            claim_kind=case.claim_kind,
        )
        records.append(
            CaseJudgeRecord(
                case_id=case.case_id,
                claim_kind=case.claim_kind,
                gold_verdict=case.gold_verdict,
                predicted_verdict=prediction.verdict,
                latency_ms=prediction.latency_ms,
                input_tokens=prediction.input_tokens,
                output_tokens=prediction.output_tokens,
                cost_usd=prediction.cost_usd,
                error=prediction.error,
                reason=prediction.reason,
            )
        )
    metrics = compute_judge_metrics(judge_name=judge.judge_name, records=records)
    return metrics, tuple(records)


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
        return "python -m ai_worker.tasks.evaluation.citation_judge_compare"
    return " ".join(["python -m ai_worker.tasks.evaluation.citation_judge_compare", *sys.argv[1:]])


def parse_judges_arg(raw_judges: str) -> tuple[JudgeNameLiteral, ...]:
    """Parse and validate comma-separated `--judges` argument."""
    valid_set = set(DEFAULT_JUDGES)
    selected: list[JudgeNameLiteral] = []
    for item in raw_judges.split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        if cleaned not in valid_set:
            raise ValueError(f"Unknown judge {cleaned!r}; expected subset of {', '.join(DEFAULT_JUDGES)}")
        if cleaned not in selected:
            selected.append(cleaned)  # type: ignore[arg-type]
    if not selected:
        raise ValueError("At least one judge must be selected")
    return tuple(selected)


async def run_citation_judge_compare(
    *,
    judges: Sequence[JudgeNameLiteral] = DEFAULT_JUDGES,
    cases: Sequence[CitationBenchmarkCase] = BENCHMARK_CASES,
    offline_smoke: bool = False,
    nli_scorer: NliScorerInput | None = None,
    openai_client: Any | None = None,
    openai_model: str = DEFAULT_OPENAI_MODEL,
    nli_model: str = DEFAULT_NLI_MODEL_NAME,
    device: str = DEFAULT_DEVICE,
    max_sentences: int = DEFAULT_MAX_SENTENCES,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    command: str | None = None,
) -> CitationJudgeCompareReport:
    """Execute selected judges on the benchmark dataset and produce a comparison report."""
    active_nli_scorer: NliScorerInput | None = nli_scorer
    active_openai_client: Any | None = openai_client
    if offline_smoke:
        if active_nli_scorer is None:
            active_nli_scorer = OfflineSmokeNliScorer()
        if active_openai_client is None:
            active_openai_client = OfflineSmokeOpenAIClient()

    shared_nli_judge: LocalNliCitationJudge | None = None
    if "local_nli" in judges or "rule_plus_nli" in judges:
        shared_nli_judge = LocalNliCitationJudge(
            scorer=active_nli_scorer,
            model_name=nli_model,
            device=device,
            max_sentences=max_sentences,
            max_tokens=max_tokens,
        )

    judge_instances: list[CitationJudge] = []
    for name in judges:
        if name == "local_nli":
            assert shared_nli_judge is not None
            judge_instances.append(shared_nli_judge)
        elif name == "rule_plus_nli":
            assert shared_nli_judge is not None
            judge_instances.append(RulePlusNliCitationJudge(nli_judge=shared_nli_judge))
        elif name == "openai_llm_judge":
            judge_instances.append(OpenAILlmCitationJudge(model=openai_model, client=active_openai_client))

    metrics_list: list[JudgeMetricsReport] = []
    case_records_map: dict[str, tuple[CaseJudgeRecord, ...]] = {}
    for judge in judge_instances:
        metrics, records = await evaluate_judge_on_cases(judge=judge, cases=cases)
        metrics_list.append(metrics)
        case_records_map[judge.judge_name] = records

    return CitationJudgeCompareReport(
        benchmark_size=len(cases),
        judges=tuple(metrics_list),
        case_records=case_records_map,
        offline_smoke=offline_smoke,
        openai_model=openai_model,
        nli_model=nli_model,
        git_commit=_resolve_git_commit(),
        command=command if command is not None else _default_command_string(),
    )


def format_compare_report_json(report: CitationJudgeCompareReport) -> str:
    """Serialize `CitationJudgeCompareReport` as indented UTF-8 JSON."""
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n"


def format_compare_report_markdown(report: CitationJudgeCompareReport) -> str:
    """Render `CitationJudgeCompareReport` as a Markdown comparison table."""
    lines = [
        "# Citation Judge Comparison Report",
        "",
        f"- **Benchmark Size**: `{report.benchmark_size}`",
        f"- **Offline Smoke**: `{report.offline_smoke}`",
        f"- **NLI Model**: `{report.nli_model}`",
        f"- **OpenAI Model**: `{report.openai_model}`",
        f"- **Git Commit**: `{report.git_commit}`",
        f"- **Command**: `{report.command}`",
        "",
        (
            "| Judge | Macro-F1 | Support Recall | Neutral Recall | Contradict Recall | "
            "Crit Contradict Recall | Crit False-Support Rate | ADR 0004 (<=0.02) | "
            "P50 (ms) | P95 (ms) | Total Cost ($) | $/100 Claims | Errors |"
        ),
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | :---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in report.judges:
        fsr_pass = "PASS" if item.passes_adr_0004_critical_fsr else "FAIL"
        lines.append(
            f"| `{item.judge_name}` | {item.macro_f1:.4f} | {item.support_recall:.4f} | "
            f"{item.neutral_recall:.4f} | {item.contradict_recall:.4f} | "
            f"{item.critical_contradict_recall:.4f} | {item.critical_false_support_rate:.4f} | "
            f"{fsr_pass} | {item.latency_ms_p50:.3f} | {item.latency_ms_p95:.3f} | "
            f"{item.total_cost_usd:.6f} | {item.cost_per_100_claims_usd:.6f} | {item.error_count} |"
        )
    lines.append("")
    return "\n".join(lines)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare local_nli, rule_plus_nli, and openai_llm_judge on Korean citation benchmark."
    )
    parser.add_argument(
        "--judges",
        default=",".join(DEFAULT_JUDGES),
        help="Comma-separated judge list (default: local_nli,rule_plus_nli,openai_llm_judge).",
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="markdown",
        dest="output_format",
        help="Output format (json or markdown).",
    )
    parser.add_argument(
        "--offline-smoke",
        action="store_true",
        help="Run with deterministic offline fake NLI scorer and OpenAI client (no downloads or API calls).",
    )
    parser.add_argument(
        "--openai-model",
        default=DEFAULT_OPENAI_MODEL,
        help=f"OpenAI model ID for openai_llm_judge (default: {DEFAULT_OPENAI_MODEL}).",
    )
    parser.add_argument(
        "--nli-model",
        default=DEFAULT_NLI_MODEL_NAME,
        help=f"HuggingFace CrossEncoder NLI model ID (default: {DEFAULT_NLI_MODEL_NAME}).",
    )
    parser.add_argument(
        "--device",
        default=DEFAULT_DEVICE,
        help=f"Torch inference device (default: {DEFAULT_DEVICE}).",
    )
    parser.add_argument(
        "--max-sentences",
        type=int,
        default=DEFAULT_MAX_SENTENCES,
        help=f"Top-k sentences kept before NLI inference (default: {DEFAULT_MAX_SENTENCES}).",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help=f"Token bound per NLI pair (default: {DEFAULT_MAX_TOKENS}).",
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
    nli_scorer: NliScorerInput | None = None,
    openai_client: Any | None = None,
) -> int:
    args = _build_arg_parser().parse_args(argv)
    cli_command = (
        " ".join(["python -m ai_worker.tasks.evaluation.citation_judge_compare", *argv])
        if argv is not None
        else _default_command_string()
    )
    try:
        selected_judges = parse_judges_arg(args.judges)
        report = asyncio.run(
            run_citation_judge_compare(
                judges=selected_judges,
                offline_smoke=args.offline_smoke,
                nli_scorer=nli_scorer,
                openai_client=openai_client,
                openai_model=args.openai_model,
                nli_model=args.nli_model,
                device=args.device,
                max_sentences=args.max_sentences,
                max_tokens=args.max_tokens,
                command=cli_command,
            )
        )
        rendered = (
            format_compare_report_json(report)
            if args.output_format == "json"
            else format_compare_report_markdown(report)
        )
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        sys.stderr.write(f"citation_judge_compare error: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
