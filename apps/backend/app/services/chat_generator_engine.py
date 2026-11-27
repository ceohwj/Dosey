import asyncio
import hashlib
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from app.core.chat_citation_retrieval import (
    ChatCitationRetrievalError,
    ChatEvidenceRetrieverProtocol,
    RevealedTextProtocol,
)
from app.core.closed_demo_retrieval import (
    ClosedDemoRetrievalExecutionError,
    ClosedDemoRetrievalService,
)
from app.services.chat_ai import (
    ChatEngine,
    ChatGenerationFailedError,
    ChatHistoryPair,
    ChatMedicationInput,
    ChatReplyInput,
    ChatReplyOutput,
    ChatServiceUnavailableError,
    ChatTimeoutError,
    ChatVerifiedCitation,
)
from app.services.chat_ai.client import ChatProvider
from app.services.chat_ai.exceptions import (
    ChatGenerationError,
    ChatGenerationTimeoutError,
    ChatGenerationUnavailableError,
)
from app.services.chat_ai.generator import ChatGenerator
from app.services.chat_ai.schemas import ChatEvidenceItem, ChatGenerationInput
from app.services.chat_ai.schemas import ChatHistoryItem as GenerationHistoryItem
from app.services.chat_ai.schemas import ChatMedicationInput as GenerationMedicationInput
from rag_runtime.chat_claim_citation_validator import (
    ClaimCandidate,
    ClaimEvidenceRecord,
    CriticalClaimContradictionError,
    NliScorerUnavailableError,
    RulePlusNliClaimValidator,
)


class _RetrievedEvidenceLike(Protocol):
    @property
    def display_order(self) -> int: ...

    @property
    def source_code(self) -> str: ...

    @property
    def source_version(self) -> str: ...

    @property
    def locator(self) -> str: ...

    @property
    def content(self) -> RevealedTextProtocol: ...


class ChatGeneratorEngine(ChatEngine):
    """Runtime Chat engine with optional opt-in `rule_plus_nli` claim verification.

    Note: The citation-validated path requires persisted app-DB `knowledge_chunk_id`
    UUIDs on retrieved evidence items and stays disabled (`citation_validator=None`)
    on default `ClosedDemoRetrievalService` / `get_chat_engine` wiring until
    production chunk-ID unification and `ai` runtime packaging are enabled.
    """

    def __init__(
        self,
        *,
        provider: ChatProvider,
        model: str,
        timeout_seconds: float,
        closed_demo_retriever: ClosedDemoRetrievalService | ChatEvidenceRetrieverProtocol | None = None,
        citation_validator: RulePlusNliClaimValidator | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._closed_demo_retriever = closed_demo_retriever
        self._citation_validator = citation_validator

    async def reply(self, chat_input: ChatReplyInput) -> ChatReplyOutput:
        mapped_error: Exception | None = None
        try:
            generator = ChatGenerator(
                provider=self._provider,
                model=self._model,
                timeout_seconds=self._timeout_seconds,
            )
            generation_input = ChatGenerationInput(
                question=chat_input.content,
                history=[self._to_generation_history(pair) for pair in chat_input.history],
                medications=[self._to_generation_medication(medication) for medication in chat_input.medications],
            )
            if self._closed_demo_retriever is not None and self._citation_validator is not None:
                return await self._reply_with_citation_validation(
                    generator=generator,
                    generation_input=generation_input,
                )

            if self._closed_demo_retriever is not None:
                evidence = await self._closed_demo_retriever.retrieve(generation_input.question)
                generation_input = generation_input.model_copy(
                    update={
                        "evidence": [
                            ChatEvidenceItem(
                                display_order=item.display_order,
                                source_code=item.source_code,
                                source_version=item.source_version,
                                locator=item.locator,
                                content=item.content.reveal(),
                            )
                            for item in evidence
                        ]
                    }
                )
            result = await generator.generate(generation_input)
        except (ChatGenerationTimeoutError, TimeoutError):
            mapped_error = ChatTimeoutError("챗봇 응답 대기 시간이 초과됐습니다.")
        except ChatGenerationUnavailableError:
            mapped_error = ChatServiceUnavailableError("챗봇 LLM 호출에 실패했습니다.")
        except (
            ChatCitationRetrievalError,
            ChatGenerationError,
            ClosedDemoRetrievalExecutionError,
            CriticalClaimContradictionError,
            NliScorerUnavailableError,
            ValidationError,
            ValueError,
        ):
            mapped_error = ChatGenerationFailedError("챗봇 응답 생성 처리 중 오류가 발생했습니다.")

        if mapped_error is not None:
            raise mapped_error

        return ChatReplyOutput(
            content=result.content,
            model_name=result.model_name,
            prompt_version=result.prompt_version,
        )

    async def _reply_with_citation_validation(
        self,
        *,
        generator: ChatGenerator,
        generation_input: ChatGenerationInput,
    ) -> ChatReplyOutput:
        assert self._closed_demo_retriever is not None
        assert self._citation_validator is not None
        async with asyncio.timeout(self._timeout_seconds):
            evidence = await self._closed_demo_retriever.retrieve(generation_input.question)
            evidence_items, evidence_records = self._build_evidence_inputs(evidence)
            structured_input = generation_input.model_copy(update={"evidence": evidence_items})
            structured = await generator.generate_structured_claims(structured_input)
            claim_candidates = [
                ClaimCandidate(
                    claim_key=claim.claim_key,
                    claim_text=claim.claim_text,
                    claim_kind=claim.claim_kind,
                    evidence_chunk_ids=tuple(claim.evidence_chunk_ids),
                )
                for claim in structured.claims
            ]
            validated_records = await self._citation_validator.validate_async(
                claims=claim_candidates,
                evidence=evidence_records,
                answer_text=structured.content,
            )

        verified_citations = [
            ChatVerifiedCitation(
                claim_key=record.claim_key,
                claim_text=record.claim_text,
                claim_kind=record.claim_kind,
                verdict=record.verdict,
                source_code=record.source_code,
                source_version=record.source_version,
                locator=record.locator,
                display_order=record.display_order,
                knowledge_chunk_id=record.knowledge_chunk_id,
                content_sha256=record.content_sha256,
                cited_text=record.cited_text,
            )
            for record in validated_records
        ]
        return ChatReplyOutput(
            content=structured.content,
            model_name=structured.model_name,
            prompt_version=structured.prompt_version,
            citations=verified_citations,
        )

    @staticmethod
    def _build_evidence_inputs(
        evidence: Sequence[_RetrievedEvidenceLike],
    ) -> tuple[list[ChatEvidenceItem], list[ClaimEvidenceRecord]]:
        evidence_items: list[ChatEvidenceItem] = []
        evidence_records: list[ClaimEvidenceRecord] = []
        for item in evidence:
            raw_chunk_id = getattr(item, "knowledge_chunk_id", None)
            if isinstance(raw_chunk_id, UUID):
                knowledge_chunk_id = raw_chunk_id
            elif isinstance(raw_chunk_id, str):
                knowledge_chunk_id = UUID(raw_chunk_id)
            else:
                raise ValueError(
                    "Citation validation requires persisted knowledge_chunk_id UUID on every evidence item"
                )
            chunk_id = str(knowledge_chunk_id)
            content_text = item.content.reveal()
            content_sha256 = hashlib.sha256(content_text.encode("utf-8")).hexdigest()
            evidence_items.append(
                ChatEvidenceItem(
                    display_order=item.display_order,
                    source_code=item.source_code,
                    source_version=item.source_version,
                    locator=item.locator,
                    content=content_text,
                    chunk_id=chunk_id,
                )
            )
            evidence_records.append(
                ClaimEvidenceRecord(
                    chunk_id=chunk_id,
                    knowledge_chunk_id=knowledge_chunk_id,
                    source_code=item.source_code,
                    source_version=item.source_version,
                    locator=item.locator,
                    content=content_text,
                    content_sha256=content_sha256,
                )
            )
        return evidence_items, evidence_records

    @staticmethod
    def _to_generation_medication(medication: ChatMedicationInput) -> GenerationMedicationInput:
        return GenerationMedicationInput(
            medication_name=medication.medication_name,
            strength_text=medication.strength_text,
            dose_value=medication.dose_value,
            dose_unit=medication.dose_unit,
            frequency_per_day=medication.frequency_per_day,
            timing_text=medication.timing_text,
            duration_days=medication.duration_days,
        )

    @staticmethod
    def _to_generation_history(pair: ChatHistoryPair) -> GenerationHistoryItem:
        return GenerationHistoryItem(question=pair.question, answer=pair.answer)
