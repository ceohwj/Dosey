"""Append-only Candidate Guard audit persistence with exact historical reads."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.prescriptions import Prescription, PrescriptionVersion, PrescriptionVersionMedication
from app.models.profiles import Profile
from app.models.rag_candidate import MedicationCandidateSearch, MedicationCandidateSearchResult
from app.models.rag_candidate_guard_audit import RagCandidateRequestGuardAudit
from app.models.rag_candidate_index import RagCandidateIndexVersion
from app.models.rag_citation_authorization import (
    RagCitationAuthorizationReceipt,
    RagCitationAuthorizationSourceDecision,
)
from app.models.rag_request_authority import (
    RagRequestGuardAuthority,
    RagRequestMemberDecision,
    RagRequestSourceDecision,
)
from app.models.rag_request_guard_runtime_binding import RagRequestGuardRuntimeBinding
from app.models.rag_runtime import (
    AiJobExecutionContext,
    RagRuntimeBundleSource,
    RagRuntimeEnvironment,
    RagRuntimeReleaseBundle,
)
from app.models.rag_source import (
    RagSource,
    RagSourceEndpoint,
    RagSourceOperation,
    RagSourceSnapshot,
    RagSourceSnapshotMember,
)
from app.models.rag_source_use_approval import RagSourceUseApproval
from app.repositories.rag_request_authority_repository import RagRequestAuthorityRepository
from app.repositories.rag_request_guard_runtime_binding_repository import RagRequestGuardRuntimeBindingRepository
from app.repositories.rag_source_capture_observation_repository import RagSourceCaptureObservationRepository
from rag_runtime.candidate_guard_audit import (
    CandidateGuardAuditObservation,
    CandidateGuardAuditValidationError,
    candidate_guard_audit_projection,
    canonical_candidate_guard_proof_hash,
    compute_candidate_guard_audit_hash,
)
from rag_runtime.request_authority import RequestAuthorityArtifactRef
from rag_runtime.request_guard_runtime_binding import RequestGuardRuntimeBindingRef


class CandidateGuardAuditConflictError(Exception):
    """The immutable request ID already has a different semantic observation."""


class CandidateGuardAuditCorruptError(Exception):
    """Persisted content or a historical authority binding was tampered with."""


class RagCandidateGuardAuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, observation: CandidateGuardAuditObservation, proof_manifest: dict) -> str:
        projection = candidate_guard_audit_projection(observation)
        proof_hash = canonical_candidate_guard_proof_hash(proof_manifest)
        if observation.proof_manifest_sha256 != proof_hash:
            raise CandidateGuardAuditValidationError("proof hash mismatch")
        digest = compute_candidate_guard_audit_hash(observation)
        existing = await self._session.get(RagCandidateRequestGuardAudit, observation.request_guard_decision_id)
        if existing is not None:
            self._assert_stored(existing)
            if (
                existing.artifact_content_sha256 != digest
                or existing.projection != projection
                or existing.proof_manifest != proof_manifest
            ):
                raise CandidateGuardAuditConflictError("request decision already has different audit content")
            await self._assert_bindings(observation, proof_manifest, at_write=False)
            return digest
        await self._assert_bindings(observation, proof_manifest, at_write=True)
        statement = (
            pg_insert(RagCandidateRequestGuardAudit)
            .values(
                request_guard_decision_id=observation.request_guard_decision_id,
                artifact_code="candidate_request_guard_audit",
                artifact_version="1.0",
                artifact_content_sha256=digest,
                proof_manifest_sha256=proof_hash,
                user_id=observation.user_id,
                prescription_version_id=observation.prescription_version_id,
                prescription_version_medication_id=observation.prescription_version_medication_id,
                request_operation_code=observation.request_operation_code,
                search_id=observation.search_id,
                candidate_search_result_id=observation.candidate_search_result_id,
                runtime_environment_id=observation.runtime_environment_id,
                environment_revision=observation.environment_revision,
                governance_revision_ref=observation.governance_revision_ref,
                safety_epoch=observation.safety_epoch,
                candidate_index_version_id=observation.candidate_index_version_id,
                candidate_index_artifact_sha256=observation.candidate_index_artifact_sha256,
                query_digest=observation.query_digest,
                outcome=observation.outcome,
                checked_at=observation.checked_at,
                valid_until=observation.valid_until,
                projection=projection,
                proof_manifest=proof_manifest,
            )
            .on_conflict_do_nothing(index_elements=["request_guard_decision_id"])
            .returning(RagCandidateRequestGuardAudit.request_guard_decision_id)
        )
        if (await self._session.execute(statement)).scalar_one_or_none() is None:
            concurrent = await self._session.get(RagCandidateRequestGuardAudit, observation.request_guard_decision_id)
            if concurrent is None:
                raise CandidateGuardAuditConflictError("concurrent audit insert failed")
            self._assert_stored(concurrent)
            if (
                concurrent.artifact_content_sha256 != digest
                or concurrent.projection != projection
                or concurrent.proof_manifest != proof_manifest
            ):
                raise CandidateGuardAuditConflictError("concurrent audit insert changed semantic content")
            await self._assert_bindings(observation, proof_manifest, at_write=False)
        return digest

    async def get_exact(
        self, decision_id: UUID, expected_hash: str
    ) -> tuple[CandidateGuardAuditObservation, dict] | None:
        if (
            type(decision_id) is not UUID
            or type(expected_hash) is not str
            or len(expected_hash) != 64
            or any(c not in "0123456789abcdef" for c in expected_hash)
        ):
            raise CandidateGuardAuditValidationError("invalid exact read identity")
        row = await self._session.get(RagCandidateRequestGuardAudit, decision_id)
        if row is None:
            return None
        observation = self._assert_stored(row)
        if row.artifact_content_sha256 != expected_hash:
            return None
        await self._assert_bindings(observation, row.proof_manifest, at_write=False)
        return observation, row.proof_manifest

    async def delete_for_account_withdrawal(self, user_id: UUID) -> int:
        """Delete Candidate extension, unshared #806, then unshared #713 rows.

        The caller owns the transaction; FK RESTRICT protects concurrent consumers.
        """
        if type(user_id) is not UUID:
            raise CandidateGuardAuditValidationError("user_id must be UUID")
        rows = (
            await self._session.scalars(
                select(RagCandidateRequestGuardAudit).where(RagCandidateRequestGuardAudit.user_id == user_id)
            )
        ).all()
        count = 0
        guard_refs: set[tuple[str, str, str]] = set()
        for row in rows:
            binding = await self._session.get(RagRequestGuardRuntimeBinding, row.request_guard_decision_id)
            if binding is None:
                raise CandidateGuardAuditCorruptError("Candidate extension lost #806 binding")
            self._assert_stored(row)
            guard_refs.add(
                (
                    binding.legacy_request_guard_artifact_code,
                    binding.legacy_request_guard_artifact_version,
                    binding.legacy_request_guard_content_sha256,
                )
            )
            await self._session.delete(row)
            await self._session.flush()
            citation = await self._session.scalar(
                select(RagCitationAuthorizationSourceDecision.id)
                .where(
                    RagCitationAuthorizationSourceDecision.request_guard_decision_id
                    == binding.request_guard_decision_id
                )
                .limit(1)
            )
            guide = await self._session.scalar(
                select(AiJobExecutionContext.id)
                .where(
                    AiJobExecutionContext.request_guard_runtime_binding_artifact_code == binding.artifact_code,
                    AiJobExecutionContext.request_guard_runtime_binding_artifact_version == binding.artifact_version,
                    AiJobExecutionContext.request_guard_runtime_binding_content_sha256
                    == binding.artifact_content_sha256,
                )
                .limit(1)
            )
            if citation is None and guide is None:
                await self._session.delete(binding)
            count += 1
        await self._session.flush()
        for code, version, digest in guard_refs:
            still_bound = await self._session.scalar(
                select(RagRequestGuardRuntimeBinding.request_guard_decision_id)
                .where(
                    RagRequestGuardRuntimeBinding.legacy_request_guard_artifact_code == code,
                    RagRequestGuardRuntimeBinding.legacy_request_guard_artifact_version == version,
                    RagRequestGuardRuntimeBinding.legacy_request_guard_content_sha256 == digest,
                )
                .limit(1)
            )
            citation_source = await self._session.scalar(
                select(RagCitationAuthorizationSourceDecision.id)
                .where(
                    RagCitationAuthorizationSourceDecision.origin_guard_artifact_code == code,
                    RagCitationAuthorizationSourceDecision.origin_guard_artifact_version == version,
                    RagCitationAuthorizationSourceDecision.origin_guard_content_sha256 == digest,
                )
                .limit(1)
            )
            citation_receipt = await self._session.scalar(
                select(RagCitationAuthorizationReceipt.id)
                .where(
                    RagCitationAuthorizationReceipt.origin_guard_artifact_code == code,
                    RagCitationAuthorizationReceipt.origin_guard_artifact_version == version,
                    RagCitationAuthorizationReceipt.origin_guard_content_sha256 == digest,
                )
                .limit(1)
            )
            if still_bound is not None or citation_source is not None or citation_receipt is not None:
                continue
            guard = await self._session.scalar(
                select(RagRequestGuardAuthority).where(
                    RagRequestGuardAuthority.artifact_code == code,
                    RagRequestGuardAuthority.artifact_version == version,
                    RagRequestGuardAuthority.artifact_content_sha256 == digest,
                )
            )
            if guard is None or guard.user_id != user_id:
                continue
            criteria = (
                ("request_guard_artifact_code", code),
                ("request_guard_artifact_version", version),
                ("request_guard_content_sha256", digest),
            )
            for model in (RagRequestMemberDecision, RagRequestSourceDecision):
                await self._session.execute(
                    delete(model).where(*(getattr(model, name) == value for name, value in criteria))
                )
            await self._session.delete(guard)
        await self._session.flush()
        return count

    def _assert_stored(self, row: RagCandidateRequestGuardAudit) -> CandidateGuardAuditObservation:  # noqa: C901
        try:
            p = row.projection
            if type(p) is not dict or p.get("projection_version") != "candidate-request-guard-audit-v1":
                raise CandidateGuardAuditValidationError("bad stored projection")

            def uuid(name):
                return UUID(p[name]) if p[name] is not None else None

            def dt(name):
                return (
                    datetime.strptime(p[name], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
                    if p[name] is not None
                    else None
                )

            observation = CandidateGuardAuditObservation(
                request_guard_decision_id=uuid("request_guard_decision_id"),
                runtime_binding_ref=p["runtime_binding_ref"],
                user_id=uuid("user_id"),
                prescription_version_id=uuid("prescription_version_id"),
                prescription_version_medication_id=uuid("prescription_version_medication_id"),
                request_operation_code=p["request_operation_code"],
                search_id=uuid("search_id"),
                candidate_search_result_id=uuid("candidate_search_result_id"),
                runtime_environment_id=uuid("runtime_environment_id"),
                environment_revision=p["environment_revision"],
                governance_revision_ref=p["governance_revision_ref"],
                safety_epoch=p["safety_epoch"],
                candidate_index_version_id=uuid("candidate_index_version_id"),
                candidate_index_artifact_sha256=p["candidate_index_artifact_sha256"],
                query_digest=p["query_digest"],
                resolver_policy_sha256=p["resolver_policy_sha256"],
                retention_policy_sha256=p["retention_policy_sha256"],
                proof_manifest_sha256=p["proof_manifest_sha256"],
                checked_at=dt("checked_at"),
                valid_until=dt("valid_until"),
                outcome=p["outcome"],
                reason_codes=tuple(p["reason_codes"]),
            )
            if candidate_guard_audit_projection(observation) != p:
                raise CandidateGuardAuditValidationError("noncanonical stored projection")
            if (
                canonical_candidate_guard_proof_hash(row.proof_manifest) != row.proof_manifest_sha256
                or observation.proof_manifest_sha256 != row.proof_manifest_sha256
            ):
                raise CandidateGuardAuditValidationError("stored proof hash mismatch")
            if (
                row.artifact_code != "candidate_request_guard_audit"
                or row.artifact_version != "1.0"
                or compute_candidate_guard_audit_hash(observation) != row.artifact_content_sha256
            ):
                raise CandidateGuardAuditValidationError("stored audit hash mismatch")
            for field in (
                "request_guard_decision_id",
                "user_id",
                "prescription_version_id",
                "prescription_version_medication_id",
                "request_operation_code",
                "search_id",
                "candidate_search_result_id",
                "runtime_environment_id",
                "environment_revision",
                "governance_revision_ref",
                "safety_epoch",
                "candidate_index_version_id",
                "candidate_index_artifact_sha256",
                "query_digest",
                "outcome",
            ):
                if getattr(row, field) != getattr(observation, field):
                    raise CandidateGuardAuditValidationError(f"stored {field} disagrees with projection")
            if row.checked_at != observation.checked_at or row.valid_until != observation.valid_until:
                raise CandidateGuardAuditValidationError("stored timestamps disagree with projection")
            return observation
        except (KeyError, TypeError, ValueError, CandidateGuardAuditValidationError) as error:
            raise CandidateGuardAuditCorruptError("Candidate audit stored content invalid") from error

    async def _assert_bindings(  # noqa: C901
        self, observation: CandidateGuardAuditObservation, manifest: dict, *, at_write: bool
    ) -> None:
        p = candidate_guard_audit_projection(observation)
        m = manifest
        if m["runtime_binding_ref"] != p["runtime_binding_ref"]:
            raise CandidateGuardAuditValidationError("proof/runtime reference mismatch")
        if (
            m["resolver_policy_ref"]["content_sha256"] != p["resolver_policy_sha256"]
            or m["retention_policy_ref"]["content_sha256"] != p["retention_policy_sha256"]
            or m["aggregate_outcome"] != p["outcome"]
        ):
            raise CandidateGuardAuditValidationError("proof/extension policy or outcome mismatch")
        binding = await self._session.get(RagRequestGuardRuntimeBinding, observation.request_guard_decision_id)
        if binding is None or (binding.artifact_code, binding.artifact_version, binding.artifact_content_sha256) != (
            p["runtime_binding_ref"]["artifact_code"],
            p["runtime_binding_ref"]["version"],
            p["runtime_binding_ref"]["content_sha256"],
        ):
            raise CandidateGuardAuditValidationError("#806 binding absent or mismatched")
        verified_binding = await RagRequestGuardRuntimeBindingRepository(self._session).get_exact(
            RequestGuardRuntimeBindingRef(**p["runtime_binding_ref"])
        )
        if verified_binding is None:
            raise CandidateGuardAuditValidationError("#806 binding digest invalid")
        if (
            binding.user_id != observation.user_id
            or binding.request_operation_code != observation.request_operation_code
            or binding.decision_stage != "REQUEST"
            or binding.actual_decision_outcome != observation.outcome
            or binding.scope_manifest_hash != m["scope_manifest_hash"]
        ):
            raise CandidateGuardAuditValidationError("#806 common facts mismatch")
        guard = await self._session.scalar(
            select(RagRequestGuardAuthority).where(
                RagRequestGuardAuthority.artifact_code == binding.legacy_request_guard_artifact_code,
                RagRequestGuardAuthority.artifact_version == binding.legacy_request_guard_artifact_version,
                RagRequestGuardAuthority.artifact_content_sha256 == binding.legacy_request_guard_content_sha256,
            )
        )
        if (
            guard is None
            or guard.user_id != observation.user_id
            or guard.request_operation_code != observation.request_operation_code
            or guard.decision_stage != "REQUEST"
        ):
            raise CandidateGuardAuditValidationError("#713 common parent mismatch")
        env = await self._session.get(RagRuntimeEnvironment, observation.runtime_environment_id)
        if (
            env is None
            or getattr(env.environment_code, "value", env.environment_code) != binding.environment_code
            or (
                at_write
                and observation.outcome == "PASS"
                and (
                    env.environment_revision != observation.environment_revision
                    or env.safety_epoch != observation.safety_epoch
                    or env.governance_revision_ref != observation.governance_revision_ref
                )
            )
        ):
            raise CandidateGuardAuditValidationError("runtime environment binding mismatch")
        index = await self._session.get(RagCandidateIndexVersion, observation.candidate_index_version_id)
        bundle = await self._session.get(RagRuntimeReleaseBundle, binding.bundle_id)
        if (
            index is None
            or bundle is None
            or bundle.bundle_manifest_hash != binding.bundle_manifest_hash
            or bundle.environment_code != binding.environment_code
            or bundle.candidate_index_ref != f"candidate-index-version/{index.id}"
            or bundle.candidate_index_version != index.index_version
            or bundle.candidate_index_manifest_hash != observation.candidate_index_artifact_sha256
        ):
            raise CandidateGuardAuditValidationError("Candidate Index/Bundle binding mismatch")
        bundle_sources = (
            await self._session.scalars(
                select(RagRuntimeBundleSource).where(RagRuntimeBundleSource.bundle_id == binding.bundle_id)
            )
        ).all()
        candidate_sources = [
            source
            for source in bundle_sources
            if getattr(source.source_purpose, "value", source.source_purpose) in ("CATALOG", "CANDIDATE_INDEX_INPUT")
        ]
        if len(candidate_sources) != len(m["targets"]):
            raise CandidateGuardAuditValidationError("proof target set is incomplete or extra")
        remaining = {
            (str(source.source_snapshot_id), getattr(source.source_purpose, "value", source.source_purpose)): source
            for source in candidate_sources
        }
        for target in m["targets"]:
            source = target["source"]
            bundle_source = remaining.pop((source["snapshot_id"], source["bundle_source_purpose"]), None)
            if bundle_source is None or (
                bundle_source.source_version,
                bundle_source.canonical_checksum,
                bundle_source.required,
                bundle_source.selected_for_operation,
            ) != (source["source_version"], source["canonical_sha256"], source["required"], source["selected"]):
                raise CandidateGuardAuditValidationError("Bundle target mismatch")
            snapshot = await self._session.get(RagSourceSnapshot, UUID(source["snapshot_id"]))
            operation = await self._session.get(RagSourceOperation, snapshot.operation_id) if snapshot else None
            endpoint = await self._session.get(RagSourceEndpoint, operation.endpoint_id) if operation else None
            source_row = await self._session.get(RagSource, endpoint.source_id) if endpoint else None
            if snapshot is None or operation is None or endpoint is None or source_row is None:
                raise CandidateGuardAuditValidationError("Source target identity missing")
            if (
                source_row.source_code,
                endpoint.endpoint_code,
                operation.operation_code,
                snapshot.source_version,
                snapshot.canonical_checksum,
            ) != (
                source["source_code"],
                source["endpoint_code"],
                source["operation_code"],
                source["source_version"],
                source["canonical_sha256"],
            ):
                raise CandidateGuardAuditValidationError("Source target identity mismatch")
            approval = target["approval"]
            approval_row = await self._session.get(RagSourceUseApproval, UUID(approval["id"]))
            if (
                approval_row is None
                or approval_row.source_snapshot_id != snapshot.id
                or approval_row.source_code != source["source_code"]
                or approval_row.source_version != source["source_version"]
                or approval_row.environment != approval["environment"]
                or approval_row.environment != binding.environment_code
                or approval_row.purpose != approval["purpose"]
                or approval_row.approval_version != approval["version"]
                or approval_row.valid_from.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ") != approval["valid_from"]
                or approval_row.expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ") != approval["expires_at"]
            ):
                raise CandidateGuardAuditValidationError("Source approval binding mismatch")
            freshness = target["freshness"]
            capture = await RagSourceCaptureObservationRepository(self._session).get_exact(
                UUID(freshness["capture_observation_id"]), freshness["capture_observation_sha256"]
            )
            if (
                capture is None
                or capture.source_snapshot_id != snapshot.id
                or capture.source_version != source["source_version"]
                or capture.canonical_checksum != source["canonical_sha256"]
                or capture.source_code != source["source_code"]
                or capture.endpoint_code != source["endpoint_code"]
                or capture.operation_code != source["operation_code"]
                or capture.capture_completed_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                != freshness["anchor_at"]
            ):
                raise CandidateGuardAuditValidationError("capture observation binding mismatch")
            ref = target["source_decision_ref"]
            decision = await self._session.scalar(
                select(RagRequestSourceDecision).where(
                    RagRequestSourceDecision.artifact_code == ref["artifact_code"],
                    RagRequestSourceDecision.artifact_version == ref["version"],
                    RagRequestSourceDecision.artifact_content_sha256 == ref["content_sha256"],
                )
            )
            if (
                decision is None
                or await RagRequestAuthorityRepository(self._session).get_request_source_decision_by_artifact_ref(
                    RequestAuthorityArtifactRef(**ref)
                )
                is None
                or (
                    decision.request_guard_artifact_code,
                    decision.request_guard_artifact_version,
                    decision.request_guard_content_sha256,
                    decision.user_id,
                    decision.request_operation_code,
                    decision.decision_stage,
                    decision.source_snapshot_id,
                    decision.source_code,
                    decision.source_version,
                    decision.actual_decision_outcome,
                )
                != (
                    guard.artifact_code,
                    guard.artifact_version,
                    guard.artifact_content_sha256,
                    observation.user_id,
                    observation.request_operation_code,
                    "REQUEST",
                    snapshot.id,
                    source["source_code"],
                    source["source_version"],
                    target["outcome"],
                )
            ):
                raise CandidateGuardAuditValidationError("#713 Source Decision mismatch")
            actual_members = (
                await self._session.scalars(
                    select(RagSourceSnapshotMember).where(RagSourceSnapshotMember.source_snapshot_id == snapshot.id)
                )
            ).all()
            if {str(member.id) for member in actual_members} != {
                member["source_snapshot_member_id"] for member in target["member_decisions"]
            }:
                raise CandidateGuardAuditValidationError("Member Decision set incomplete")
            member_map = {str(member.id): member for member in actual_members}
            for member in target["member_decisions"]:
                source_member = member_map[member["source_snapshot_member_id"]]
                ref = member["member_decision_ref"]
                decision = await self._session.scalar(
                    select(RagRequestMemberDecision).where(
                        RagRequestMemberDecision.artifact_code == ref["artifact_code"],
                        RagRequestMemberDecision.artifact_version == ref["version"],
                        RagRequestMemberDecision.artifact_content_sha256 == ref["content_sha256"],
                    )
                )
                if (
                    decision is None
                    or await RagRequestAuthorityRepository(self._session).get_request_member_decision_by_artifact_ref(
                        RequestAuthorityArtifactRef(**ref)
                    )
                    is None
                    or getattr(source_member.member_kind, "value", source_member.member_kind) != member["member_kind"]
                    or source_member.content_sha256 != member["content_sha256"]
                    or (
                        decision.request_guard_artifact_code,
                        decision.request_guard_artifact_version,
                        decision.request_guard_content_sha256,
                        decision.user_id,
                        decision.request_operation_code,
                        decision.decision_stage,
                        decision.source_snapshot_id,
                        decision.source_snapshot_member_id,
                        decision.member_kind,
                        decision.actual_decision_outcome,
                    )
                    != (
                        guard.artifact_code,
                        guard.artifact_version,
                        guard.artifact_content_sha256,
                        observation.user_id,
                        observation.request_operation_code,
                        "REQUEST",
                        snapshot.id,
                        source_member.id,
                        member["member_kind"],
                        member["outcome"],
                    )
                ):
                    raise CandidateGuardAuditValidationError("#713 Member Decision mismatch")
        if remaining:
            raise CandidateGuardAuditValidationError("unrecorded Bundle sources")
        if observation.search_id is not None:
            search = await self._session.get(MedicationCandidateSearch, observation.search_id)
            if (
                search is None
                or search.prescription_version_medication_id != observation.prescription_version_medication_id
                or search.query_digest != observation.query_digest
                or search.runtime_release_bundle_id != binding.bundle_id
                or search.runtime_release_bundle_manifest_hash != binding.bundle_manifest_hash
                or search.runtime_environment_id != observation.runtime_environment_id
                or search.runtime_environment_revision != observation.environment_revision
                or search.candidate_index_version_id != observation.candidate_index_version_id
            ):
                raise CandidateGuardAuditValidationError("Candidate Search relation mismatch")
        if observation.candidate_search_result_id is not None:
            result = await self._session.get(MedicationCandidateSearchResult, observation.candidate_search_result_id)
            if result is None or result.search_id != observation.search_id:
                raise CandidateGuardAuditValidationError("Candidate Result relation mismatch")
        medication = await self._session.get(
            PrescriptionVersionMedication, observation.prescription_version_medication_id
        )
        if medication is None or medication.prescription_version_id != observation.prescription_version_id:
            raise CandidateGuardAuditValidationError("Prescription medication relation mismatch")
        version = await self._session.get(PrescriptionVersion, observation.prescription_version_id)
        prescription = await self._session.get(Prescription, version.prescription_id) if version else None
        profile = await self._session.get(Profile, prescription.profile_id) if prescription else None
        if profile is None or profile.user_id != observation.user_id:
            raise CandidateGuardAuditValidationError("Prescription owner mismatch")
