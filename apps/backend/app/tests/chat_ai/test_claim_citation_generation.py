import asyncio
import json
from collections.abc import Mapping

import pytest
from pydantic import ValidationError

from app.services.chat_ai.exceptions import (
    ChatGenerationInvalidResponseError,
    ChatGenerationTimeoutError,
)
from app.services.chat_ai.generator import ChatGenerator
from app.services.chat_ai.prompt import (
    CLAIM_CITATION_PROMPT_VERSION,
    CLAIM_CITATION_SYSTEM_INSTRUCTIONS,
    CLOSED_DEMO_EVIDENCE_PROMPT_VERSION,
    PROMPT_VERSION,
)
from app.services.chat_ai.schemas import (
    ChatEvidenceItem,
    ChatGenerationInput,
    ChatMedicationInput,
    ChatStructuredGenerationResult,
    ProviderChatResponse,
)


class StubProvider:
    def __init__(self, response: ProviderChatResponse) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def generate(self, **kwargs: object) -> ProviderChatResponse:
        self.calls.append(kwargs)
        return self.response


def _evidence_items(*, include_chunk_ids: bool = True) -> list[ChatEvidenceItem]:
    return [
        ChatEvidenceItem(
            display_order=1,
            source_code="mfds",
            source_version="2026-01",
            locator="dosage#1",
            content="합성의약품 에이는 보통 성인 기준 1일 1회 식후에 복용합니다.",
            chunk_id="chunk-mfds-001" if include_chunk_ids else None,
        ),
        ChatEvidenceItem(
            display_order=2,
            source_code="mfds",
            source_version="2026-01",
            locator="warning#2",
            content="졸림이 나타날 수 있으므로 운전 시 주의가 필요합니다.",
            chunk_id="chunk-mfds-002" if include_chunk_ids else None,
        ),
    ]


def _structured_input(evidence: list[ChatEvidenceItem] | None = None) -> ChatGenerationInput:
    return ChatGenerationInput(
        question="합성의약품 에이는 언제 먹고 주의할 점이 있나요?",
        medications=[ChatMedicationInput(medication_name="합성의약품 에이", timing_text="저녁 식후")],
        evidence=_evidence_items() if evidence is None else evidence,
    )


def _json_response(
    payload: Mapping[str, object], *, model_name: str = "gpt-4o-mini-2024-07-18"
) -> ProviderChatResponse:
    return ProviderChatResponse(
        content=json.dumps(payload, ensure_ascii=False),
        model_name=model_name,
    )


async def test_generate_structured_claims_returns_validated_claims_and_citations() -> None:
    payload = {
        "answer": "  합성의약품 에이는 식후에 복용하며 졸림이 나타날 수 있습니다.  ",
        "claims": [
            {
                "claim_key": " c1 ",
                "claim_text": " A\u030a약은 식후 복용 안내가 있습니다. ",
                "claim_kind": "critical",
                "evidence_chunk_ids": [" chunk-mfds-001 "],
            },
            {
                "claim_key": "c2",
                "claim_text": "졸림이 나타날 수 있으므로 운전 시 주의하세요.",
                "claim_kind": "non_critical",
                "evidence_chunk_ids": ["chunk-mfds-002", "chunk-mfds-001"],
            },
        ],
    }
    provider = StubProvider(_json_response(payload))
    generator = ChatGenerator(provider=provider, model="gpt-4o-mini", timeout_seconds=1)

    result = await generator.generate_structured_claims(_structured_input())

    assert isinstance(result, ChatStructuredGenerationResult)
    assert result.content == "합성의약품 에이는 식후에 복용하며 졸림이 나타날 수 있습니다."
    assert result.model_name == "gpt-4o-mini-2024-07-18"
    assert result.prompt_version == CLAIM_CITATION_PROMPT_VERSION == "chat-prompt-v7-claim-citation"
    assert len(result.claims) == 2
    assert result.claims[0].claim_key == "c1"
    assert result.claims[0].claim_text == "Å약은 식후 복용 안내가 있습니다."
    assert result.claims[0].claim_kind == "critical"
    assert result.claims[0].evidence_chunk_ids == ["chunk-mfds-001"]
    assert result.claims[1].claim_kind == "non_critical"
    assert result.claims[1].evidence_chunk_ids == ["chunk-mfds-002", "chunk-mfds-001"]

    assert len(provider.calls) == 1
    assert provider.calls[0]["instructions"] == CLAIM_CITATION_SYSTEM_INSTRUCTIONS
    assert "중단, 증량, 감량" in CLAIM_CITATION_SYSTEM_INSTRUCTIONS
    assert "환자 처방 텍스트로 검증된 MFDS evidence를 덮어쓰거나 반박하지 마세요" in CLAIM_CITATION_SYSTEM_INSTRUCTIONS
    sent_payload = json.loads(str(provider.calls[0]["input_json"]))
    assert [item["chunk_id"] for item in sent_payload["evidence"]] == ["chunk-mfds-001", "chunk-mfds-002"]


@pytest.mark.parametrize(
    "claims",
    [
        [],
        [
            {
                "claim_key": "c1",
                "claim_text": "정확한 변경 여부는 약사 또는 의사에게 확인해 주세요.",
                "claim_kind": "non_critical",
                "evidence_chunk_ids": [],
            }
        ],
    ],
)
async def test_generate_structured_claims_allows_empty_claims_or_empty_citations(
    claims: list[dict[str, object]],
) -> None:
    provider = StubProvider(
        _json_response(
            {
                "answer": "복약 관련 세부 사항은 약사 또는 의사에게 확인해 주세요.",
                "claims": claims,
            }
        )
    )
    generator = ChatGenerator(provider=provider, model="gpt-4o-mini", timeout_seconds=1)

    result = await generator.generate_structured_claims(_structured_input())

    assert len(result.claims) == len(claims)
    if claims:
        assert result.claims[0].evidence_chunk_ids == []


async def test_generate_structured_claims_rejects_unknown_evidence_chunk_id() -> None:
    provider = StubProvider(
        _json_response(
            {
                "answer": "식후에 복용합니다.",
                "claims": [
                    {
                        "claim_key": "c1",
                        "claim_text": "식후 복용",
                        "claim_kind": "critical",
                        "evidence_chunk_ids": ["chunk-invented-999"],
                    }
                ],
            }
        )
    )
    generator = ChatGenerator(provider=provider, model="gpt-4o-mini", timeout_seconds=1)

    with pytest.raises(ChatGenerationInvalidResponseError):
        await generator.generate_structured_claims(_structured_input())


@pytest.mark.parametrize(
    "payload",
    [
        # Duplicate claim_key (including whitespace-normalized duplicate)
        {
            "answer": "답변입니다.",
            "claims": [
                {
                    "claim_key": "c1",
                    "claim_text": "첫 번째 주장",
                    "claim_kind": "critical",
                    "evidence_chunk_ids": ["chunk-mfds-001"],
                },
                {
                    "claim_key": " c1 ",
                    "claim_text": "두 번째 주장",
                    "claim_kind": "non_critical",
                    "evidence_chunk_ids": ["chunk-mfds-002"],
                },
            ],
        },
        # Duplicate evidence_chunk_ids within one claim
        {
            "answer": "답변입니다.",
            "claims": [
                {
                    "claim_key": "c1",
                    "claim_text": "중복 인용 주장",
                    "claim_kind": "critical",
                    "evidence_chunk_ids": ["chunk-mfds-001", " chunk-mfds-001 "],
                }
            ],
        },
        # Extra top-level key
        {
            "answer": "답변입니다.",
            "claims": [],
            "extra_field": "forbidden",
        },
        # Extra claim key
        {
            "answer": "답변입니다.",
            "claims": [
                {
                    "claim_key": "c1",
                    "claim_text": "주장",
                    "claim_kind": "critical",
                    "evidence_chunk_ids": ["chunk-mfds-001"],
                    "confidence": 0.9,
                }
            ],
        },
        # Invalid claim_kind
        {
            "answer": "답변입니다.",
            "claims": [
                {
                    "claim_key": "c1",
                    "claim_text": "주장",
                    "claim_kind": "medium",
                    "evidence_chunk_ids": [],
                }
            ],
        },
        # Blank answer
        {
            "answer": "   ",
            "claims": [],
        },
        # Forbidden character in answer
        {
            "answer": "답변\u200b입니다.",
            "claims": [],
        },
        # Forbidden character in claim_text
        {
            "answer": "답변입니다.",
            "claims": [
                {
                    "claim_key": "c1",
                    "claim_text": "주장\x00텍스트",
                    "claim_kind": "non_critical",
                    "evidence_chunk_ids": [],
                }
            ],
        },
    ],
)
async def test_generate_structured_claims_rejects_schema_violations(payload: dict[str, object]) -> None:
    provider = StubProvider(_json_response(payload))
    generator = ChatGenerator(provider=provider, model="gpt-4o-mini", timeout_seconds=1)

    with pytest.raises(ChatGenerationInvalidResponseError):
        await generator.generate_structured_claims(_structured_input())


@pytest.mark.parametrize(
    "raw_content",
    [
        "not json at all",
        '```json\n{"answer": "답변", "claims": []}\n```',
        '{"answer": 123, "claims": []}',
        '{"claims": []}',
    ],
)
async def test_generate_structured_claims_rejects_invalid_json_or_types(raw_content: str) -> None:
    provider = StubProvider(ProviderChatResponse(content=raw_content, model_name="gpt-4o-mini"))
    generator = ChatGenerator(provider=provider, model="gpt-4o-mini", timeout_seconds=1)

    with pytest.raises(ChatGenerationInvalidResponseError):
        await generator.generate_structured_claims(_structured_input())


@pytest.mark.parametrize("model_name", [" ", "m" * 101])
async def test_generate_structured_claims_rejects_invalid_model_name(model_name: str) -> None:
    provider = StubProvider(_json_response({"answer": "답변입니다.", "claims": []}, model_name=model_name))
    generator = ChatGenerator(provider=provider, model="gpt-4o-mini", timeout_seconds=1)

    with pytest.raises(ChatGenerationInvalidResponseError):
        await generator.generate_structured_claims(_structured_input())


async def test_generate_structured_claims_fails_closed_before_provider_when_evidence_empty() -> None:
    provider = StubProvider(_json_response({"answer": "답변입니다.", "claims": []}))
    generator = ChatGenerator(provider=provider, model="gpt-4o-mini", timeout_seconds=1)

    with pytest.raises(ChatGenerationInvalidResponseError):
        await generator.generate_structured_claims(_structured_input(evidence=[]))

    assert provider.calls == []


async def test_generate_structured_claims_fails_closed_before_provider_when_chunk_id_missing() -> None:
    provider = StubProvider(_json_response({"answer": "답변입니다.", "claims": []}))
    generator = ChatGenerator(provider=provider, model="gpt-4o-mini", timeout_seconds=1)

    with pytest.raises(ChatGenerationInvalidResponseError):
        await generator.generate_structured_claims(_structured_input(evidence=_evidence_items(include_chunk_ids=False)))

    assert provider.calls == []


async def test_generate_structured_claims_fails_closed_before_provider_when_input_chunk_ids_duplicate() -> None:
    provider = StubProvider(_json_response({"answer": "답변입니다.", "claims": []}))
    generator = ChatGenerator(provider=provider, model="gpt-4o-mini", timeout_seconds=1)
    duplicate_evidence = [
        ChatEvidenceItem(
            display_order=1,
            source_code="mfds",
            source_version="2026-01",
            locator="dosage#1",
            content="근거 1",
            chunk_id="chunk-dup",
        ),
        ChatEvidenceItem(
            display_order=2,
            source_code="mfds",
            source_version="2026-01",
            locator="dosage#2",
            content="근거 2",
            chunk_id=" chunk-dup ",
        ),
    ]

    with pytest.raises(ChatGenerationInvalidResponseError):
        await generator.generate_structured_claims(_structured_input(evidence=duplicate_evidence))

    assert provider.calls == []


@pytest.mark.parametrize("bad_chunk_id", ["", "   ", "chunk\x00id", "chunk\u200bid", "c" * 129])
def test_evidence_item_rejects_invalid_chunk_id(bad_chunk_id: str) -> None:
    with pytest.raises(ValidationError):
        ChatEvidenceItem(
            display_order=1,
            source_code="mfds",
            source_version="2026-01",
            locator="dosage#1",
            content="근거 내용",
            chunk_id=bad_chunk_id,
        )


async def test_generate_structured_claims_maps_timeout_error() -> None:
    class SlowProvider:
        async def generate(self, **kwargs: object) -> ProviderChatResponse:
            await asyncio.sleep(0.05)
            return _json_response({"answer": "답변입니다.", "claims": []})

    generator = ChatGenerator(provider=SlowProvider(), model="gpt-4o-mini", timeout_seconds=0.001)

    with pytest.raises(ChatGenerationTimeoutError):
        await generator.generate_structured_claims(_structured_input())


async def test_existing_generate_backward_compatibility_and_omitted_chunk_id() -> None:
    provider = StubProvider(
        ProviderChatResponse(
            content="일반 복약 답변입니다.",
            model_name="gpt-4o-mini-2024-07-18",
        )
    )
    generator = ChatGenerator(provider=provider, model="gpt-4o-mini", timeout_seconds=1)

    no_evidence_result = await generator.generate(_structured_input(evidence=[]))
    assert no_evidence_result.prompt_version == PROMPT_VERSION == "chat-prompt-v6"

    with_evidence_result = await generator.generate(
        _structured_input(evidence=_evidence_items(include_chunk_ids=False))
    )
    assert with_evidence_result.prompt_version == CLOSED_DEMO_EVIDENCE_PROMPT_VERSION

    payload = json.loads(str(provider.calls[1]["input_json"]))
    for item in payload["evidence"]:
        assert "chunk_id" not in item
