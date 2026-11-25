"""Explicit TEST-only Source checks; never a persisted production REQUEST authority.

No configuration is installed by application DI. All numerical policies and allowed
Snapshots must be supplied by a synthetic harness, with no fallback for real Sources.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError, ErrorDetail
from app.models.prescriptions import PrescriptionVersionMedication
from app.models.rag_candidate import MedicationCandidateSearch, MedicationCandidateSearchResult
from app.models.rag_catalog import RagEntityIdentity, RagMedicationProduct
from app.models.rag_runtime import RagRuntimeBundleSource, RagRuntimeEnvironment
from app.models.rag_source import (
    RagSource,
    RagSourceEndpoint,
    RagSourceOperation,
    RagSourceSnapshot,
    RagSourceSnapshotVerification,
)
from app.models.rag_source_use_approval import RagSourceUseApproval
from app.repositories.rag_candidate_index_repository import VerifiedReadyCandidateIndexSnapshot
from app.services.candidate_runtime_binding import CandidateRuntimeBinding
from app.services.rag.candidate_policy import ResolverPolicy, policy_is_valid


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _unavailable() -> ApiError:
    return ApiError(
        status_code=503,
        code="CANDIDATE_RESOLVER_UNAVAILABLE",
        message="현재 약품 후보 검색 기준을 확인할 수 없습니다.",
        details=[ErrorDetail(field="medication_candidate", reason="SOURCE_AUTHORITY_UNAVAILABLE")],
    )


def _stale() -> ApiError:
    return ApiError(
        status_code=409,
        code="IDENTIFICATION_CONTEXT_STALE",
        message="약품 식별 기준이 변경되었습니다. 새 검색이 필요합니다.",
        details=[ErrorDetail(field="medication_candidate", reason="CONTEXT_CHANGED")],
    )


@dataclass(frozen=True, slots=True)
class SyntheticSourcePolicy:
    source_snapshot_id: UUID
    source_code: str
    maximum_age: timedelta

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_snapshot_id, UUID)
            or not self.source_code.startswith("SYNTHETIC_")
            or self.maximum_age <= timedelta(0)
        ):
            raise ValueError("Explicit synthetic Snapshot and positive freshness period required")

    @property
    def scope_policy_hash(self) -> str:
        return _hash(
            {
                "schema": "candidate-synthetic-scope-v1",
                "snapshot_id": str(self.source_snapshot_id),
                "source_code": self.source_code,
                "environment": "TEST",
                "purpose": "PRODUCT_IDENTIFICATION",
            }
        )

    @property
    def freshness_policy_hash(self) -> str:
        return _hash(
            {
                "schema": "candidate-synthetic-freshness-v1",
                "anchor": "verified_at",
                "maximum_age_microseconds": self.maximum_age // timedelta(microseconds=1),
            }
        )


@dataclass(frozen=True, slots=True)
class SyntheticCandidateContext:
    runtime_environment_id: UUID
    source_policies: tuple[SyntheticSourcePolicy, ...]
    resolver_policy: ResolverPolicy
    expected_governance_revision_ref: str
    expected_safety_epoch: int
    search_ttl: timedelta
    query_hmac_key: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.runtime_environment_id, UUID)
            or not self.source_policies
            or len({p.source_snapshot_id for p in self.source_policies}) != len(self.source_policies)
            or not policy_is_valid(self.resolver_policy)
            or self.resolver_policy.enable_dense
            or not self.resolver_policy.policy_version.startswith("synthetic-")
            or not self.expected_governance_revision_ref.startswith("synthetic-")
            or type(self.expected_safety_epoch) is not int
            or self.expected_safety_epoch < 1
            or self.search_ttl <= timedelta(0)
            or len(self.query_hmac_key) < 32
        ):
            raise ValueError("Complete explicit TEST-only Candidate context required")

    @property
    def policy_hash(self) -> str:
        policy = asdict(self.resolver_policy)
        policy["auto_select_stages"] = sorted(self.resolver_policy.auto_select_stages)
        return _hash(
            {
                "schema": "candidate-synthetic-policy-v1",
                "resolver": policy,
                "environment_id": str(self.runtime_environment_id),
                "governance": self.expected_governance_revision_ref,
                "epoch": self.expected_safety_epoch,
                "ttl_microseconds": self.search_ttl // timedelta(microseconds=1),
                "sources": sorted(
                    (str(p.source_snapshot_id), p.source_code, p.scope_policy_hash, p.freshness_policy_hash)
                    for p in self.source_policies
                ),
            }
        )

    def query_digest(self, medication_name: str, strength_text: str | None) -> str:
        payload = _canonical(
            {
                "schema": "candidate-synthetic-query-v1",
                "policy_hash": self.policy_hash,
                "medication_name": medication_name,
                "strength_text": strength_text,
            }
        )
        return hmac.new(self.query_hmac_key.encode(), payload.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class SyntheticCandidateReceipt:
    scope_hash: str
    source_facts_hash: str
    policy_hash: str
    checked_at: datetime
    expires_at: datetime


class SyntheticCandidateSourceGuard:
    def __init__(self, session: AsyncSession, context: SyntheticCandidateContext) -> None:
        self._session = session
        self._context = context

    async def validate(
        self,
        *,
        binding: CandidateRuntimeBinding,
        snapshot: VerifiedReadyCandidateIndexSnapshot,
        medication: PrescriptionVersionMedication,
        user_id: UUID,
        operation: str,
        query_digest: str,
        search: MedicationCandidateSearch | None = None,
    ) -> SyntheticCandidateReceipt:
        context = self._context
        self._check_scope(binding, snapshot, medication, operation, query_digest, search)
        # Caller already locks the environment through outer commit. Refresh identity-map
        # state: an earlier cached observation is not a current governance observation.
        environment = await self._session.scalar(
            select(RagRuntimeEnvironment)
            .where(RagRuntimeEnvironment.id == context.runtime_environment_id)
            .execution_options(populate_existing=True)
        )
        if (
            environment is None
            or environment.environment_code != "TEST"
            or environment.governance_revision_ref != context.expected_governance_revision_ref
            or environment.safety_epoch != context.expected_safety_epoch
        ):
            raise _unavailable()
        pins = tuple(
            (
                await self._session.scalars(
                    select(RagRuntimeBundleSource)
                    .where(
                        RagRuntimeBundleSource.bundle_id == binding.runtime_release_bundle_id,
                        (RagRuntimeBundleSource.required | RagRuntimeBundleSource.selected_for_operation),
                    )
                    .order_by(RagRuntimeBundleSource.source_snapshot_id, RagRuntimeBundleSource.source_purpose)
                    .with_for_update(read=True)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        policies = {p.source_snapshot_id: p for p in context.source_policies}
        selected_refs = {(str(p.source_snapshot_id), p.source_version) for p in pins if p.selected_for_operation}
        index_refs = {(ref.snapshot_id, ref.source_version) for ref in snapshot.source_refs}
        if not pins or not index_refs or not index_refs <= selected_refs:
            raise _unavailable()
        # Only catalog/product-identification Sources are supported in this fixture path.
        # Other required purposes must receive a separately approved operation policy.
        if any(p.source_purpose not in {"CATALOG", "CANDIDATE_INDEX_INPUT"} for p in pins):
            raise _unavailable()
        deadlines: list[datetime] = []
        facts: list[object] = []
        for pin in pins:
            policy = policies.get(pin.source_snapshot_id)
            if (
                policy is None
                or pin.scope_policy_hash != policy.scope_policy_hash
                or pin.freshness_policy_hash != policy.freshness_policy_hash
            ):
                raise _unavailable()
            fact, deadline = await self._check_source(pin, policy)
            facts.append(fact)
            deadlines.append(deadline)
        await self._check_displayed_product(search, snapshot)
        # PostgreSQL CURRENT_TIMESTAMP is transaction-start time. Use a fresh wall clock,
        # including after waits, and re-run this guard immediately before finalization.
        now = await self._session.scalar(select(func.clock_timestamp()))
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise _unavailable()
        expires_at = min(now + context.search_ttl, *deadlines)
        if expires_at <= now:
            raise _unavailable()
        if search is not None and search.expires_at is not None:
            expires_at = min(expires_at, search.expires_at)
            if expires_at <= now:
                raise _stale()
        return SyntheticCandidateReceipt(
            scope_hash=_hash(
                {
                    "schema": "candidate-synthetic-receipt-v1",
                    "operation": operation,
                    "user_id": str(user_id),
                    "medication_id": str(medication.id),
                    "prescription_version_id": str(medication.prescription_version_id),
                    "search_id": str(search.id) if search else None,
                    "bundle_id": str(binding.runtime_release_bundle_id),
                    "bundle_hash": binding.runtime_release_bundle_manifest_hash,
                    "environment_id": str(binding.runtime_environment_id),
                    "revision": binding.runtime_environment_revision,
                    "governance": environment.governance_revision_ref,
                    "epoch": environment.safety_epoch,
                    "index_id": str(snapshot.version.id),
                    "query_digest": query_digest,
                    "policy_hash": context.policy_hash,
                    "source_facts_hash": _hash(facts),
                    "checked_at": now.isoformat(),
                    "expires_at": expires_at.isoformat(),
                }
            ),
            source_facts_hash=_hash(facts),
            policy_hash=context.policy_hash,
            checked_at=now,
            expires_at=expires_at,
        )

    async def _check_displayed_product(
        self, search: MedicationCandidateSearch | None, snapshot: VerifiedReadyCandidateIndexSnapshot
    ) -> None:
        if search is None or search.status != "READY":
            return
        results = tuple(
            (
                await self._session.scalars(
                    select(MedicationCandidateSearchResult)
                    .where(
                        MedicationCandidateSearchResult.search_id == search.id,
                        MedicationCandidateSearchResult.is_displayed.is_(True),
                    )
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if len(results) != 1 or not results[0].selection_eligible or results[0].product_id is None:
            raise _unavailable()
        result = results[0]
        sources = {
            m.product_source_snapshot_id
            for m in snapshot.members
            if m.identity_entity_type.value == "PRODUCT"
            and m.identity_code_system == result.code_system
            and m.identity_canonical_code == result.canonical_code
        }
        allowed_sources = {UUID(ref.snapshot_id) for ref in snapshot.source_refs}
        if len(sources) != 1 or not sources <= allowed_sources:
            raise _unavailable()
        product = await self._session.scalar(
            select(RagMedicationProduct)
            .join(RagEntityIdentity, RagEntityIdentity.id == RagMedicationProduct.entity_identity_id)
            .where(
                RagEntityIdentity.entity_type == "PRODUCT",
                RagEntityIdentity.code_system == result.code_system,
                RagEntityIdentity.canonical_code == result.canonical_code,
                RagMedicationProduct.id == result.product_id,
                RagMedicationProduct.source_snapshot_id == next(iter(sources)),
                RagMedicationProduct.code_system == result.code_system,
                RagMedicationProduct.canonical_code == result.canonical_code,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if (
            product is None
            or product.product_status != "ACTIVE"
            or any(
                getattr(product, field) != getattr(result, field)
                for field in ("product_name", "strength_text", "dosage_form", "manufacturer_name", "product_status")
            )
        ):
            raise _unavailable()

    def _check_scope(
        self,
        binding: CandidateRuntimeBinding,
        snapshot: VerifiedReadyCandidateIndexSnapshot,
        medication: PrescriptionVersionMedication,
        operation: str,
        query_digest: str,
        search: MedicationCandidateSearch | None,
    ) -> None:
        context = self._context
        if (
            operation not in {"create", "read", "select"}
            or not binding.complete
            or binding.runtime_environment_id != context.runtime_environment_id
            or query_digest != context.query_digest(medication.medication_name, medication.strength_text)
        ):
            raise _stale()
        if search is not None and (
            search.prescription_version_medication_id != medication.id
            or search.query_digest != query_digest
            or search.candidate_index_version_id != snapshot.version.id
            or CandidateRuntimeBinding(
                search.runtime_release_bundle_id,
                search.runtime_release_bundle_manifest_hash,
                search.runtime_environment_id,
                search.runtime_environment_revision,
            )
            != binding
        ):
            raise _stale()

    async def _check_verification(self, snapshot: RagSourceSnapshot) -> None:
        # The lifecycle writer's seal anchors state; it is not publication approval.
        seal = await self._session.scalar(
            select(RagSourceSnapshotVerification)
            .where(
                RagSourceSnapshotVerification.id == snapshot.verification_seal_id,
                RagSourceSnapshotVerification.snapshot_id == snapshot.id,
                RagSourceSnapshotVerification.check_name == "snapshot-state-seal",
                RagSourceSnapshotVerification.verification_result == "NO_CHANGE",
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        selection = await self._session.scalar(
            select(RagSourceSnapshotVerification)
            .where(
                RagSourceSnapshotVerification.snapshot_id == snapshot.id,
                RagSourceSnapshotVerification.check_name == "snapshot-current-selection",
                RagSourceSnapshotVerification.verification_result == "PASSED",
                RagSourceSnapshotVerification.verified_at == snapshot.effective_at,
                RagSourceSnapshotVerification.verified_by.is_not(None),
                func.length(func.trim(RagSourceSnapshotVerification.verified_by)) > 0,
            )
            .order_by(RagSourceSnapshotVerification.id)
            .limit(1)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if seal is None or selection is None:
            raise _unavailable()
        if snapshot.rejected_record_count > 0:
            publication = await self._session.scalar(
                select(RagSourceSnapshotVerification)
                .where(
                    RagSourceSnapshotVerification.snapshot_id == snapshot.id,
                    RagSourceSnapshotVerification.check_name == "snapshot-publication-approval",
                    RagSourceSnapshotVerification.verification_result == "PASSED",
                    RagSourceSnapshotVerification.verified_by.is_not(None),
                    func.length(func.trim(RagSourceSnapshotVerification.verified_by)) > 0,
                    RagSourceSnapshotVerification.verified_at <= snapshot.effective_at,
                )
                .order_by(RagSourceSnapshotVerification.id)
                .limit(1)
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
            if publication is None:
                raise _unavailable()

    async def _check_source(
        self, pin: RagRuntimeBundleSource, policy: SyntheticSourcePolicy
    ) -> tuple[object, datetime]:
        row = (
            await self._session.execute(
                select(RagSource, RagSourceEndpoint, RagSourceOperation, RagSourceSnapshot)
                .join(RagSourceEndpoint, RagSourceEndpoint.source_id == RagSource.id)
                .join(RagSourceOperation, RagSourceOperation.endpoint_id == RagSourceEndpoint.id)
                .join(RagSourceSnapshot, RagSourceSnapshot.operation_id == RagSourceOperation.id)
                .where(RagSourceSnapshot.id == pin.source_snapshot_id)
                .with_for_update(read=True, of=(RagSource, RagSourceEndpoint, RagSourceOperation, RagSourceSnapshot))
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if row is None:
            raise _unavailable()
        source, endpoint, operation, snapshot = row
        if (
            source.source_code != policy.source_code
            or source.lifecycle_status != "ACTIVE"
            or endpoint.lifecycle_status != "VERIFIED"
            or endpoint.runtime_status != "ENABLED"
            or endpoint.acquisition_status != "APPROVED"
            or operation.runtime_status != "ENABLED"
            or operation.acquisition_status != "APPROVED"
            or snapshot.verification_status != "CURRENT"
            or snapshot.source_version != pin.source_version
            or snapshot.canonical_checksum != pin.canonical_checksum
            or snapshot.verified_at is None
            or snapshot.verification_seal_id is None
        ):
            raise _unavailable()
        await self._check_verification(snapshot)
        approval = await self._session.scalar(
            select(RagSourceUseApproval)
            .where(
                RagSourceUseApproval.source_snapshot_id == snapshot.id,
                RagSourceUseApproval.source_code == source.source_code,
                RagSourceUseApproval.source_version == pin.source_version,
                RagSourceUseApproval.environment == "TEST",
                RagSourceUseApproval.purpose == "PRODUCT_IDENTIFICATION",
                RagSourceUseApproval.approval_version == pin.approval_version,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        now = await self._session.scalar(select(func.clock_timestamp()))
        if (
            approval is None
            or not isinstance(now, datetime)
            or approval.revoked_at is not None
            or not approval.valid_from <= now < approval.expires_at
            or not snapshot.collected_at <= snapshot.verified_at <= now
            or snapshot.effective_at is None
            or snapshot.effective_at > now
        ):
            raise _unavailable()
        deadline = min(approval.expires_at, snapshot.verified_at + policy.maximum_age)
        if now >= deadline:
            raise _unavailable()
        return (
            {
                "snapshot_id": str(snapshot.id),
                "source_code": source.source_code,
                "version": pin.source_version,
                "checksum": pin.canonical_checksum,
                "purpose": pin.source_purpose,
                "required": pin.required,
                "selected": pin.selected_for_operation,
                "approval_id": str(approval.id),
                "approval_version": pin.approval_version,
                "approval_expiry": approval.expires_at.isoformat(),
                "scope_hash": pin.scope_policy_hash,
                "freshness_hash": pin.freshness_policy_hash,
                "verified_at": snapshot.verified_at.isoformat(),
            },
            deadline,
        )
