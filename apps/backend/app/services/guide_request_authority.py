"""Issue one evaluated REQUEST authority for an exact Sync Guide runtime."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.guides import Guide
from app.models.rag_runtime import RagRuntimeBundleStatus, RagRuntimeEnvironmentStatus
from app.models.users import User
from app.repositories.guide_repository import GuideRepository
from app.repositories.rag_request_authority_repository import RagRequestAuthorityRepository, RequestGuardAuthorityRecord
from app.repositories.rag_request_guard_runtime_binding_repository import RagRequestGuardRuntimeBindingRepository
from app.repositories.rag_runtime_repository import RagRuntimeRepository
from app.services.guide_intake import GuideRuntimeContextSnapshot
from app.services.guide_retrieval_binding import load_verified_guide_retrieval_binding_manifest
from app.services.guide_sync_runtime_lifecycle import GuideSyncRuntimePreparationError
from app.services.rag_runtime_bundle_build import verify_persisted_bundle_manifest_hash
from rag_runtime.request_authority import RequestAuthorityDecisionOutcome, RequestAuthorityDecisionStage
from rag_runtime.request_guard_runtime_binding import (
    RequestGuardRuntimeBindingObservation,
    canonical_scope_manifest_hash,
)
from rag_runtime.runtime_environment import RuntimeEnvironmentCode

_OPERATION = "GUIDE_SYNC_ANSWER"
_UNAVAILABLE = "Guide request authority is unavailable"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class GuideRequestAuthorityInput:
    user_id: UUID
    profile_id: UUID
    guide_id: UUID
    prescription_id: UUID
    prescription_version_id: UUID
    runtime_environment_id: UUID
    runtime_environment_revision: int
    runtime_release_bundle_id: UUID
    runtime_release_bundle_manifest_hash: str
    runtime_execution_manifest_id: UUID
    runtime_execution_manifest_hash: str
    guide_retrieval_binding_manifest_id: UUID
    guide_retrieval_binding_manifest_hash: str
    evaluated_at: datetime


@dataclass(frozen=True, slots=True)
class GuideRequestAuthorityEvaluation:
    outcome: RequestAuthorityDecisionOutcome
    request_scope_codes: tuple[str, ...]
    patient_context_digest: str | None = None


class GuideRequestAuthorityEvaluator(Protocol):
    async def evaluate(
        self, *, session: AsyncSession, request: GuideRequestAuthorityInput
    ) -> GuideRequestAuthorityEvaluation: ...


class GuideRequestAuthorityRejected(GuideSyncRuntimePreparationError):  # noqa: N818 - frozen CR-A interface
    """A real FAIL decision was recorded in the caller's open transaction."""


class GuideRequestAuthorityIssuer:
    def __init__(
        self, session: AsyncSession, *, environment_code: str, evaluator: GuideRequestAuthorityEvaluator
    ) -> None:
        self._session = session
        self._environment_code = environment_code
        self._evaluator = evaluator

    async def resolve(self, *, user: User, guide: Guide) -> GuideRuntimeContextSnapshot:
        try:
            request = await self._read_request(user=user, guide=guide)
            evaluation, scope_hash = await self._evaluate(request)
            legacy_ref = await RagRequestAuthorityRepository(self._session).record_request_guard_authority(
                RequestGuardAuthorityRecord(
                    user_id=request.user_id,
                    request_operation_code=_OPERATION,
                    decision_stage=RequestAuthorityDecisionStage.REQUEST,
                )
            )
            observation = RequestGuardRuntimeBindingObservation(
                request_guard_decision_id=uuid4(),
                actual_decision_outcome=evaluation.outcome,
                user_id=request.user_id,
                request_operation_code=_OPERATION,
                decision_stage=RequestAuthorityDecisionStage.REQUEST,
                environment=RuntimeEnvironmentCode(self._environment_code),
                bundle_id=request.runtime_release_bundle_id,
                bundle_manifest_hash=request.runtime_release_bundle_manifest_hash,
                request_scope_codes=evaluation.request_scope_codes,
                scope_manifest_hash=scope_hash,
                legacy_request_authority_ref=legacy_ref,
            )
            ref = await RagRequestGuardRuntimeBindingRepository(self._session).record(observation)
        except Exception:
            raise GuideSyncRuntimePreparationError(_UNAVAILABLE) from None

        if evaluation.outcome is RequestAuthorityDecisionOutcome.FAIL:
            raise GuideRequestAuthorityRejected("Guide request authority was rejected")

        return GuideRuntimeContextSnapshot(
            runtime_environment_id=request.runtime_environment_id,
            runtime_environment_revision=request.runtime_environment_revision,
            runtime_release_bundle_id=request.runtime_release_bundle_id,
            runtime_release_bundle_manifest_hash=request.runtime_release_bundle_manifest_hash,
            runtime_execution_manifest_id=request.runtime_execution_manifest_id,
            runtime_execution_manifest_hash=request.runtime_execution_manifest_hash,
            guide_retrieval_binding_manifest_id=request.guide_retrieval_binding_manifest_id,
            guide_retrieval_binding_manifest_hash=request.guide_retrieval_binding_manifest_hash,
            runtime_guard_decision_ref=str(observation.request_guard_decision_id),
            request_guard_runtime_binding_ref=ref,
            patient_context_digest=evaluation.patient_context_digest,
            source_scope_manifest_hash=scope_hash,
        )

    async def revalidate(self, *, user: User, guide: Guide, snapshot: GuideRuntimeContextSnapshot) -> bool:
        try:
            if type(snapshot) is not GuideRuntimeContextSnapshot:
                return False
            request = await self._read_request(user=user, guide=guide)
            pinned = (
                request.runtime_environment_id,
                request.runtime_environment_revision,
                request.runtime_release_bundle_id,
                request.runtime_release_bundle_manifest_hash,
                request.runtime_execution_manifest_id,
                request.runtime_execution_manifest_hash,
                request.guide_retrieval_binding_manifest_id,
                request.guide_retrieval_binding_manifest_hash,
            )
            expected = (
                snapshot.runtime_environment_id,
                snapshot.runtime_environment_revision,
                snapshot.runtime_release_bundle_id,
                snapshot.runtime_release_bundle_manifest_hash,
                snapshot.runtime_execution_manifest_id,
                snapshot.runtime_execution_manifest_hash,
                snapshot.guide_retrieval_binding_manifest_id,
                snapshot.guide_retrieval_binding_manifest_hash,
            )
            if pinned != expected:
                return False
            guard = await RagRequestGuardRuntimeBindingRepository(self._session).get_exact(
                snapshot.request_guard_runtime_binding_ref
            )
            if guard is None or (
                guard.actual_decision_outcome,
                guard.user_id,
                guard.request_operation_code,
                guard.decision_stage,
                guard.environment.value,
                guard.bundle_id,
                guard.bundle_manifest_hash,
                str(guard.request_guard_decision_id),
            ) != (
                RequestAuthorityDecisionOutcome.PASS,
                request.user_id,
                _OPERATION,
                RequestAuthorityDecisionStage.REQUEST,
                self._environment_code,
                request.runtime_release_bundle_id,
                request.runtime_release_bundle_manifest_hash,
                snapshot.runtime_guard_decision_ref,
            ):
                return False
            evaluation, scope_hash = await self._evaluate(request)
            return (
                evaluation.outcome is RequestAuthorityDecisionOutcome.PASS
                and evaluation.request_scope_codes == guard.request_scope_codes
                and scope_hash == guard.scope_manifest_hash == snapshot.source_scope_manifest_hash
                and evaluation.patient_context_digest == snapshot.patient_context_digest
            )
        except Exception:
            return False

    async def _read_request(self, *, user: User, guide: Guide) -> GuideRequestAuthorityInput:
        if type(user.id) is not UUID or type(guide.id) is not UUID:
            raise ValueError("invalid request identity")
        guides = GuideRepository(self._session)
        owned = await guides.get_owned(guide_id=guide.id, user_id=user.id)
        if owned is None or (owned.id, owned.profile_id, owned.prescription_id, owned.prescription_version_id) != (
            guide.id,
            guide.profile_id,
            guide.prescription_id,
            guide.prescription_version_id,
        ):
            raise ValueError("Guide ownership mismatch")
        prescription = await guides.get_prescription_owned(prescription_id=guide.prescription_id, user_id=user.id)
        if prescription is None or (prescription.id, prescription.profile_id, prescription.active_version_id) != (
            guide.prescription_id,
            guide.profile_id,
            guide.prescription_version_id,
        ):
            raise ValueError("prescription version mismatch")

        runtime = RagRuntimeRepository(self._session)
        environment = await runtime.get_environment_by_code(self._environment_code)
        if (
            environment is None
            or environment.environment_status is not RagRuntimeEnvironmentStatus.ACTIVE
            or environment.active_bundle_id is None
            or environment.active_bundle_manifest_hash is None
            or type(environment.environment_revision) is not int
            or environment.environment_revision < 1
        ):
            raise ValueError("runtime environment unavailable")
        bundle = await runtime.get_release_bundle_by_id(environment.active_bundle_id)
        if (
            bundle is None
            or bundle.id != environment.active_bundle_id
            or bundle.bundle_manifest_hash != environment.active_bundle_manifest_hash
            or bundle.environment_code != environment.environment_code
            or bundle.bundle_status is not RagRuntimeBundleStatus.READY
            or not await verify_persisted_bundle_manifest_hash(self._session, bundle.id)
        ):
            raise ValueError("runtime bundle unavailable")
        manifest = await runtime.get_execution_manifest_by_id(bundle.execution_manifest_id)
        if manifest is None or manifest.id != bundle.execution_manifest_id:
            raise ValueError("execution manifest unavailable")
        bindings = await runtime.list_guide_retrieval_binding_manifests_for_runtime(
            runtime_release_bundle_id=bundle.id,
            runtime_release_bundle_manifest_hash=bundle.bundle_manifest_hash,
            runtime_execution_manifest_id=manifest.id,
            runtime_execution_manifest_hash=manifest.manifest_hash,
        )
        if len(bindings) != 1:
            raise ValueError("retrieval binding is not unique")
        binding = bindings[0]
        verified = await load_verified_guide_retrieval_binding_manifest(
            self._session, binding.id, binding.manifest_hash
        )
        if verified is None or (
            verified.runtime_release_bundle_id,
            verified.runtime_release_bundle_manifest_hash,
            verified.runtime_execution_manifest_id,
            verified.runtime_execution_manifest_hash,
        ) != (bundle.id, bundle.bundle_manifest_hash, manifest.id, manifest.manifest_hash):
            raise ValueError("retrieval binding unavailable")
        return GuideRequestAuthorityInput(
            user_id=user.id,
            profile_id=guide.profile_id,
            guide_id=guide.id,
            prescription_id=guide.prescription_id,
            prescription_version_id=guide.prescription_version_id,
            runtime_environment_id=environment.id,
            runtime_environment_revision=environment.environment_revision,
            runtime_release_bundle_id=bundle.id,
            runtime_release_bundle_manifest_hash=bundle.bundle_manifest_hash,
            runtime_execution_manifest_id=manifest.id,
            runtime_execution_manifest_hash=manifest.manifest_hash,
            guide_retrieval_binding_manifest_id=binding.id,
            guide_retrieval_binding_manifest_hash=binding.manifest_hash,
            evaluated_at=datetime.now(UTC),
        )

    async def _evaluate(self, request: GuideRequestAuthorityInput) -> tuple[GuideRequestAuthorityEvaluation, str]:
        if self._evaluator is None:
            raise ValueError("request evaluator unavailable")
        evaluation = await self._evaluator.evaluate(session=self._session, request=request)
        if type(evaluation) is not GuideRequestAuthorityEvaluation:
            raise ValueError("invalid request evaluation")
        if type(evaluation.outcome) is not RequestAuthorityDecisionOutcome:
            raise ValueError("invalid request outcome")
        scope_hash = canonical_scope_manifest_hash(evaluation.request_scope_codes)
        digest = evaluation.patient_context_digest
        if digest is not None and (type(digest) is not str or _DIGEST.fullmatch(digest) is None):
            raise ValueError("invalid patient context digest")
        return evaluation, scope_hash


class GuideRequestAuthorityProviderFactory:
    def __init__(self, *, environment_code: str, evaluator: GuideRequestAuthorityEvaluator) -> None:
        self._environment_code = environment_code
        self._evaluator = evaluator

    def for_session(self, session: AsyncSession) -> GuideRequestAuthorityIssuer:
        return GuideRequestAuthorityIssuer(session, environment_code=self._environment_code, evaluator=self._evaluator)
