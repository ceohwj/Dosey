import asyncio
import math

from pydantic import ValidationError

from app.services.chat_ai.client import ChatProvider
from app.services.chat_ai.exceptions import (
    ChatGenerationConfigurationError,
    ChatGenerationInvalidResponseError,
    ChatGenerationTimeoutError,
)
from app.services.chat_ai.prompt import (
    CHAT_SYSTEM_INSTRUCTIONS,
    CLAIM_CITATION_PROMPT_VERSION,
    CLAIM_CITATION_SYSTEM_INSTRUCTIONS,
    CLOSED_DEMO_EVIDENCE_PROMPT_VERSION,
    CLOSED_DEMO_EVIDENCE_SYSTEM_INSTRUCTIONS,
    PROMPT_VERSION,
)
from app.services.chat_ai.schemas import (
    ChatEvidenceItem,
    ChatGenerationInput,
    ChatGenerationResult,
    ChatMedicationPromptItem,
    ChatPromptPayload,
    ChatStructuredAnswerPayload,
    ChatStructuredGenerationResult,
    ProviderChatResponse,
    normalize_chat_answer,
)

MAX_OUTPUT_TOKENS = 800


class ChatGenerator:
    def __init__(self, *, provider: ChatProvider, model: str, timeout_seconds: float) -> None:
        if not model.strip() or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ChatGenerationConfigurationError("Chat generation configuration is invalid")
        self._provider = provider
        self._model = model
        self._timeout_seconds = timeout_seconds

    async def generate(self, chat_input: ChatGenerationInput) -> ChatGenerationResult:
        input_json = self._build_provider_input(chat_input)
        evidence_enabled = bool(chat_input.evidence)
        provider_response = await self._call_provider(
            instructions=(CLOSED_DEMO_EVIDENCE_SYSTEM_INSTRUCTIONS if evidence_enabled else CHAT_SYSTEM_INSTRUCTIONS),
            input_json=input_json,
        )

        content = provider_response.content.strip()
        if not content or len(content) > 10_000:
            raise ChatGenerationInvalidResponseError("Chat provider result is invalid")
        try:
            content = normalize_chat_answer(content)
        except ValueError as error:
            raise ChatGenerationInvalidResponseError("Chat provider result is invalid") from error

        return ChatGenerationResult(
            content=content,
            model_name=provider_response.model_name,
            prompt_version=CLOSED_DEMO_EVIDENCE_PROMPT_VERSION if evidence_enabled else PROMPT_VERSION,
        )

    async def generate_structured_claims(self, chat_input: ChatGenerationInput) -> ChatStructuredGenerationResult:
        """Generate a strict JSON answer with claim-level evidence chunk citations."""
        evidence_chunk_ids = self._extract_evidence_chunk_ids(chat_input.evidence)
        input_json = self._build_provider_input(chat_input)
        provider_response = await self._call_provider(
            instructions=CLAIM_CITATION_SYSTEM_INSTRUCTIONS,
            input_json=input_json,
        )

        try:
            parsed_payload = ChatStructuredAnswerPayload.model_validate_json(provider_response.content)
        except (ValidationError, ValueError) as error:
            raise ChatGenerationInvalidResponseError("Chat provider structured response is invalid") from error

        for claim in parsed_payload.claims:
            for chunk_id in claim.evidence_chunk_ids:
                if chunk_id not in evidence_chunk_ids:
                    raise ChatGenerationInvalidResponseError("Chat provider cited an unknown evidence chunk_id")

        try:
            return ChatStructuredGenerationResult(
                content=parsed_payload.answer,
                claims=parsed_payload.claims,
                model_name=provider_response.model_name,
                prompt_version=CLAIM_CITATION_PROMPT_VERSION,
            )
        except ValidationError as error:
            raise ChatGenerationInvalidResponseError("Chat provider structured result is invalid") from error

    async def _call_provider(self, *, instructions: str, input_json: str) -> ProviderChatResponse:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                provider_response = await self._provider.generate(
                    model=self._model,
                    instructions=instructions,
                    input_json=input_json,
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                )
        except TimeoutError as error:
            raise ChatGenerationTimeoutError("Chat provider call timed out") from error

        model_name = provider_response.model_name
        if not model_name.strip() or len(model_name) > 100:
            raise ChatGenerationInvalidResponseError("Chat provider result is invalid")
        return provider_response

    @staticmethod
    def _extract_evidence_chunk_ids(evidence: list[ChatEvidenceItem]) -> set[str]:
        if not evidence:
            raise ChatGenerationInvalidResponseError("Structured claim generation requires non-empty evidence")
        chunk_ids: set[str] = set()
        for item in evidence:
            if item.chunk_id is None or not item.chunk_id.strip():
                raise ChatGenerationInvalidResponseError("Every evidence item must have a chunk_id")
            if item.chunk_id in chunk_ids:
                raise ChatGenerationInvalidResponseError("Evidence chunk_ids must be unique")
            chunk_ids.add(item.chunk_id)
        return chunk_ids

    @staticmethod
    def _build_provider_input(chat_input: ChatGenerationInput) -> str:
        medication_items: list[ChatMedicationPromptItem] = []
        for medication in chat_input.medications:
            has_complete_dose = medication.dose_value is not None and medication.dose_unit is not None
            medication_items.append(
                ChatMedicationPromptItem(
                    medication_name=medication.medication_name,
                    strength_text=medication.strength_text,
                    dose_value=(medication.dose_value if has_complete_dose else None),
                    dose_unit=(medication.dose_unit if has_complete_dose else None),
                    frequency_per_day=medication.frequency_per_day,
                    timing_text=medication.timing_text,
                    duration_days=medication.duration_days,
                )
            )
        payload = ChatPromptPayload(
            question=chat_input.question,
            history=chat_input.history,
            medications=medication_items,
            evidence=(
                [
                    ChatEvidenceItem(
                        display_order=evidence.display_order,
                        source_code=evidence.source_code,
                        source_version=evidence.source_version,
                        locator=evidence.locator,
                        content=evidence.content,
                        chunk_id=evidence.chunk_id,
                    )
                    for evidence in chat_input.evidence
                ]
                or None
            ),
        )
        return payload.model_dump_json(exclude_none=True)
