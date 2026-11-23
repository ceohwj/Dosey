import copy
import json
from pathlib import Path

import pytest

from rag_runtime.candidate_guard_audit import (
    CandidateGuardAuditValidationError,
    canonical_candidate_guard_proof_hash,
)

VECTOR = json.loads(
    (
        Path(__file__).parents[3]
        / "docs/contracts/targets/post-mvp-1/candidate-request-guard-audit-v1.hash-vector.json"
    ).read_text()
)


def test_frozen_proof_vector():
    assert canonical_candidate_guard_proof_hash(VECTOR["hash_input"]) == VECTOR["canonical_utf8_sha256"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"unexpected": "x"}),
        lambda value: value["targets"][0]["reason_codes"].append("APPROVAL_EXPIRED"),
        lambda value: value["targets"][0]["source"].update(source_code="e\u0301"),
        lambda value: value["targets"][0]["freshness"].update(anchor_at="2026-11-04T00:00:00Z"),
        lambda value: value["targets"][0]["approval"].update(id="ABCDEF00-0000-4000-8000-000000000002"),
        lambda value: value["targets"][0].update(outcome="PASS"),
    ],
)
def test_rejects_noncanonical_or_inconsistent_proof(mutation):
    manifest = copy.deepcopy(VECTOR["hash_input"])
    mutation(manifest)
    with pytest.raises(CandidateGuardAuditValidationError):
        canonical_candidate_guard_proof_hash(manifest)


def _observation(**changes):
    from datetime import UTC, datetime, timedelta
    from uuid import UUID

    from rag_runtime.candidate_guard_audit import CandidateGuardAuditObservation

    now = datetime(2026, 11, 4, tzinfo=UTC)
    id_ = UUID("00000000-0000-4000-8000-000000000001")
    values = dict(
        request_guard_decision_id=id_,
        runtime_binding_ref=VECTOR["hash_input"]["runtime_binding_ref"],
        user_id=id_,
        prescription_version_id=id_,
        prescription_version_medication_id=id_,
        request_operation_code="medication-candidate.search.create",
        search_id=id_,
        candidate_search_result_id=None,
        runtime_environment_id=id_,
        environment_revision=1,
        governance_revision_ref="synthetic",
        safety_epoch=1,
        candidate_index_version_id=id_,
        candidate_index_artifact_sha256="a" * 64,
        query_digest="b" * 64,
        resolver_policy_sha256="a" * 64,
        retention_policy_sha256="a" * 64,
        proof_manifest_sha256=VECTOR["canonical_utf8_sha256"],
        checked_at=now,
        valid_until=now + timedelta(minutes=1),
        outcome="PASS",
        reason_codes=(),
    )
    values.update(changes)
    return CandidateGuardAuditObservation(**values)


def test_extension_projection_is_closed_and_hash_stable():
    from rag_runtime.candidate_guard_audit import candidate_guard_audit_projection, compute_candidate_guard_audit_hash

    observation = _observation()
    projection = candidate_guard_audit_projection(observation)
    assert set(projection) == {
        "projection_version",
        "request_guard_decision_id",
        "runtime_binding_ref",
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
        "resolver_policy_sha256",
        "retention_policy_sha256",
        "proof_manifest_sha256",
        "checked_at",
        "valid_until",
        "outcome",
        "reason_codes",
    }
    assert len(compute_candidate_guard_audit_hash(observation)) == 64


@pytest.mark.parametrize(
    "changes",
    [
        {"outcome": "PASS", "valid_until": None},
        {"outcome": "FAIL", "reason_codes": ()},
        {"outcome": "PASS", "reason_codes": ("APPROVAL_EXPIRED",)},
        {"candidate_search_result_id": __import__("uuid").uuid4()},
        {"request_operation_code": "medication-candidate.confirm"},
        {"environment_revision": True},
    ],
)
def test_extension_rejects_invalid_semantics(changes):
    from rag_runtime.candidate_guard_audit import candidate_guard_audit_projection

    with pytest.raises(CandidateGuardAuditValidationError):
        candidate_guard_audit_projection(_observation(**changes))
