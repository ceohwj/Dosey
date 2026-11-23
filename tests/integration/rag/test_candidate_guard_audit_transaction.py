"""Actual PostgreSQL atomicity for the explicit synthetic Candidate audit coordinator."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import Table, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401
from app.core import config
from app.core.db.databases import Base
from app.dtos.medication_candidates import ConfirmMedicationCandidateRequest
from app.models.async_jobs import IdempotencyRecord
from app.models.prescriptions import PrescriptionVersionMedication
from app.models.rag_candidate import MedicationCandidateSearch, MedicationIdentification
from app.models.rag_candidate_guard_audit import RagCandidateRequestGuardAudit
from app.models.rag_candidate_index import RagCandidateIndexVersion
from app.models.rag_request_authority import RagRequestGuardAuthority, RagRequestSourceDecision
from app.models.rag_request_guard_runtime_binding import RagRequestGuardRuntimeBinding
from app.models.rag_runtime import RagRuntimeEnvironment, RagRuntimeReleaseBundle
from app.models.rag_source import (
    RagSource,
    RagSourceEndpoint,
    RagSourceIngestionArtifact,
    RagSourceIngestionRun,
    RagSourceOperation,
    RagSourceSnapshot,
    RagSourceSnapshotMember,
)
from app.models.rag_source_use_approval import RagSourceUseApproval
from app.repositories.rag_candidate_guard_audit_repository import RagCandidateGuardAuditRepository
from app.repositories.rag_request_authority_repository import RequestGuardAuthorityRecord, RequestSourceDecisionRecord
from app.repositories.rag_source_capture_observation_repository import (
    RagSourceCaptureObservationRepository,
    raw_manifest_hash,
)
from app.services.candidate_guard_audit import (
    CandidateAuditStopResult,
    CandidateGuardAuditCoordinator,
    CandidateGuardAuditEnvelope,
    CandidateGuardPolicyDenied,
)
from app.tests.db_extensions import EXTENSION_SCHEMA, ensure_trigram_extension, ensure_vector_extension
from rag_runtime.candidate_guard_audit import (
    CandidateGuardAuditObservation,
    CandidateGuardAuditValidationError,
    canonical_candidate_guard_proof_hash,
    compute_candidate_guard_audit_hash,
)
from rag_runtime.request_authority import (
    RequestAuthorityDecisionOutcome,
    RequestAuthorityDecisionStage,
    compute_request_guard_authority_ref,
    compute_request_source_decision_authority_ref,
)
from rag_runtime.request_guard_runtime_binding import (
    RequestGuardRuntimeBindingObservation,
    canonical_scope_manifest_hash,
    compute_request_guard_runtime_binding_ref,
)
from rag_runtime.runtime_environment import RuntimeEnvironmentCode
from rag_runtime.source_capture_observation import (
    SourceCaptureIssuerRef,
    SourceCaptureObservation,
    compute_source_capture_observation_hash,
)
from tests.integration.rag.test_candidate_search_synthetic_flow import _create, _seed_flow, _service, _tables


def _ref(ref: object) -> dict[str, str]:
    return {"artifact_code": ref.artifact_code, "version": ref.version, "content_sha256": ref.content_sha256}


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if config.DB_NAME != "test" or config.DB_HOST not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("synthetic audit integration requires a local test database")
    schema = "candidate_audit_" + uuid4().hex
    admin = create_async_engine(config.database_url, poolclass=NullPool, hide_parameters=True)
    engine = create_async_engine(
        config.database_url,
        poolclass=NullPool,
        hide_parameters=True,
        isolation_level="REPEATABLE READ",
        connect_args={"server_settings": {"search_path": f"{schema},public,{EXTENSION_SCHEMA}"}},
    )
    roots = _tables() + [
        RagCandidateRequestGuardAudit.__table__,
        RagRequestGuardAuthority.__table__,
        RagRequestSourceDecision.__table__,
        RagRequestGuardRuntimeBinding.__table__,
        RagSourceSnapshotMember.__table__,
        RagSourceIngestionArtifact.__table__,
        Base.metadata.tables["rag_source_capture_observation"],
    ]
    seen: dict[str, Table] = {}
    stack = list(roots)
    while stack:
        table = stack.pop()
        if table.name in seen:
            continue
        seen[table.name] = table
        stack.extend(key.column.table for key in table.foreign_keys)
    try:
        async with admin.begin() as connection:
            await ensure_vector_extension(connection)
            await ensure_trigram_extension(connection)
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=list(seen.values())))
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


async def _seed_synthetic_capture(sessions: async_sessionmaker[AsyncSession], fixture) -> SourceCaptureObservation:
    """Simulated complete provider capture; all rows commit before Candidate evaluation."""
    async with sessions.begin() as session:
        snapshot = await session.get(RagSourceSnapshot, fixture.source_id)
        operation = await session.get(RagSourceOperation, snapshot.operation_id)
        endpoint = await session.get(RagSourceEndpoint, operation.endpoint_id)
        source = await session.get(RagSource, endpoint.source_id)
        started = datetime.now(UTC) - timedelta(minutes=2)
        completed = started + timedelta(seconds=1)
        # The older Search fixture has no acquisition receipt. This TEST-only
        # NO_CHANGE capture extends it without claiming a real network fetch.
        snapshot.endpoint_receipt_hash = "e" * 64
        artifact = RagSourceIngestionArtifact(
            ingestion_run_id=uuid4(),
            page_number=1,
            artifact_kind="RAW_RESPONSE",
            artifact_key="synthetic-page-1",
            storage_backend="synthetic",
            object_key="synthetic-page-1",
            raw_checksum="b" * 64,
            byte_size=4,
            content_type="application/json",
        )
        manifest_hash = raw_manifest_hash([artifact])
        run = RagSourceIngestionRun(
            id=artifact.ingestion_run_id,
            operation_id=operation.id,
            run_group_key=uuid4().hex,
            snapshot_id=snapshot.id,
            run_status="NO_CHANGE",
            attempt_number=1,
            started_at=started,
            finished_at=completed,
        )
        session.add_all([run, artifact])
        await session.flush()
        observation = SourceCaptureObservation(
            capture_observation_id=uuid4(),
            ingestion_run_id=run.id,
            source_snapshot_id=snapshot.id,
            source_version=snapshot.source_version,
            canonical_checksum=snapshot.canonical_checksum,
            source_code=source.source_code,
            endpoint_code=endpoint.endpoint_code,
            operation_code=operation.operation_code,
            capture_protocol_version="synthetic-test-v1",
            issuer_ref=SourceCaptureIssuerRef("source_live_capture_issuer", "synthetic-test-v1", "c" * 64),
            request_profile_sha256="d" * 64,
            capture_origin="LIVE_PROVIDER",
            capture_started_at=started,
            capture_completed_at=completed,
            recorded_at=completed + timedelta(seconds=1),
            raw_manifest_sha256=manifest_hash,
            endpoint_contract_receipt_sha256="e" * 64,
            full_scan_completed=True,
            primary_key_validation_passed=True,
            page_count=1,
            record_count=snapshot.record_count,
        )
        await RagSourceCaptureObservationRepository(session).record(observation)
        return observation


async def _envelope(
    session: AsyncSession,
    fixture,
    capture: SourceCaptureObservation,
    *,
    outcome: str = "FAIL",
    search_id: UUID | None = None,
    result_id: UUID | None = None,
    operation: str = "medication-candidate.search.create",
) -> CandidateGuardAuditEnvelope:
    manifest = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "docs/contracts/targets/post-mvp-1/candidate-request-guard-audit-v1.hash-vector.json"
        ).read_text()
    )["hash_input"]
    snapshot = await session.get(RagSourceSnapshot, fixture.source_id)
    operation_row = await session.get(RagSourceOperation, snapshot.operation_id)
    endpoint = await session.get(RagSourceEndpoint, operation_row.endpoint_id)
    source_row = await session.get(RagSource, endpoint.source_id)
    approval = await session.get(RagSourceUseApproval, fixture.approval_id)
    environment = await session.get(RagRuntimeEnvironment, fixture.environment_id)
    bundle = await session.get(RagRuntimeReleaseBundle, fixture.bundle_id)
    index = await session.get(RagCandidateIndexVersion, fixture.index_id)
    medication = await session.get(PrescriptionVersionMedication, fixture.medication_id)
    target = manifest["targets"][0]
    target["source"].update(
        source_code=source_row.source_code,
        endpoint_code=endpoint.endpoint_code,
        operation_code=operation_row.operation_code,
        snapshot_id=str(snapshot.id),
        source_version=snapshot.source_version,
        canonical_sha256=snapshot.canonical_checksum,
    )
    target["approval"].update(
        id=str(approval.id),
        version=approval.approval_version,
        valid_from=approval.valid_from.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        expires_at=approval.expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )
    target["freshness"].update(
        capture_observation_id=str(capture.capture_observation_id),
        capture_observation_sha256=compute_source_capture_observation_hash(capture),
        anchor_at=capture.capture_completed_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )
    target["lifecycle"].update(
        verification_seal_id=str(snapshot.verification_seal_id),
        source_status=source_row.lifecycle_status.value,
        endpoint_status=endpoint.lifecycle_status.value,
        endpoint_runtime=endpoint.runtime_status.value,
        endpoint_acquisition=endpoint.acquisition_status.value,
        operation_runtime=operation_row.runtime_status.value,
        operation_acquisition=operation_row.acquisition_status.value,
        snapshot_status=snapshot.verification_status.value,
    )
    source_members = (
        await session.scalars(
            select(RagSourceSnapshotMember).where(RagSourceSnapshotMember.source_snapshot_id == snapshot.id)
        )
    ).all()
    assert not source_members, "this synthetic fixture has no Source Snapshot members"
    target["outcome"] = outcome
    target["reason_codes"] = ["APPROVAL_EXPIRED"] if outcome == "FAIL" else []
    manifest["aggregate_outcome"] = outcome
    guard = RequestGuardAuthorityRecord(fixture.owner_id, operation, RequestAuthorityDecisionStage.REQUEST)
    guard_ref = compute_request_guard_authority_ref(
        user_id=guard.user_id, request_operation_code=operation, decision_stage=guard.decision_stage
    )
    source = RequestSourceDecisionRecord(
        guard_ref,
        fixture.owner_id,
        operation,
        guard.decision_stage,
        snapshot.id,
        source_row.source_code,
        snapshot.source_version,
        RequestAuthorityDecisionOutcome(outcome),
    )
    target["source_decision_ref"] = _ref(
        compute_request_source_decision_authority_ref(
            request_guard_ref=guard_ref,
            user_id=source.user_id,
            request_operation_code=operation,
            decision_stage=source.decision_stage,
            source_snapshot_id=snapshot.id,
            source_code=source.source_code,
            source_version=source.source_version,
            actual_decision_outcome=source.actual_decision_outcome,
        )
    )
    scopes = ("PRODUCT_IDENTIFICATION",)
    binding = RequestGuardRuntimeBindingObservation(
        request_guard_decision_id=uuid4(),
        actual_decision_outcome=RequestAuthorityDecisionOutcome(outcome),
        user_id=fixture.owner_id,
        request_operation_code=operation,
        decision_stage=guard.decision_stage,
        environment=RuntimeEnvironmentCode.TEST,
        bundle_id=bundle.id,
        bundle_manifest_hash=bundle.bundle_manifest_hash,
        request_scope_codes=scopes,
        scope_manifest_hash=canonical_scope_manifest_hash(scopes),
        legacy_request_authority_ref=guard_ref,
    )
    manifest["runtime_binding_ref"] = _ref(compute_request_guard_runtime_binding_ref(binding))
    manifest["scope_manifest_hash"] = binding.scope_manifest_hash
    checked_at = datetime.now(UTC)
    observation = CandidateGuardAuditObservation(
        request_guard_decision_id=binding.request_guard_decision_id,
        runtime_binding_ref=manifest["runtime_binding_ref"],
        user_id=fixture.owner_id,
        prescription_version_id=medication.prescription_version_id,
        prescription_version_medication_id=medication.id,
        request_operation_code=operation,
        search_id=search_id,
        candidate_search_result_id=result_id,
        runtime_environment_id=environment.id,
        environment_revision=environment.environment_revision,
        governance_revision_ref=environment.governance_revision_ref,
        safety_epoch=environment.safety_epoch,
        candidate_index_version_id=index.id,
        candidate_index_artifact_sha256=bundle.candidate_index_manifest_hash,
        query_digest=fixture.context.query_digest(
            medication_name=medication.medication_name, strength_text=medication.strength_text
        ),
        resolver_policy_sha256="a" * 64,
        retention_policy_sha256="a" * 64,
        proof_manifest_sha256=canonical_candidate_guard_proof_hash(manifest),
        checked_at=checked_at,
        valid_until=checked_at + timedelta(minutes=5) if outcome == "PASS" else None,
        outcome=outcome,
        reason_codes=("APPROVAL_EXPIRED",) if outcome == "FAIL" else (),
    )
    return CandidateGuardAuditEnvelope(
        guard=guard, sources=(source,), members=(), binding=binding, observation=observation, proof_manifest=manifest
    )


@pytest.mark.asyncio
async def test_coordinator_rolls_back_domain_without_callback_savepoint_and_commits_original_audit_only(
    sessions,
) -> None:
    fixture = await _seed_flow(sessions)
    capture = await _seed_synthetic_capture(sessions, fixture)
    async with sessions.begin() as session:
        envelope = await _envelope(session, fixture, capture)
        original_id = envelope.observation.request_guard_decision_id
        original_hash = envelope.observation.proof_manifest_sha256
        coordinator = CandidateGuardAuditCoordinator(session)

        async def operation() -> None:
            await _service(session, fixture.context).create_candidate_search(
                user=SimpleNamespace(id=fixture.owner_id),
                prescription_version_medication_id=fixture.medication_id,
            )
            raise CandidateGuardPolicyDenied(envelope)

        result = await coordinator.execute_mutation(operation)
        assert isinstance(result, CandidateAuditStopResult)
        assert result.status_code == 503
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MedicationCandidateSearch)) == 0
        assert await session.scalar(select(func.count()).select_from(RagRequestGuardAuthority)) == 1
        assert await session.scalar(select(func.count()).select_from(RagRequestSourceDecision)) == 1
        assert await session.scalar(select(func.count()).select_from(RagRequestGuardRuntimeBinding)) == 1
        assert await session.scalar(select(func.count()).select_from(RagCandidateRequestGuardAudit)) == 1
        stored = await RagCandidateGuardAuditRepository(session).get_exact(
            original_id, compute_candidate_guard_audit_hash(envelope.observation)
        )
        assert stored is not None and stored[0].proof_manifest_sha256 == original_hash


@pytest.mark.asyncio
async def test_pass_audit_and_search_share_outer_transaction(sessions) -> None:
    fixture = await _seed_flow(sessions)
    capture = await _seed_synthetic_capture(sessions, fixture)
    async with sessions.begin() as session:
        created = await _service(session, fixture.context).create_candidate_search(
            user=SimpleNamespace(id=fixture.owner_id),
            prescription_version_medication_id=fixture.medication_id,
        )
        envelope = await _envelope(session, fixture, capture, outcome="PASS", search_id=created.search_id)
        digest = await CandidateGuardAuditCoordinator(session).write_pass(envelope)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MedicationCandidateSearch)) == 1
        assert await RagCandidateGuardAuditRepository(session).get_exact(
            envelope.observation.request_guard_decision_id, digest
        )


@pytest.mark.asyncio
async def test_missing_capture_rolls_back_domain_and_all_audit_rows(sessions) -> None:
    fixture = await _seed_flow(sessions)
    capture = await _seed_synthetic_capture(sessions, fixture)
    missing_capture = replace(capture, capture_observation_id=uuid4())
    with pytest.raises(CandidateGuardAuditValidationError, match="capture observation binding mismatch"):
        async with sessions.begin() as session:
            created = await _service(session, fixture.context).create_candidate_search(
                user=SimpleNamespace(id=fixture.owner_id),
                prescription_version_medication_id=fixture.medication_id,
            )
            envelope = await _envelope(session, fixture, missing_capture, outcome="PASS", search_id=created.search_id)
            await CandidateGuardAuditCoordinator(session).write_pass(envelope)
    async with sessions() as session:
        for model in (
            MedicationCandidateSearch,
            RagRequestGuardAuthority,
            RagRequestSourceDecision,
            RagRequestGuardRuntimeBinding,
            RagCandidateRequestGuardAudit,
        ):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.asyncio
async def test_confirm_typed_denial_after_domain_write_uses_existing_idempotency_savepoint(sessions) -> None:
    fixture = await _seed_flow(sessions)
    capture = await _seed_synthetic_capture(sessions, fixture)
    created = await _create(sessions, fixture)
    assert created.candidate_search_result_id is not None
    request = ConfirmMedicationCandidateRequest(
        prescription_version_medication_id=fixture.medication_id,
        candidate_search_result_id=created.candidate_search_result_id,
    )
    async with sessions.begin() as session:
        envelope = await _envelope(
            session,
            fixture,
            capture,
            operation="medication-candidate.confirm",
            search_id=created.search_id,
            result_id=created.candidate_search_result_id,
        )
        service = _service(session, fixture.context)
        identification = service._identification_service
        original_confirm = identification.confirm_identification

        async def deny_after_domain_write(**kwargs):
            await original_confirm(**kwargs)
            raise CandidateGuardPolicyDenied(envelope)

        identification.confirm_identification = deny_after_domain_write
        result = await CandidateGuardAuditCoordinator(session).execute_mutation(
            lambda: service.confirm_candidate(
                user=SimpleNamespace(id=fixture.owner_id),
                request=request,
                idempotency_key="synthetic-denied-confirm-" + uuid4().hex,
            )
        )
        assert isinstance(result, CandidateAuditStopResult)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MedicationIdentification)) == 0
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 0
        assert await session.scalar(select(func.count()).select_from(RagCandidateRequestGuardAudit)) == 1
        assert await session.scalar(select(func.count()).select_from(MedicationCandidateSearch)) == 1


@pytest.mark.asyncio
async def test_cancelled_domain_operation_commits_neither_search_nor_audit(sessions) -> None:
    fixture = await _seed_flow(sessions)
    with pytest.raises(asyncio.CancelledError):
        async with sessions.begin() as session:
            coordinator = CandidateGuardAuditCoordinator(session)

            async def cancelled_operation() -> None:
                await _service(session, fixture.context).create_candidate_search(
                    user=SimpleNamespace(id=fixture.owner_id),
                    prescription_version_medication_id=fixture.medication_id,
                )
                raise asyncio.CancelledError()

            await coordinator.execute_mutation(cancelled_operation)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MedicationCandidateSearch)) == 0
        assert await session.scalar(select(func.count()).select_from(RagCandidateRequestGuardAudit)) == 0


@pytest.mark.asyncio
async def test_existing_success_replay_after_revocation_does_not_write_new_audit(sessions) -> None:
    fixture = await _seed_flow(sessions)
    created = await _create(sessions, fixture)
    assert created.candidate_search_result_id is not None
    request = ConfirmMedicationCandidateRequest(
        prescription_version_medication_id=fixture.medication_id,
        candidate_search_result_id=created.candidate_search_result_id,
    )
    key = "synthetic-audit-replay-" + uuid4().hex
    async with sessions.begin() as session:
        first = await _service(session, fixture.context).confirm_candidate(
            user=SimpleNamespace(id=fixture.owner_id),
            request=request,
            idempotency_key=key,
        )
        receipt = await session.scalar(select(IdempotencyRecord))
        assert receipt is not None
        saved = (receipt.key_hmac, receipt.request_hash, receipt.response_body_snapshot)
    from app.repositories.rag_source_use_approval_repository import (
        RagSourceUseApprovalRepository,
        SourceUseApprovalRevoke,
    )

    async with sessions.begin() as session:
        await RagSourceUseApprovalRepository(session).revoke(
            SourceUseApprovalRevoke(
                approval_id=fixture.approval_id,
                revoked_at=datetime.now(UTC),
                revoked_by=fixture.owner_id,
                revoked_reason="SYNTHETIC_TEST_REVOCATION",
            )
        )
    async with sessions.begin() as session:
        replay = await CandidateGuardAuditCoordinator(session).execute_mutation(
            lambda: _service(session, None).confirm_candidate(
                user=SimpleNamespace(id=fixture.owner_id),
                request=request,
                idempotency_key=key,
            )
        )
        assert replay.model_dump(mode="json") == first.model_dump(mode="json")
        receipt = await session.scalar(select(IdempotencyRecord))
        assert receipt is not None
        assert (receipt.key_hmac, receipt.request_hash, receipt.response_body_snapshot) == saved
        assert await session.scalar(select(func.count()).select_from(RagCandidateRequestGuardAudit)) == 0
