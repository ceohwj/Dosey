"""Synthetic-only Candidate Search coordinator behind the closed public gate."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.core import config
from app.core.errors import ApiError, ErrorDetail
from app.dtos.medication_candidates import MedicationCandidateSearchData
from app.models.prescriptions import PrescriptionVersionMedication
from app.models.rag_candidate import MedicationCandidateSearch, MedicationCandidateSearchStatus
from app.models.users import User
from app.repositories.medication_candidate_repository import MedicationCandidateRepository
from app.repositories.rag_runtime_repository import RagRuntimeRepository
from app.services.candidate_index_binding import resolve_candidate_index_binding
from app.services.candidate_runtime_binding import CandidateRuntimeBinding, require_current_candidate_runtime
from app.services.candidate_search_resolution import resolve_synthetic_candidate_search
from app.services.candidate_source_guard import SyntheticCandidateContext, SyntheticCandidateSourceGuard
from app.services.idempotency import SyncMutationIdempotencyService
from app.services.medication_candidates import MedicationCandidateService
from app.services.medication_identification import MedicationIdentificationService


def _unavailable() -> ApiError:
    return ApiError(
        status_code=503,
        code="CANDIDATE_RESOLVER_UNAVAILABLE",
        message="현재 약품 후보 검색 기준을 확인할 수 없습니다.",
        details=[ErrorDetail(field="candidate_search", reason="SYNTHETIC_CONTEXT_UNAVAILABLE")],
    )


def _stale() -> ApiError:
    return ApiError(
        status_code=409,
        code="IDENTIFICATION_CONTEXT_STALE",
        message="약품 식별 기준이 최신 상태가 아닙니다. 새 Search를 실행해 주세요.",
        details=[ErrorDetail(field="candidate_search", reason="SEARCH_CONTEXT_CHANGED")],
    )


class _GuardedIdentificationService(MedicationIdentificationService):
    """Keep successful replay in the idempotency service ahead of current guards."""

    def __init__(
        self,
        delegate: MedicationIdentificationService,
        repository: MedicationCandidateRepository,
        coordinator: CandidateSearchService,
    ) -> None:
        super().__init__(repository)
        self._delegate = delegate
        self._coordinator = coordinator

    async def validate_candidate_runtime(
        self,
        *,
        candidate_search_result_id: UUID,
        user_id: UUID,
    ) -> None:
        self._coordinator._require_context()
        selection = await self._repository.get_result_selection_for_update_owned(
            candidate_search_result_id=candidate_search_result_id,
            user_id=user_id,
        )
        if selection is None:
            await self._delegate.validate_candidate_runtime(
                candidate_search_result_id=candidate_search_result_id,
                user_id=user_id,
            )
            return
        await self._delegate.validate_candidate_runtime(
            candidate_search_result_id=candidate_search_result_id,
            user_id=user_id,
        )
        medication = await self._repository.get_medication_for_candidate_search_owned(
            prescription_version_medication_id=selection.search.prescription_version_medication_id,
            user_id=user_id,
        )
        if medication is None:
            raise _stale()
        await self._coordinator._validate_search(
            search=selection.search,
            medication=medication,
            user_id=user_id,
            operation="select",
        )


class CandidateSearchService(MedicationCandidateService):
    """Internal fixture producer; the ordinary request DI has no synthetic context."""

    def __init__(
        self,
        repository: MedicationCandidateRepository,
        identification_service: MedicationIdentificationService,
        idempotency_service: SyncMutationIdempotencyService,
        *,
        context: SyntheticCandidateContext | None = None,
    ) -> None:
        super().__init__(repository, identification_service, idempotency_service)
        self._producer_identification_service = identification_service
        self._context = context
        self._identification_service = _GuardedIdentificationService(identification_service, repository, self)

    def _require_context(self) -> SyntheticCandidateContext:
        if self._context is None:
            raise _unavailable()
        return self._context

    async def _binding_and_index(self, *, expected_binding: CandidateRuntimeBinding | None = None):
        context = self._require_context()
        if expected_binding is not None and expected_binding.runtime_environment_id != context.runtime_environment_id:
            raise _stale()
        runtime_repository = RagRuntimeRepository(self._repository.session)
        binding = await require_current_candidate_runtime(
            runtime_repository,
            runtime_environment_id=context.runtime_environment_id,
            expected_binding=expected_binding,
        )
        assert binding.runtime_release_bundle_id is not None
        bundle = await runtime_repository.get_release_bundle_by_id(binding.runtime_release_bundle_id)
        if bundle is None:
            raise _unavailable()
        snapshot = await resolve_candidate_index_binding(self._repository.session, bundle=bundle)
        return binding, snapshot

    async def _validate_search(
        self,
        *,
        search: MedicationCandidateSearch,
        medication: PrescriptionVersionMedication,
        user_id: UUID,
        operation: str,
    ) -> None:
        context = self._require_context()
        expected_binding = CandidateRuntimeBinding(
            search.runtime_release_bundle_id,
            search.runtime_release_bundle_manifest_hash,
            search.runtime_environment_id,
            search.runtime_environment_revision,
        )
        if not expected_binding.complete or search.candidate_index_version_id is None:
            raise _stale()
        query_digest = context.query_digest(
            medication_name=medication.medication_name,
            strength_text=medication.strength_text,
        )
        if search.query_digest != query_digest:
            raise _stale()
        binding, snapshot = await self._binding_and_index(expected_binding=expected_binding)
        if search.candidate_index_version_id != snapshot.version.id:
            raise _stale()
        await SyntheticCandidateSourceGuard(self._repository.session, context).validate(
            binding=binding,
            snapshot=snapshot,
            medication=medication,
            user_id=user_id,
            operation=operation,
            query_digest=query_digest,
            search=search,
        )

    async def create_candidate_search(
        self,
        *,
        user: User,
        prescription_version_medication_id: UUID,
    ) -> MedicationCandidateSearchData:
        context = self._require_context()
        medication = await self._repository.get_medication_for_candidate_search_owned(
            prescription_version_medication_id=prescription_version_medication_id,
            user_id=user.id,
        )
        if medication is None:
            raise ApiError(
                status_code=404,
                code="PRESCRIPTION_MEDICATION_NOT_FOUND",
                message="처방 약제를 찾을 수 없습니다.",
                details=[ErrorDetail(field="prescription_version_medication_id", reason="NOT_FOUND")],
            )
        await self._repository.get_active_search_for_update(
            prescription_version_medication_id=prescription_version_medication_id,
            user_id=user.id,
        )
        binding, snapshot = await self._binding_and_index()
        query_digest = context.query_digest(
            medication_name=medication.medication_name,
            strength_text=medication.strength_text,
        )
        receipt = await SyntheticCandidateSourceGuard(self._repository.session, context).validate(
            binding=binding,
            snapshot=snapshot,
            medication=medication,
            user_id=user.id,
            operation="create",
            query_digest=query_digest,
        )
        assert binding.runtime_release_bundle_id is not None
        recorded = await self._producer_identification_service.record_candidate_search(
            prescription_version_medication_id=prescription_version_medication_id,
            user_id=user.id,
            query_digest=query_digest,
            runtime_release_bundle_id=binding.runtime_release_bundle_id,
            candidate_index_version_id=snapshot.version.id,
            expires_at=receipt.expires_at,
            runtime_environment_id=context.runtime_environment_id,
        )
        search = recorded.search
        if search.status == MedicationCandidateSearchStatus.READY:
            await self._validate_search(search=search, medication=medication, user_id=user.id, operation="read")
        else:
            if search.status != MedicationCandidateSearchStatus.RUNNING:
                raise _stale()
            resolution = await resolve_synthetic_candidate_search(
                self._repository.session,
                snapshot=snapshot,
                medication_name=medication.medication_name,
                strength_text=medication.strength_text,
                policy=context.resolver_policy,
            )
            await self._validate_search(search=search, medication=medication, user_id=user.id, operation="create")
            await self._producer_identification_service.finalize_candidate_search(
                search_id=search.id,
                user_id=user.id,
                status=resolution.status,
                results=resolution.results,
                status_reason=resolution.status_reason,
            )
        return await super().get_candidate_search(
            user=user,
            prescription_version_medication_id=prescription_version_medication_id,
        )

    async def get_candidate_search(
        self,
        *,
        user: User,
        prescription_version_medication_id: UUID,
    ) -> MedicationCandidateSearchData:
        result = await super().get_candidate_search(
            user=user,
            prescription_version_medication_id=prescription_version_medication_id,
        )
        if result.status != MedicationCandidateSearchStatus.READY:
            return result
        self._require_context()
        medication = await self._repository.get_medication_for_candidate_search_owned(
            prescription_version_medication_id=prescription_version_medication_id,
            user_id=user.id,
        )
        active_search = await self._repository.get_active_search_for_update(
            prescription_version_medication_id=prescription_version_medication_id,
            user_id=user.id,
        )
        if medication is None or active_search is None or active_search.id != result.search_id:
            raise _stale()
        if active_search.expires_at is not None and active_search.expires_at <= datetime.now(config.TIMEZONE):
            return await super().get_candidate_search(
                user=user,
                prescription_version_medication_id=prescription_version_medication_id,
            )
        await self._validate_search(search=active_search, medication=medication, user_id=user.id, operation="read")
        return result
