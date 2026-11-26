import re
import unicodedata
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator, model_validator

_FORBIDDEN_INPUT_CHARACTERS = frozenset(
    {
        "\x00",
        "\u200b",
        "\u200c",
        "\u200d",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
        "\ufeff",
    }
)
_WHITESPACE_PATTERN = re.compile(r"\s+")


def _reject_forbidden_input_characters(value: str) -> None:
    if any(character in value for character in _FORBIDDEN_INPUT_CHARACTERS):
        raise ValueError("input text contains a forbidden character")


def _normalize_question(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("question must be a string")
    normalized = unicodedata.normalize("NFC", value).strip()
    _reject_forbidden_input_characters(normalized)
    if not normalized:
        raise ValueError("question must not be blank")
    return normalized


def _normalize_display_text(value: str) -> str:
    normalized = _WHITESPACE_PATTERN.sub(" ", unicodedata.normalize("NFC", value).strip())
    _reject_forbidden_input_characters(normalized)
    if not normalized:
        raise ValueError("display text must not be blank")
    return normalized


def _normalize_generated_content(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("generated content must be a string")
    return unicodedata.normalize("NFC", value.strip())


def normalize_chat_answer(value: object) -> str:
    normalized = _normalize_generated_content(value)
    _reject_forbidden_input_characters(normalized)
    if not normalized:
        raise ValueError("chat answer must not be blank")
    return normalized


def _normalize_trimmed_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = unicodedata.normalize("NFC", value).strip()
    _reject_forbidden_input_characters(normalized)
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    return normalized


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _StrictGeneratedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ChatMedicationInput(_StrictModel):
    medication_name: str = Field(max_length=255)
    strength_text: str | None = Field(
        default=None,
        max_length=100,
    )
    dose_value: Decimal | None = Field(default=None, gt=0)
    dose_unit: str | None = Field(default=None, max_length=50)
    frequency_per_day: int | None = Field(default=None, gt=0)
    timing_text: str | None = Field(default=None, max_length=255)
    duration_days: int | None = Field(default=None, gt=0)

    @field_validator("medication_name")
    @classmethod
    def normalize_medication_name(cls, value: str) -> str:
        return _normalize_display_text(value)

    @field_validator("strength_text", "dose_unit", "timing_text")
    @classmethod
    def normalize_optional_display_text(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return _normalize_display_text(value)


class ChatHistoryItem(_StrictModel):
    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=10_000)

    @field_validator("question", mode="before")
    @classmethod
    def normalize_question(cls, value: object) -> str:
        return _normalize_question(value)

    @field_validator("answer", mode="before")
    @classmethod
    def normalize_answer(cls, value: object) -> str:
        return normalize_chat_answer(value)


class ChatEvidenceItem(_StrictModel):
    display_order: int = Field(ge=1)
    source_code: str = Field(min_length=1, max_length=100)
    source_version: str = Field(min_length=1, max_length=200)
    locator: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1)
    chunk_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("chunk_id", mode="before")
    @classmethod
    def validate_chunk_id(cls, value: object) -> str | None:
        if value is None:
            return None
        return _normalize_trimmed_text(value, field_name="chunk_id")


class ChatGenerationInput(_StrictModel):
    question: str = Field(max_length=2000)
    history: list[ChatHistoryItem] = Field(default_factory=list, max_length=3)
    medications: list[ChatMedicationInput] = Field(min_length=1, max_length=30)
    evidence: list[ChatEvidenceItem] = Field(default_factory=list, max_length=5)

    @field_validator("question", mode="before")
    @classmethod
    def normalize_question(cls, value: object) -> str:
        return _normalize_question(value)

    @model_validator(mode="after")
    def validate_history_total_length(self) -> "ChatGenerationInput":
        if sum(len(item.question) + len(item.answer) for item in self.history) > 12_000:
            raise ValueError("history exceeds total character limit")
        return self


JsonDecimal = Annotated[Decimal, PlainSerializer(lambda value: str(value), return_type=str, when_used="json")]


class ChatMedicationPromptItem(_StrictModel):
    medication_name: str
    strength_text: str | None = None
    dose_value: JsonDecimal | None = None
    dose_unit: str | None = None
    frequency_per_day: int | None = None
    timing_text: str | None = None
    duration_days: int | None = None


class ChatPromptPayload(_StrictModel):
    question: str
    history: list[ChatHistoryItem]
    medications: list[ChatMedicationPromptItem]
    evidence: list[ChatEvidenceItem] | None = None


class ProviderChatResponse(_StrictGeneratedModel):
    content: str
    model_name: str
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class ChatGenerationResult(_StrictGeneratedModel):
    content: str = Field(min_length=1, max_length=10_000)
    model_name: str = Field(min_length=1, max_length=100)
    prompt_version: str

    @field_validator("content", mode="before")
    @classmethod
    def normalize_content(cls, value: object) -> str:
        normalized = _normalize_generated_content(value)
        if not normalized:
            raise ValueError("content must not be blank")
        return normalized


ChatClaimKind = Literal["critical", "non_critical"]
ChunkIdStr = Annotated[str, Field(min_length=1, max_length=128)]


def _validate_unique_claim_keys(claims: list["ChatGeneratedClaim"]) -> list["ChatGeneratedClaim"]:
    seen_keys: set[str] = set()
    for claim in claims:
        if claim.claim_key in seen_keys:
            raise ValueError("claims must not contain duplicate claim_key values")
        seen_keys.add(claim.claim_key)
    return claims


class ChatGeneratedClaim(_StrictGeneratedModel):
    claim_key: str = Field(min_length=1, max_length=64)
    claim_text: str = Field(min_length=1, max_length=2000)
    claim_kind: ChatClaimKind
    evidence_chunk_ids: list[ChunkIdStr] = Field(default_factory=list, max_length=10)

    @field_validator("claim_key", mode="before")
    @classmethod
    def validate_claim_key(cls, value: object) -> str:
        return _normalize_trimmed_text(value, field_name="claim_key")

    @field_validator("claim_text", mode="before")
    @classmethod
    def validate_claim_text(cls, value: object) -> str:
        return _normalize_trimmed_text(value, field_name="claim_text")

    @field_validator("evidence_chunk_ids", mode="before")
    @classmethod
    def validate_evidence_chunk_ids(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("evidence_chunk_ids must be a list")
        seen: set[str] = set()
        normalized_ids: list[str] = []
        for item in value:
            normalized = _normalize_trimmed_text(item, field_name="evidence_chunk_id")
            if normalized in seen:
                raise ValueError("evidence_chunk_ids must not contain duplicates")
            seen.add(normalized)
            normalized_ids.append(normalized)
        return normalized_ids


class ChatStructuredAnswerPayload(_StrictGeneratedModel):
    answer: str = Field(min_length=1, max_length=10_000)
    claims: list[ChatGeneratedClaim] = Field(default_factory=list, max_length=30)

    @field_validator("answer", mode="before")
    @classmethod
    def validate_answer(cls, value: object) -> str:
        return normalize_chat_answer(value)

    @field_validator("claims")
    @classmethod
    def validate_claims_unique(cls, value: list[ChatGeneratedClaim]) -> list[ChatGeneratedClaim]:
        return _validate_unique_claim_keys(value)


class ChatStructuredGenerationResult(_StrictGeneratedModel):
    content: str = Field(min_length=1, max_length=10_000)
    claims: list[ChatGeneratedClaim] = Field(default_factory=list, max_length=30)
    model_name: str = Field(min_length=1, max_length=100)
    prompt_version: str

    @field_validator("content", mode="before")
    @classmethod
    def normalize_content(cls, value: object) -> str:
        return normalize_chat_answer(value)

    @field_validator("claims")
    @classmethod
    def validate_claims_unique(cls, value: list[ChatGeneratedClaim]) -> list[ChatGeneratedClaim]:
        return _validate_unique_claim_keys(value)
