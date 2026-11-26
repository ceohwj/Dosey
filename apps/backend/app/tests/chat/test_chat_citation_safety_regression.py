"""Offline safety regression suite for `RulePlusNliClaimValidator` and `ChatGeneratorEngine`."""

from __future__ import annotations

import hashlib
import importlib
import json
import time
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.services.chat_ai import (
    ChatGenerationFailedError,
    ChatMedicationInput,
    ChatReplyInput,
    ChatTimeoutError,
)
from app.services.chat_ai.schemas import ProviderChatResponse
from app.services.chat_generator_engine import ChatGeneratorEngine
from rag_runtime.chat_claim_citation_validator import (
    ClaimCandidate,
    ClaimEvidenceRecord,
    CriticalClaimContradictionError,
    NliTriple,
    RulePlusNliClaimValidator,
    select_top_k_evidence_sentences,
)


class _FakeSensitiveText:
    def __init__(self, text: str) -> None:
        self._text = text

    def reveal(self) -> str:
        return self._text


class _FakeEvidenceItem:
    def __init__(
        self,
        *,
        display_order: int,
        content: str,
        knowledge_chunk_id: UUID | None,
        source_code: str = "MFDS_EDB",
        source_version: str = "2026.11.01",
        locator: str = "용법·용량 제1항",
    ) -> None:
        self.display_order = display_order
        self.source_code = source_code
        self.source_version = source_version
        self.locator = locator
        self.knowledge_chunk_id = knowledge_chunk_id
        self.content = _FakeSensitiveText(content)


class _FakeClosedDemoRetriever:
    def __init__(self, items: tuple[_FakeEvidenceItem, ...]) -> None:
        self._items = items

    async def retrieve(self, question: str) -> tuple[_FakeEvidenceItem, ...]:
        del question
        return self._items


class _FakeStructuredProvider:
    def __init__(self, payload: dict[str, object] | str) -> None:
        self._content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.calls: list[dict[str, object]] = []

    async def generate(self, **kwargs: object) -> ProviderChatResponse:
        self.calls.append(kwargs)
        return ProviderChatResponse(content=self._content, model_name="synthetic-chat-model")


def _reply_input(question: str = "복용 방법과 보관 방법을 알려주세요.") -> ChatReplyInput:
    return ChatReplyInput(
        prescription_id=uuid4(),
        medications=[
            ChatMedicationInput(
                medication_name="합성의약품 타이레놀",
                dose_value=Decimal("1"),
                dose_unit="정",
                frequency_per_day=1,
                timing_text="식후",
                duration_days=7,
            )
        ],
        content=question,
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_evidence_record(
    *,
    content: str,
    knowledge_chunk_id: UUID | None = None,
    chunk_id: str | None = None,
    locator: str = "용법·용량 제1항",
) -> ClaimEvidenceRecord:
    resolved_uuid = knowledge_chunk_id
    resolved_chunk_id = chunk_id or (str(resolved_uuid) if resolved_uuid is not None else "ev-1")
    return ClaimEvidenceRecord(
        chunk_id=resolved_chunk_id,
        knowledge_chunk_id=resolved_uuid,
        source_code="MFDS_EDB",
        source_version="2026.11.01",
        locator=locator,
        content=content,
        content_sha256=_sha256(content),
    )


async def test_a1_critical_contradict_blocks_full_assistant_answer_fail_closed() -> None:
    chunk_id = uuid4()
    evidence_text = "성인은 1일 1회 1정을 식후에 복용하십시오."
    retriever = _FakeClosedDemoRetriever(
        (_FakeEvidenceItem(display_order=1, content=evidence_text, knowledge_chunk_id=chunk_id),)
    )
    provider = _FakeStructuredProvider(
        {
            "answer": "복용 안내는 근거를 확인하세요.",
            "claims": [
                {
                    "claim_key": "c1",
                    "claim_text": "이 약은 성인에게 처방됩니다.",
                    "claim_kind": "critical",
                    "evidence_chunk_ids": [str(chunk_id)],
                }
            ],
        }
    )

    def _contradict_scorer(premise: str, hypothesis: str) -> NliTriple:
        del premise, hypothesis
        return (0.05, 0.10, 0.85)

    engine = ChatGeneratorEngine(
        provider=provider,
        model="synthetic-model",
        timeout_seconds=2.0,
        closed_demo_retriever=retriever,  # type: ignore[arg-type]
        citation_validator=RulePlusNliClaimValidator(scorer=_contradict_scorer),
    )

    with pytest.raises(ChatGenerationFailedError) as exc_info:
        await engine.reply(_reply_input())

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


async def test_a1_mixed_verdicts_filter_neutral_critical_and_contradict_non_critical() -> None:
    chunk_1 = uuid4()
    chunk_2 = uuid4()
    ev_1_text = "성인은 1일 1회 1정을 식후 30분에 충분한 물과 함께 복용하십시오."
    ev_2_text = "직사광선을 피하여 실온의 건조한 장소에 밀폐 보관하십시오."
    retriever = _FakeClosedDemoRetriever(
        (
            _FakeEvidenceItem(display_order=1, content=ev_1_text, knowledge_chunk_id=chunk_1, locator="용법·용량"),
            _FakeEvidenceItem(display_order=2, content=ev_2_text, knowledge_chunk_id=chunk_2, locator="보관방법"),
        )
    )
    provider = _FakeStructuredProvider(
        {
            "answer": "성인은 1일 1회 1정을 식후 30분에 복용하세요. 보관은 실온에서 하고, 외포장은 종이로 분리배출할 수 있습니다.",
            "claims": [
                {
                    "claim_key": "crit-supported",
                    "claim_text": "성인은 1일 1회 1정을 식후 30분에 복용하세요.",
                    "claim_kind": "critical",
                    "evidence_chunk_ids": [str(chunk_1)],
                },
                {
                    "claim_key": "crit-neutral",
                    "claim_text": "신장 기능이 정상이면 8시간 간격을 두세요.",
                    "claim_kind": "critical",
                    "evidence_chunk_ids": [str(chunk_1)],
                },
                {
                    "claim_key": "noncrit-contradict",
                    "claim_text": "습기가 많은 욕실에 보관하세요.",
                    "claim_kind": "non_critical",
                    "evidence_chunk_ids": [str(chunk_2)],
                },
                {
                    "claim_key": "noncrit-neutral",
                    "claim_text": "외포장 종이 상자는 분리배출이 가능합니다.",
                    "claim_kind": "non_critical",
                    "evidence_chunk_ids": [str(chunk_2)],
                },
                {
                    "claim_key": "noncrit-no-evidence",
                    "claim_text": "제조소 정보가 다를 수 있습니다.",
                    "claim_kind": "non_critical",
                    "evidence_chunk_ids": [],
                },
            ],
        }
    )

    recorded_pairs: list[tuple[str, str]] = []

    def _routing_scorer(premise: str, hypothesis: str) -> NliTriple:
        recorded_pairs.append((premise, hypothesis))
        if "식후 30분" in hypothesis:
            return (0.91, 0.06, 0.03)
        if "욕실에 보관" in hypothesis:
            return (0.04, 0.16, 0.80)
        return (0.15, 0.75, 0.10)

    engine = ChatGeneratorEngine(
        provider=provider,
        model="synthetic-model",
        timeout_seconds=2.0,
        closed_demo_retriever=retriever,  # type: ignore[arg-type]
        citation_validator=RulePlusNliClaimValidator(scorer=_routing_scorer),
    )

    result = await engine.reply(_reply_input())

    assert result.prompt_version == "chat-prompt-v7-claim-citation"
    assert result.citations is not None
    assert len(result.citations) == 2

    # Verify `(premise=evidence_snippet, hypothesis=claim_text)` ordering was preserved.
    assert any(premise == ev_1_text and "식후 30분" in hypothesis for premise, hypothesis in recorded_pairs)

    first, second = result.citations
    assert first.claim_key == "crit-supported"
    assert first.claim_kind == "CRITICAL"
    assert first.verdict == "SUPPORTED"
    assert first.knowledge_chunk_id == chunk_1
    assert first.content_sha256 == _sha256(ev_1_text)
    assert first.cited_text == ev_1_text
    assert first.display_order == 1

    assert second.claim_key == "noncrit-neutral"
    assert second.claim_kind == "NON_CRITICAL"
    assert second.verdict == "NEEDS_VERIFICATION"
    assert second.knowledge_chunk_id is None
    assert second.content_sha256 is None
    assert second.cited_text is None
    assert second.locator == "보관방법"
    assert second.display_order == 2


def test_unpersisted_chunk_cannot_emit_supported_citation() -> None:
    validator = RulePlusNliClaimValidator(scorer=lambda _p, _h: (0.95, 0.03, 0.02))
    ev = _make_evidence_record(
        content="직사광선을 피하여 실온에 보관하십시오.", knowledge_chunk_id=None, chunk_id="ev-1"
    )
    claims = [
        ClaimCandidate(
            claim_key="c1",
            claim_text="실온에 보관하세요.",
            claim_kind="non_critical",
            evidence_chunk_ids=("ev-1",),
        )
    ]

    records = validator.validate(claims=claims, evidence=[ev])
    assert records == []


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "증상이 나아졌으니 약을 임의로 중단해도 됩니다.",
        "오늘부터 복용을 중단하세요.",
        "속이 불편하면 약을 끊으세요.",
        "통증이 심하면 용량을 늘리세요.",
        "상태가 좋으면 용량을 줄이세요.",
        "아침 약을 잊었으면 저녁에 두 배로 드세요.",
        "필요시 임의 증량하여 복용하십시오.",
        "컨디션에 따라 임의 감량할 수 있습니다.",
    ],
)
async def test_unsafe_medication_modification_advice_blocked_fail_closed(unsafe_text: str) -> None:
    chunk_id = uuid4()
    ev_text = "정해진 용법과 용량을 준수하고 복용 변경 시 의사 또는 약사와 상의하십시오."
    retriever = _FakeClosedDemoRetriever(
        (_FakeEvidenceItem(display_order=1, content=ev_text, knowledge_chunk_id=chunk_id),)
    )

    # Case A: unsafe phrase inside claim
    provider_claim = _FakeStructuredProvider(
        {
            "answer": "복약 안내를 확인하세요.",
            "claims": [
                {
                    "claim_key": "c-unsafe",
                    "claim_text": unsafe_text,
                    "claim_kind": "critical",
                    "evidence_chunk_ids": [str(chunk_id)],
                }
            ],
        }
    )
    engine_claim = ChatGeneratorEngine(
        provider=provider_claim,
        model="synthetic-model",
        timeout_seconds=2.0,
        closed_demo_retriever=retriever,  # type: ignore[arg-type]
        citation_validator=RulePlusNliClaimValidator(scorer=lambda _p, _h: (0.99, 0.01, 0.0)),
    )
    with pytest.raises(ChatGenerationFailedError):
        await engine_claim.reply(_reply_input())

    # Case B: unsafe phrase inside top-level answer_text even with empty claims
    provider_answer = _FakeStructuredProvider({"answer": unsafe_text, "claims": []})
    engine_answer = ChatGeneratorEngine(
        provider=provider_answer,
        model="synthetic-model",
        timeout_seconds=2.0,
        closed_demo_retriever=retriever,  # type: ignore[arg-type]
        citation_validator=RulePlusNliClaimValidator(scorer=lambda _p, _h: (0.99, 0.01, 0.0)),
    )
    with pytest.raises(ChatGenerationFailedError):
        await engine_answer.reply(_reply_input())


def test_safe_prohibition_of_arbitrary_modification_is_not_falsely_blocked() -> None:
    chunk_id = uuid4()
    ev_text = "의사 또는 약사와 상의 없이 임의로 중단하지 마십시오."
    ev = _make_evidence_record(content=ev_text, knowledge_chunk_id=chunk_id)
    validator = RulePlusNliClaimValidator(scorer=lambda _p, _h: (0.92, 0.05, 0.03))

    records = validator.validate(
        claims=[
            ClaimCandidate(
                claim_key="c1",
                claim_text="증상이 좋아져도 임의로 중단하지 마세요.",
                claim_kind="critical",
                evidence_chunk_ids=(str(chunk_id),),
            )
        ],
        evidence=[ev],
        answer_text="증상이 좋아져도 임의로 중단하지 마세요. 두 배로 복용하지 마세요.",
    )
    assert len(records) == 1
    assert records[0].verdict == "SUPPORTED"


@pytest.mark.parametrize(
    ("claim_text", "evidence_text"),
    [
        # Numeric frequency / tablet / dose unit conflicts
        ("성인은 1일 2회 1정씩 복용합니다.", "성인은 1일 1회 1정을 매일 같은 시간에 복용하십시오."),
        ("한 번에 2정씩 복용하세요.", "성인은 1회 1정을 충분한 물과 함께 복용하십시오."),
        ("성인은 1회 10mg을 복용합니다.", "성인은 1회 5mg을 경구 투여하십시오."),
        # Timing conflicts
        ("반드시 식전에 복용해야 합니다.", "위장 부담을 줄이기 위해 식후 30분에 복용하십시오."),
        ("빈속에 공복으로 복용해도 괜찮습니다.", "위장관 자극이 있으므로 공복 복용은 피하십시오."),
        ("이 약은 취침 전에 복용하는 약입니다.", "이 약은 아침 식후에 복용하십시오."),
        # Contraindication / prohibition polarity flips
        ("임산부도 안전하게 복용 가능합니다.", "임부 또는 임신하고 있을 가능성이 있는 여성에게는 투여하지 않는다."),
        (
            "아세트아미노펜 감기약과 중복 복용해도 무방합니다.",
            "아세트아미노펜이 포함된 다른 제품과 함께 복용하지 마십시오.",
        ),
        ("임부 복용 가능 약물입니다.", "사용상의 주의사항: 임부는 투여 금기입니다."),
    ],
)
def test_deterministic_rule_guard_catches_conflicts_even_if_nli_returns_support(
    claim_text: str,
    evidence_text: str,
) -> None:
    chunk_id = uuid4()
    ev = _make_evidence_record(content=evidence_text, knowledge_chunk_id=chunk_id)
    # Intentionally deceptive NLI scorer returning high support!
    validator = RulePlusNliClaimValidator(scorer=lambda _p, _h: (0.98, 0.01, 0.01))

    with pytest.raises(CriticalClaimContradictionError):
        validator.validate(
            claims=[
                ClaimCandidate(
                    claim_key="c-conflict",
                    claim_text=claim_text,
                    claim_kind="critical",
                    evidence_chunk_ids=(str(chunk_id),),
                )
            ],
            evidence=[ev],
        )


def test_critical_claim_with_unverified_numbers_downgrades_support_to_neutral_excluded() -> None:
    chunk_id = uuid4()
    ev = _make_evidence_record(
        content="직사광선을 피하여 실온의 건조한 곳에 보관하십시오.",
        knowledge_chunk_id=chunk_id,
    )
    validator = RulePlusNliClaimValidator(scorer=lambda _p, _h: (0.90, 0.08, 0.02))

    records = validator.validate(
        claims=[
            ClaimCandidate(
                claim_key="c-unverified-num",
                claim_text="성인은 1일 2회 500mg을 복용합니다.",
                claim_kind="critical",
                evidence_chunk_ids=(str(chunk_id),),
            )
        ],
        evidence=[ev],
    )
    assert records == []


def test_sentence_prefilter_enforces_top_k_and_512_budget() -> None:
    filler = "이 약은 흰색의 원형 필름코팅정제로서 직사광선을 피하여 실온의 건조한 장소에 밀폐 보관하십시오. " * 15
    target = "성인은 1일 1회 1정을 식후 30분에 물과 함께 복용하십시오."
    selected = select_top_k_evidence_sentences(
        claim="성인은 하루 한 번 1정을 식후 30분에 복용하나요?",
        evidence=f"{filler} {target}",
        max_sentences=3,
        max_tokens=512,
        max_chars=512,
    )
    premise = " ".join(selected)
    assert len(selected) <= 3
    assert len(premise) <= 512
    assert target in premise


def test_missing_sentence_transformers_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = importlib.import_module

    def _failing_import(name: str, package: str | None = None) -> object:
        if name == "sentence_transformers":
            raise ImportError("sentence_transformers not installed")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", _failing_import)

    chunk_id = uuid4()
    ev = _make_evidence_record(content="식후 30분에 복용하십시오.", knowledge_chunk_id=chunk_id)
    validator = RulePlusNliClaimValidator()

    with pytest.raises(RuntimeError, match="sentence-transformers"):
        validator.validate(
            claims=[
                ClaimCandidate(
                    claim_key="c1",
                    claim_text="식후 30분에 복용합니다.",
                    claim_kind="critical",
                    evidence_chunk_ids=(str(chunk_id),),
                )
            ],
            evidence=[ev],
        )


async def test_crm10_timeout_inside_validator_triggers_chat_timeout_error() -> None:
    chunk_id = uuid4()
    ev_text = "성인은 1일 1회 1정을 식후에 복용하십시오."
    retriever = _FakeClosedDemoRetriever(
        (_FakeEvidenceItem(display_order=1, content=ev_text, knowledge_chunk_id=chunk_id),)
    )
    provider = _FakeStructuredProvider(
        {
            "answer": "성인은 1일 1회 1정을 식후에 복용하세요.",
            "claims": [
                {
                    "claim_key": "c1",
                    "claim_text": "성인은 1일 1회 1정을 식후에 복용하세요.",
                    "claim_kind": "critical",
                    "evidence_chunk_ids": [str(chunk_id)],
                }
            ],
        }
    )

    def _slow_scorer(premise: str, hypothesis: str) -> NliTriple:
        del premise, hypothesis
        time.sleep(0.25)
        return (0.9, 0.05, 0.05)

    engine = ChatGeneratorEngine(
        provider=provider,
        model="synthetic-model",
        timeout_seconds=0.05,
        closed_demo_retriever=retriever,  # type: ignore[arg-type]
        citation_validator=RulePlusNliClaimValidator(scorer=_slow_scorer),
    )

    with pytest.raises(ChatTimeoutError) as exc_info:
        await engine.reply(_reply_input())

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


async def test_citation_validator_none_preserves_legacy_v6_and_v7_closed_demo_paths() -> None:
    legacy_provider = _FakeStructuredProvider("기존 v6 평문 답변")
    legacy_engine = ChatGeneratorEngine(
        provider=legacy_provider,
        model="synthetic-model",
        timeout_seconds=2.0,
        citation_validator=None,
    )
    legacy_result = await legacy_engine.reply(_reply_input())
    assert legacy_result.content == "기존 v6 평문 답변"
    assert legacy_result.prompt_version == "chat-prompt-v6"
    assert legacy_result.citations is None

    chunk_id = uuid4()
    retriever = _FakeClosedDemoRetriever(
        (_FakeEvidenceItem(display_order=1, content="식후에 복용하십시오.", knowledge_chunk_id=chunk_id),)
    )
    closed_demo_provider = _FakeStructuredProvider("기존 v7 closed-demo 평문 답변")
    closed_demo_engine = ChatGeneratorEngine(
        provider=closed_demo_provider,
        model="synthetic-model",
        timeout_seconds=2.0,
        closed_demo_retriever=retriever,  # type: ignore[arg-type]
        citation_validator=None,
    )
    closed_demo_result = await closed_demo_engine.reply(_reply_input())
    assert closed_demo_result.content == "기존 v7 closed-demo 평문 답변"
    assert closed_demo_result.prompt_version == "chat-prompt-v7-closed-demo-evidence"
    assert closed_demo_result.citations is None


async def test_engine_citation_validator_fails_closed_when_evidence_lacks_persisted_chunk_id() -> None:
    retriever = _FakeClosedDemoRetriever(
        (_FakeEvidenceItem(display_order=1, content="식후에 1정씩 복용하십시오.", knowledge_chunk_id=None),)
    )
    provider = _FakeStructuredProvider(
        {
            "answer": "식후에 1정씩 복용하세요.",
            "claims": [
                {
                    "claim_key": "c1",
                    "claim_text": "식후에 1정씩 복용하세요.",
                    "claim_kind": "critical",
                    "evidence_chunk_ids": ["ev-1"],
                }
            ],
        }
    )
    engine = ChatGeneratorEngine(
        provider=provider,
        model="synthetic-model",
        timeout_seconds=2.0,
        closed_demo_retriever=retriever,  # type: ignore[arg-type]
        citation_validator=RulePlusNliClaimValidator(scorer=lambda _p, _h: (0.95, 0.03, 0.02)),
    )

    with pytest.raises(ChatGenerationFailedError):
        await engine.reply(_reply_input())
    assert provider.calls == []


@pytest.mark.parametrize(
    "unsafe_claim",
    [
        "상태에 따라 임의 감량할 수 있습니다. 절대 두 배로 복용하지 마세요.",
        "증상이 심하면 용량을 늘려도 됩니다.",
        "어지러우면 반으로 줄여도 됩니다.",
        "까먹었을 때는 두 배 복용하세요.",
        "불편하면 복용을 멈추세요.",
        "하루 정도는 건너뛰세요.",
    ],
)
async def test_multi_sentence_bypass_and_korean_conjugation_unsafe_advice_are_blocked(unsafe_claim: str) -> None:
    chunk_id = uuid4()
    ev_text = "정해진 용법·용량에 따라 복용하십시오."
    retriever = _FakeClosedDemoRetriever(
        (_FakeEvidenceItem(display_order=1, content=ev_text, knowledge_chunk_id=chunk_id),)
    )
    provider = _FakeStructuredProvider(
        {
            "answer": unsafe_claim,
            "claims": [
                {
                    "claim_key": "c1",
                    "claim_text": unsafe_claim,
                    "claim_kind": "critical",
                    "evidence_chunk_ids": [str(chunk_id)],
                }
            ],
        }
    )
    engine = ChatGeneratorEngine(
        provider=provider,
        model="synthetic-model",
        timeout_seconds=2.0,
        closed_demo_retriever=retriever,  # type: ignore[arg-type]
        citation_validator=RulePlusNliClaimValidator(scorer=lambda _p, _h: (0.92, 0.06, 0.02)),
    )

    with pytest.raises(ChatGenerationFailedError):
        await engine.reply(_reply_input())


async def test_safe_non_discontinuation_instruction_is_not_false_blocked() -> None:
    chunk_id = uuid4()
    ev_text = "의사 또는 약사와 상의 없이 임의로 복용을 중단하지 마십시오."
    retriever = _FakeClosedDemoRetriever(
        (_FakeEvidenceItem(display_order=1, content=ev_text, knowledge_chunk_id=chunk_id),)
    )
    safe_claim = "증상이 좋아져도 임의로 복용을 중단하지 마세요."
    provider = _FakeStructuredProvider(
        {
            "answer": safe_claim,
            "claims": [
                {
                    "claim_key": "c1",
                    "claim_text": safe_claim,
                    "claim_kind": "critical",
                    "evidence_chunk_ids": [str(chunk_id)],
                }
            ],
        }
    )
    engine = ChatGeneratorEngine(
        provider=provider,
        model="synthetic-model",
        timeout_seconds=2.0,
        closed_demo_retriever=retriever,  # type: ignore[arg-type]
        citation_validator=RulePlusNliClaimValidator(scorer=lambda _p, _h: (0.94, 0.04, 0.02)),
    )

    result = await engine.reply(_reply_input())
    assert result.citations is not None and len(result.citations) == 1
    assert result.citations[0].verdict == "SUPPORTED"


def test_capsule_unit_mismatch_triggers_critical_contradiction() -> None:
    chunk_id = uuid4()
    ev = _make_evidence_record(content="성인은 1캡슐을 물과 함께 복용하십시오.", knowledge_chunk_id=chunk_id)
    validator = RulePlusNliClaimValidator(scorer=lambda _p, _h: (0.92, 0.05, 0.03))

    with pytest.raises(CriticalClaimContradictionError):
        validator.validate(
            claims=[
                ClaimCandidate(
                    claim_key="c1",
                    claim_text="성인은 2캡슐을 복용할 수 있습니다.",
                    claim_kind="critical",
                    evidence_chunk_ids=(str(chunk_id),),
                )
            ],
            evidence=[ev],
        )
