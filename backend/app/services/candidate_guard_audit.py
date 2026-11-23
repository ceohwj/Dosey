"""Internal synthetic Candidate audit transaction boundary.

The caller owns the outer session and invokes ``write_pass`` inside its domain
mutation/savepoint. A typed policy denial is caught only after the existing
idempotency service has rolled its savepoint back and checked for a winner.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Protocol, TypeVar
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError, ErrorDetail, ErrorResponse
from app.repositories.medication_candidate_repository import MedicationCandidateRepository
from app.repositories.rag_candidate_guard_audit_repository import RagCandidateGuardAuditRepository
from app.repositories.rag_request_authority_repository import (
    RagRequestAuthorityRepository,
    RequestGuardAuthorityRecord,
    RequestMemberDecisionRecord,
    RequestSourceDecisionRecord,
)
from app.repositories.rag_request_guard_runtime_binding_repository import RagRequestGuardRuntimeBindingRepository
from rag_runtime.candidate_guard_audit import (
    CandidateGuardAuditObservation,
    CandidateGuardAuditValidationError,
    candidate_guard_audit_projection,
    canonical_candidate_guard_proof_hash,
)
from rag_runtime.request_authority import (
    RequestAuthorityDecisionStage,
    compute_request_guard_authority_ref,
    compute_request_member_decision_authority_ref,
    compute_request_source_decision_authority_ref,
)
from rag_runtime.request_guard_runtime_binding import (
    RequestGuardRuntimeBindingObservation,
    compute_request_guard_runtime_binding_ref,
)

T = TypeVar("T")


class _ArtifactRef(Protocol):
    @property
    def artifact_code(self) -> str: ...
    @property
    def version(self) -> str: ...
    @property
    def content_sha256(self) -> str: ...


def _ref_dict(ref: _ArtifactRef) -> dict[str, str]:
    return {"artifact_code": ref.artifact_code, "version": ref.version, "content_sha256": ref.content_sha256}


def _observation_from_projection(projection: dict) -> CandidateGuardAuditObservation:
    values = dict(projection)
    values.pop("projection_version")
    for key in (
        "request_guard_decision_id",
        "user_id",
        "prescription_version_id",
        "prescription_version_medication_id",
        "search_id",
        "candidate_search_result_id",
        "runtime_environment_id",
        "candidate_index_version_id",
    ):
        if values[key] is not None:
            values[key] = UUID(values[key])
    for key in ("checked_at", "valid_until"):
        if values[key] is not None:
            values[key] = datetime.fromisoformat(values[key].replace("Z", "+00:00"))
    values["reason_codes"] = tuple(values["reason_codes"])
    return CandidateGuardAuditObservation(**values)


@dataclass(frozen=True, slots=True, init=False)
class CandidateGuardAuditEnvelope:
    """Value snapshot of the complete evaluator result; no live ORM or mutable JSON."""

    guard: RequestGuardAuthorityRecord
    sources: tuple[RequestSourceDecisionRecord, ...]
    members: tuple[RequestMemberDecisionRecord, ...]
    binding: RequestGuardRuntimeBindingObservation
    _observation_bytes: bytes
    proof_bytes: bytes

    def __init__(  # noqa: C901 - complete transitive envelope validation is one boundary
        self,
        *,
        guard: RequestGuardAuthorityRecord,
        sources: tuple[RequestSourceDecisionRecord, ...],
        members: tuple[RequestMemberDecisionRecord, ...],
        binding: RequestGuardRuntimeBindingObservation,
        observation: CandidateGuardAuditObservation,
        proof_manifest: dict,
    ) -> None:
        if type(guard) is not RequestGuardAuthorityRecord or type(sources) is not tuple or type(members) is not tuple:
            raise CandidateGuardAuditValidationError("complete immutable #713 records required")
        if any(type(x) is not RequestSourceDecisionRecord for x in sources) or any(
            type(x) is not RequestMemberDecisionRecord for x in members
        ):
            raise CandidateGuardAuditValidationError("invalid #713 decision record")
        if type(binding) is not RequestGuardRuntimeBindingObservation:
            raise CandidateGuardAuditValidationError("#806 binding required")
        projection = candidate_guard_audit_projection(observation)
        proof_hash = canonical_candidate_guard_proof_hash(proof_manifest)
        proof_bytes = json.dumps(
            proof_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        observation_bytes = json.dumps(
            projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        guard_ref = compute_request_guard_authority_ref(
            user_id=guard.user_id,
            request_operation_code=guard.request_operation_code,
            decision_stage=guard.decision_stage,
        )
        binding_ref = compute_request_guard_runtime_binding_ref(binding)
        if (
            guard.decision_stage is not RequestAuthorityDecisionStage.REQUEST
            or binding.legacy_request_authority_ref != guard_ref
            or binding.request_guard_decision_id != observation.request_guard_decision_id
            or binding.user_id != guard.user_id
            or observation.user_id != guard.user_id
            or binding.request_operation_code != guard.request_operation_code
            or observation.request_operation_code != guard.request_operation_code
            or binding.actual_decision_outcome.value != observation.outcome
            or observation.proof_manifest_sha256 != proof_hash
            or observation.runtime_binding_ref != _ref_dict(binding_ref)
            or proof_manifest["runtime_binding_ref"] != _ref_dict(binding_ref)
            or proof_manifest["scope_manifest_hash"] != binding.scope_manifest_hash
            or proof_manifest["aggregate_outcome"] != observation.outcome
            or proof_manifest["resolver_policy_ref"]["content_sha256"] != observation.resolver_policy_sha256
            or proof_manifest["retention_policy_ref"]["content_sha256"] != observation.retention_policy_sha256
        ):
            raise CandidateGuardAuditValidationError("#713/#806/Candidate identity or aggregate mismatch")
        source_refs = {}
        for source in sources:
            if (
                source.request_guard_ref != guard_ref
                or source.user_id != guard.user_id
                or source.request_operation_code != guard.request_operation_code
                or source.decision_stage is not RequestAuthorityDecisionStage.REQUEST
            ):
                raise CandidateGuardAuditValidationError("Source decision parent mismatch")
            ref = compute_request_source_decision_authority_ref(
                **{f.name: getattr(source, f.name) for f in fields(source)}
            )
            if ref in source_refs:
                raise CandidateGuardAuditValidationError("duplicate Source decision")
            source_refs[ref] = source
        member_refs = {}
        for member in members:
            if (
                member.request_guard_ref != guard_ref
                or member.user_id != guard.user_id
                or member.request_operation_code != guard.request_operation_code
                or member.decision_stage is not RequestAuthorityDecisionStage.REQUEST
            ):
                raise CandidateGuardAuditValidationError("Member decision parent mismatch")
            ref = compute_request_member_decision_authority_ref(
                **{f.name: getattr(member, f.name) for f in fields(member)}
            )
            if ref in member_refs:
                raise CandidateGuardAuditValidationError("duplicate Member decision")
            member_refs[ref] = member
        target_source_refs = set()
        target_member_refs = set()
        for target in proof_manifest["targets"]:
            source_ref = target["source_decision_ref"]
            matching = [ref for ref in source_refs if _ref_dict(ref) == source_ref]
            if len(matching) != 1:
                raise CandidateGuardAuditValidationError("proof Source decision set incomplete")
            ref = matching[0]
            source = source_refs[ref]
            target_source_refs.add(ref)
            if (
                source.source_snapshot_id != UUID(target["source"]["snapshot_id"])
                or source.source_code != target["source"]["source_code"]
                or source.source_version != target["source"]["source_version"]
                or source.actual_decision_outcome.value != target["outcome"]
            ):
                raise CandidateGuardAuditValidationError("proof Source facts mismatch")
            for item in target["member_decisions"]:
                matching_members = [ref for ref in member_refs if _ref_dict(ref) == item["member_decision_ref"]]
                if len(matching_members) != 1:
                    raise CandidateGuardAuditValidationError("proof Member decision set incomplete")
                member_ref = matching_members[0]
                member = member_refs[member_ref]
                target_member_refs.add(member_ref)
                if (
                    member.source_snapshot_id != source.source_snapshot_id
                    or member.source_snapshot_member_id != UUID(item["source_snapshot_member_id"])
                    or member.actual_decision_outcome.value != item["outcome"]
                ):
                    raise CandidateGuardAuditValidationError("proof Member facts mismatch")
        if set(source_refs) != target_source_refs or set(member_refs) != target_member_refs:
            raise CandidateGuardAuditValidationError("extra or missing #713 decisions")
        object.__setattr__(self, "guard", guard)
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "members", members)
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "_observation_bytes", observation_bytes)
        object.__setattr__(self, "proof_bytes", proof_bytes)

    @property
    def observation(self) -> CandidateGuardAuditObservation:
        return _observation_from_projection(json.loads(self._observation_bytes))

    @property
    def proof_manifest(self) -> dict:
        return json.loads(self.proof_bytes)


class CandidateGuardPolicyDenied(Exception):  # noqa: N818 - frozen contract name
    """Only a complete evaluator denial can cross the idempotency savepoint."""

    def __init__(self, envelope: CandidateGuardAuditEnvelope, *, stale: bool = False) -> None:
        if type(envelope) is not CandidateGuardAuditEnvelope or envelope.observation.outcome != "FAIL":
            raise CandidateGuardAuditValidationError("typed denial requires a complete FAIL envelope")
        self.envelope = envelope
        self.stale = stale
        super().__init__("Candidate policy denied")


@dataclass(frozen=True, slots=True)
class CandidateAuditStopResult:
    status_code: int
    code: str
    message: str
    detail_reason: str

    def error_response(self, *, trace_id: str) -> ErrorResponse:
        return ErrorResponse(
            code=self.code,
            message=self.message,
            details=[ErrorDetail(field="medication_candidate", reason=self.detail_reason)],
            trace_id=trace_id,
        )


class CandidateGuardAuditCoordinator:
    """Explicit internal factory target. Never creates or commits a session."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self._authority = RagRequestAuthorityRepository(session)
        self._binding = RagRequestGuardRuntimeBindingRepository(session)
        self._audit = RagCandidateGuardAuditRepository(session)
        self._candidate = MedicationCandidateRepository(session)

    async def write_pass(self, envelope: CandidateGuardAuditEnvelope) -> str:
        if envelope.observation.outcome != "PASS":
            raise CandidateGuardAuditValidationError("PASS writer requires PASS envelope")
        return await self._write(envelope)

    async def _write(self, envelope: CandidateGuardAuditEnvelope) -> str:
        if type(envelope) is not CandidateGuardAuditEnvelope:
            raise CandidateGuardAuditValidationError("complete audit envelope required")
        async with self.session.begin_nested():
            guard_ref = await self._authority.record_request_guard_authority(envelope.guard)
            if guard_ref != envelope.binding.legacy_request_authority_ref:
                raise CandidateGuardAuditValidationError("#713 Guard persisted ref mismatch")
            for source in envelope.sources:
                await self._authority.record_request_source_decision(source)
            for member in envelope.members:
                await self._authority.record_request_member_decision(member)
            binding_ref = await self._binding.record(envelope.binding)
            if _ref_dict(binding_ref) != envelope.observation.runtime_binding_ref:
                raise CandidateGuardAuditValidationError("#806 persisted ref mismatch")
            result = await self._audit.record(envelope.observation, envelope.proof_manifest)
            await self.session.flush()
            return result

    async def _revalidate_denial_parent(self, envelope: CandidateGuardAuditEnvelope) -> None:
        observation = envelope.observation
        medication = await self._candidate.get_medication_for_candidate_search_owned(
            prescription_version_medication_id=observation.prescription_version_medication_id,
            user_id=observation.user_id,
        )
        if medication is None or medication.prescription_version_id != observation.prescription_version_id:
            raise ApiError(
                status_code=404, code="PRESCRIPTION_MEDICATION_NOT_FOUND", message="처방 약제를 찾을 수 없습니다."
            )
        if observation.search_id is not None:
            selection = None
            if observation.candidate_search_result_id is not None:
                selection = await self._candidate.get_result_selection_for_update_owned(
                    candidate_search_result_id=observation.candidate_search_result_id, user_id=observation.user_id
                )
            if (
                selection is None
                or selection.search.id != observation.search_id
                or selection.search.prescription_version_medication_id != medication.id
            ):
                raise ApiError(
                    status_code=404,
                    code="CANDIDATE_SEARCH_NOT_FOUND",
                    message="약품 후보 검색 결과를 찾을 수 없습니다.",
                )

    async def execute_mutation(self, operation: Callable[[], Awaitable[T]]) -> T | CandidateAuditStopResult:
        """Own the domain savepoint, then persist only a denial that survives rollback.

        The existing idempotency service may open an inner savepoint and return a
        winning receipt. That return exits this savepoint normally without audit.
        An arbitrary callback cannot leave domain writes behind by raising denial.
        """
        try:
            async with self.session.begin_nested():
                return await operation()
        except CandidateGuardPolicyDenied as denial:
            await self._revalidate_denial_parent(denial.envelope)
            await self._write(denial.envelope)
            if denial.stale:
                return CandidateAuditStopResult(
                    409,
                    "IDENTIFICATION_CONTEXT_STALE",
                    "약품 식별 기준이 변경되었습니다. 새 검색이 필요합니다.",
                    "CONTEXT_CHANGED",
                )
            return CandidateAuditStopResult(
                503,
                "CANDIDATE_RESOLVER_UNAVAILABLE",
                "현재 약품 후보 검색 기준을 확인할 수 없습니다.",
                "SOURCE_AUTHORITY_UNAVAILABLE",
            )
