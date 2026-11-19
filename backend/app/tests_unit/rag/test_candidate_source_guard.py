from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.core.errors import ApiError
from app.services.candidate_runtime_binding import CandidateRuntimeBinding
from app.services.candidate_source_guard import (
    SyntheticCandidateContext,
    SyntheticCandidateSourceGuard,
    SyntheticSourcePolicy,
)
from app.services.rag.candidate_policy import CandidateStage, ResolverPolicy


def _context() -> SyntheticCandidateContext:
    return SyntheticCandidateContext(
        runtime_environment_id=uuid4(),
        source_policies=(SyntheticSourcePolicy(uuid4(), "SYNTHETIC_TEST", timedelta(minutes=10)),),
        resolver_policy=ResolverPolicy(
            policy_version="synthetic-test-v1",
            maximum_input_length=100,
            retrieval_limit=10,
            enable_dense=False,
            release_eligible=False,
            stage_weights=(
                (CandidateStage.PRODUCT_NAME_EXACT, 4.0),
                (CandidateStage.APPROVED_ALIAS_EXACT, 3.0),
                (CandidateStage.TRIGRAM_EDIT_DISTANCE, 2.0),
                (CandidateStage.DENSE_VECTOR, 1.0),
            ),
            rrf_k=10.0,
            minimum_relevance=0.75,
            minimum_margin=0.02,
            auto_select_stages=frozenset({CandidateStage.PRODUCT_NAME_EXACT}),
        ),
        expected_governance_revision_ref="synthetic-test-v1",
        expected_safety_epoch=1,
        search_ttl=timedelta(minutes=2),
        query_hmac_key="synthetic-query-hmac-key-at-least-32-characters",
    )


@pytest.mark.parametrize("change", ["dense", "release", "real_policy", "governance", "empty", "ttl", "key"])
def test_context_has_no_production_or_missing_policy_fallback(change):
    context = _context()
    delta = {
        "dense": {"resolver_policy": replace(context.resolver_policy, enable_dense=True)},
        "release": {"resolver_policy": replace(context.resolver_policy, release_eligible=True)},
        "real_policy": {"resolver_policy": replace(context.resolver_policy, policy_version="production")},
        "governance": {"expected_governance_revision_ref": "production"},
        "empty": {"source_policies": ()},
        "ttl": {"search_ttl": timedelta(0)},
        "key": {"query_hmac_key": "short"},
    }
    with pytest.raises(ValueError):
        replace(context, **delta[change])


def test_query_digest_binds_all_policy_content_and_key_without_plaintext():
    context = _context()
    digest = context.query_digest("합성제품", "10mg")
    assert len(digest) == 64
    assert digest == context.query_digest("합성제품", "10mg")
    assert digest != context.query_digest("합성제품", "20mg")
    assert digest != replace(context, search_ttl=timedelta(seconds=1)).query_digest("합성제품", "10mg")
    assert digest != replace(context, query_hmac_key="another-synthetic-query-key-at-least-32-chars").query_digest(
        "합성제품", "10mg"
    )
    changed_policy = replace(context.resolver_policy, minimum_relevance=0.76)
    assert digest != replace(context, resolver_policy=changed_policy).query_digest("합성제품", "10mg")


def test_synthetic_allowlist_and_period_are_explicit():
    with pytest.raises(ValueError):
        SyntheticSourcePolicy(uuid4(), "MFDS_REAL_SOURCE", timedelta(days=1))
    with pytest.raises(ValueError):
        SyntheticSourcePolicy(uuid4(), "SYNTHETIC_TEST", timedelta(0))
    source = _context().source_policies[0]
    assert source.scope_policy_hash != source.freshness_policy_hash
    assert source.freshness_policy_hash != replace(source, maximum_age=timedelta(seconds=1)).freshness_policy_hash


def _facts(context):
    now = datetime.now(UTC)
    policy = context.source_policies[0]
    source = SimpleNamespace(source_code=policy.source_code, lifecycle_status="ACTIVE")
    endpoint = SimpleNamespace(lifecycle_status="VERIFIED", runtime_status="ENABLED", acquisition_status="APPROVED")
    operation = SimpleNamespace(runtime_status="ENABLED", acquisition_status="APPROVED")
    snapshot = SimpleNamespace(
        id=policy.source_snapshot_id,
        verification_status="CURRENT",
        source_version="synthetic-source-v1",
        canonical_checksum="a" * 64,
        collected_at=now - timedelta(minutes=2),
        verified_at=now - timedelta(minutes=1),
        verification_seal_id=uuid4(),
        effective_at=now - timedelta(minutes=1),
        rejected_record_count=0,
    )
    pin = SimpleNamespace(
        source_snapshot_id=snapshot.id,
        source_version=snapshot.source_version,
        canonical_checksum=snapshot.canonical_checksum,
        approval_version="synthetic-approval-v1",
        source_purpose="CATALOG",
        required=True,
        selected_for_operation=True,
        scope_policy_hash=policy.scope_policy_hash,
        freshness_policy_hash=policy.freshness_policy_hash,
    )
    publication = SimpleNamespace(verified_at=now - timedelta(minutes=1))
    approval = SimpleNamespace(
        id=uuid4(),
        valid_from=now - timedelta(minutes=5),
        expires_at=now + timedelta(seconds=30),
        revoked_at=None,
    )
    return now, (source, endpoint, operation, snapshot), pin, publication, approval


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["valid", "expiry_equal", "expired", "future", "revoked", "stale", "hash"])
async def test_source_validity_half_open_and_freshness(condition):
    context = _context()
    now, row, pin, publication, approval = _facts(context)
    if condition == "expiry_equal":
        approval.expires_at = now
    elif condition == "expired":
        approval.expires_at = now - timedelta(microseconds=1)
    elif condition == "future":
        approval.valid_from = now + timedelta(microseconds=1)
    elif condition == "revoked":
        approval.revoked_at = now - timedelta(seconds=1)
    elif condition == "stale":
        row[3].verified_at = now - context.source_policies[0].maximum_age
        row[3].collected_at = row[3].verified_at
    elif condition == "hash":
        pin.canonical_checksum = "b" * 64
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(one_or_none=Mock(return_value=row))),
        scalar=AsyncMock(side_effect=[publication, publication, approval, now]),
    )
    guard = SyntheticCandidateSourceGuard(session, context)
    if condition == "valid":
        facts, deadline = await guard._check_source(pin, context.source_policies[0])
        assert facts["approval_id"] == str(approval.id)
        assert deadline == approval.expires_at
    else:
        with pytest.raises(ApiError) as error:
            await guard._check_source(pin, context.source_policies[0])
        assert error.value.status_code == 503


def test_scope_does_not_upgrade_other_operations_or_changed_inputs():
    context = _context()
    medication = SimpleNamespace(id=uuid4(), medication_name="합성제품", strength_text="10mg")
    binding = CandidateRuntimeBinding(uuid4(), "a" * 64, context.runtime_environment_id, 1)
    snapshot = SimpleNamespace(version=SimpleNamespace(id=uuid4()))
    guard = SyntheticCandidateSourceGuard(SimpleNamespace(), context)
    digest = context.query_digest(medication.medication_name, medication.strength_text)
    guard._check_scope(binding, snapshot, medication, "create", digest, None)
    for operation, changed in [("guide", digest), ("create", "b" * 64)]:
        with pytest.raises(ApiError) as error:
            guard._check_scope(binding, snapshot, medication, operation, changed, None)
        assert error.value.status_code == 409
