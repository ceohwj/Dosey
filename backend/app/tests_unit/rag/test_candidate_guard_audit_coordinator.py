"""The outer Candidate boundary preserves the original denial after savepoint rollback."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from app.core.errors import ApiError
from app.repositories.rag_request_authority_repository import RequestGuardAuthorityRecord, RequestSourceDecisionRecord
from app.services.candidate_guard_audit import (
    CandidateAuditStopResult,
    CandidateGuardAuditCoordinator,
    CandidateGuardAuditEnvelope,
    CandidateGuardPolicyDenied,
)
from rag_runtime.candidate_guard_audit import (
    CandidateGuardAuditObservation,
    CandidateGuardAuditValidationError,
    canonical_candidate_guard_proof_hash,
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


def _ref_dict(ref: Any) -> dict[str, str]:
    return {"artifact_code": ref.artifact_code, "version": ref.version, "content_sha256": ref.content_sha256}


def _session() -> AsyncMock:
    session = AsyncMock()
    session.begin_nested = MagicMock(return_value=AsyncMock())
    return session


def _envelope() -> CandidateGuardAuditEnvelope:
    vector = json.loads(
        (
            Path(__file__).resolve().parents[4]
            / "docs/contracts/targets/post-mvp-1/candidate-request-guard-audit-v1.hash-vector.json"
        ).read_text()
    )
    manifest = vector["hash_input"]
    user_id = uuid4()
    operation = "medication-candidate.search.create"
    guard = RequestGuardAuthorityRecord(user_id, operation, RequestAuthorityDecisionStage.REQUEST)
    guard_ref = compute_request_guard_authority_ref(
        user_id=user_id, request_operation_code=operation, decision_stage=guard.decision_stage
    )
    source = RequestSourceDecisionRecord(
        request_guard_ref=guard_ref,
        user_id=user_id,
        request_operation_code=operation,
        decision_stage=guard.decision_stage,
        source_snapshot_id=UUID(manifest["targets"][0]["source"]["snapshot_id"]),
        source_code="SYNTHETIC_PRODUCT",
        source_version="synthetic-v1",
        actual_decision_outcome=RequestAuthorityDecisionOutcome.FAIL,
    )
    manifest["targets"][0]["source_decision_ref"] = _ref_dict(
        compute_request_source_decision_authority_ref(
            request_guard_ref=source.request_guard_ref,
            user_id=source.user_id,
            request_operation_code=source.request_operation_code,
            decision_stage=source.decision_stage,
            source_snapshot_id=source.source_snapshot_id,
            source_code=source.source_code,
            source_version=source.source_version,
            actual_decision_outcome=source.actual_decision_outcome,
        )
    )
    scopes = ("PRODUCT_IDENTIFICATION",)
    binding = RequestGuardRuntimeBindingObservation(
        request_guard_decision_id=uuid4(),
        actual_decision_outcome=RequestAuthorityDecisionOutcome.FAIL,
        user_id=user_id,
        request_operation_code=operation,
        decision_stage=RequestAuthorityDecisionStage.REQUEST,
        environment=RuntimeEnvironmentCode.TEST,
        bundle_id=uuid4(),
        bundle_manifest_hash="a" * 64,
        request_scope_codes=scopes,
        scope_manifest_hash=canonical_scope_manifest_hash(scopes),
        legacy_request_authority_ref=guard_ref,
    )
    binding_ref = _ref_dict(compute_request_guard_runtime_binding_ref(binding))
    manifest["runtime_binding_ref"] = binding_ref
    manifest["scope_manifest_hash"] = binding.scope_manifest_hash
    now = datetime(2026, 11, 4, 0, 30, tzinfo=UTC)
    observation = CandidateGuardAuditObservation(
        request_guard_decision_id=binding.request_guard_decision_id,
        runtime_binding_ref=binding_ref,
        user_id=user_id,
        prescription_version_id=uuid4(),
        prescription_version_medication_id=uuid4(),
        request_operation_code=operation,
        search_id=None,
        candidate_search_result_id=None,
        runtime_environment_id=uuid4(),
        environment_revision=1,
        governance_revision_ref="synthetic-v1",
        safety_epoch=1,
        candidate_index_version_id=uuid4(),
        candidate_index_artifact_sha256="a" * 64,
        query_digest="b" * 64,
        resolver_policy_sha256="a" * 64,
        retention_policy_sha256="a" * 64,
        proof_manifest_sha256=canonical_candidate_guard_proof_hash(manifest),
        checked_at=now,
        valid_until=None,
        outcome="FAIL",
        reason_codes=("APPROVAL_EXPIRED",),
    )
    return CandidateGuardAuditEnvelope(
        guard=guard,
        sources=(source,),
        members=(),
        binding=binding,
        observation=observation,
        proof_manifest=manifest,
    )


def test_envelope_freezes_proof_and_rejects_missing_decision() -> None:
    envelope = _envelope()
    proof = envelope.proof_manifest
    proof["targets"].clear()
    assert len(envelope.proof_manifest["targets"]) == 1
    with pytest.raises(CandidateGuardAuditValidationError):
        CandidateGuardAuditEnvelope(
            guard=envelope.guard,
            sources=(),
            members=(),
            binding=envelope.binding,
            observation=envelope.observation,
            proof_manifest=envelope.proof_manifest,
        )


@pytest.mark.asyncio
async def test_denial_written_only_after_existing_operation_survives_replay_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = _envelope()
    coordinator = CandidateGuardAuditCoordinator(_session())
    write_mock = AsyncMock(return_value="a" * 64)
    monkeypatch.setattr(coordinator, "_revalidate_denial_parent", AsyncMock())
    monkeypatch.setattr(coordinator, "_write", write_mock)

    async def denied() -> None:
        raise CandidateGuardPolicyDenied(envelope)

    result = await coordinator.execute_mutation(denied)
    assert isinstance(result, CandidateAuditStopResult)
    assert result.status_code == 503
    assert result.error_response(trace_id="synthetic-trace").code == "CANDIDATE_RESOLVER_UNAVAILABLE"
    write_mock.assert_awaited_once_with(envelope)

    write_mock.reset_mock()

    async def replay() -> str:
        return "winner receipt"

    assert await coordinator.execute_mutation(replay) == "winner receipt"
    write_mock.assert_not_awaited()

    async def unrelated_error() -> None:
        raise ApiError(status_code=409, code="IDENTIFICATION_CONTEXT_STALE", message="stale")

    with pytest.raises(ApiError):
        await coordinator.execute_mutation(unrelated_error)
    write_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_parent_404_blocks_denial_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    envelope = _envelope()
    coordinator = CandidateGuardAuditCoordinator(_session())
    write_mock = AsyncMock()
    monkeypatch.setattr(
        coordinator,
        "_candidate",
        SimpleNamespace(get_medication_for_candidate_search_owned=AsyncMock(return_value=None)),
    )
    monkeypatch.setattr(coordinator, "_write", write_mock)

    async def denied() -> None:
        raise CandidateGuardPolicyDenied(envelope)

    with pytest.raises(ApiError) as raised:
        await coordinator.execute_mutation(denied)
    assert raised.value.status_code == 404
    write_mock.assert_not_awaited()
