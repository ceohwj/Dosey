from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.guide_request_authority import (
    GuideRequestAuthorityEvaluation,
    GuideRequestAuthorityIssuer,
    GuideRequestAuthorityProviderFactory,
    GuideRequestAuthorityRejected,
)
from rag_runtime.request_authority import RequestAuthorityDecisionOutcome


class _Evaluator:
    def __init__(self, outcome=RequestAuthorityDecisionOutcome.PASS):
        self.outcome = outcome
        self.requests = []

    async def evaluate(self, *, session, request):
        self.requests.append(request)
        return GuideRequestAuthorityEvaluation(self.outcome, ("GUIDE_SYNC_ANSWER",))


@pytest.fixture
def authority_fakes(monkeypatch):  # noqa: C901 - fixture contains isolated repository doubles
    import app.services.guide_request_authority as module

    user = SimpleNamespace(id=uuid4())
    guide = SimpleNamespace(id=uuid4(), profile_id=uuid4(), prescription_id=uuid4(), prescription_version_id=uuid4())
    prescription = SimpleNamespace(
        id=guide.prescription_id, profile_id=guide.profile_id, active_version_id=guide.prescription_version_id
    )
    environment = SimpleNamespace(
        id=uuid4(),
        environment_code="LOCAL",
        environment_status=module.RagRuntimeEnvironmentStatus.ACTIVE,
        environment_revision=1,
        active_bundle_id=uuid4(),
        active_bundle_manifest_hash="a" * 64,
    )
    bundle = SimpleNamespace(
        id=environment.active_bundle_id,
        bundle_manifest_hash=environment.active_bundle_manifest_hash,
        environment_code="LOCAL",
        execution_manifest_id=uuid4(),
        bundle_status=module.RagRuntimeBundleStatus.READY,
    )
    manifest = SimpleNamespace(id=bundle.execution_manifest_id, manifest_hash="b" * 64)
    binding = SimpleNamespace(
        id=uuid4(),
        manifest_hash="c" * 64,
        runtime_release_bundle_id=bundle.id,
        runtime_release_bundle_manifest_hash=bundle.bundle_manifest_hash,
        runtime_execution_manifest_id=manifest.id,
        runtime_execution_manifest_hash=manifest.manifest_hash,
    )
    state = SimpleNamespace(
        user=user,
        guide=guide,
        prescription=prescription,
        environment=environment,
        bundle=bundle,
        manifest=manifest,
        bindings=[binding],
        observations={},
        verified=True,
    )

    class _Guides:
        def __init__(self, session):
            pass

        async def get_owned(self, *, guide_id, user_id):
            return guide if guide_id == guide.id and user_id == user.id else None

        async def get_prescription_owned(self, *, prescription_id, user_id):
            return state.prescription if prescription_id == guide.prescription_id and user_id == user.id else None

    class _Runtime:
        def __init__(self, session):
            pass

        async def get_environment_by_code(self, code):
            return state.environment if code == "LOCAL" else None

        async def get_release_bundle_by_id(self, id):
            return state.bundle if id == state.bundle.id else None

        async def get_execution_manifest_by_id(self, id):
            return state.manifest if id == state.manifest.id else None

        async def list_guide_retrieval_binding_manifests_for_runtime(self, **kwargs):
            return state.bindings

    class _Authority:
        def __init__(self, session):
            pass

        async def record_request_guard_authority(self, record):
            from rag_runtime.request_authority import compute_request_guard_authority_ref

            return compute_request_guard_authority_ref(
                user_id=record.user_id,
                request_operation_code=record.request_operation_code,
                decision_stage=record.decision_stage,
            )

    class _Binding:
        def __init__(self, session):
            pass

        async def record(self, observation):
            from rag_runtime.request_guard_runtime_binding import compute_request_guard_runtime_binding_ref

            ref = compute_request_guard_runtime_binding_ref(observation)
            state.observations[ref] = observation
            return ref

        async def get_exact(self, ref):
            return state.observations.get(ref)

    async def _verified(session, bundle_id):
        return state.verified

    async def _binding(session, id, hash):
        return binding if binding in state.bindings and id == binding.id and hash == binding.manifest_hash else None

    monkeypatch.setattr(module, "GuideRepository", _Guides)
    monkeypatch.setattr(module, "RagRuntimeRepository", _Runtime)
    monkeypatch.setattr(module, "RagRequestAuthorityRepository", _Authority)
    monkeypatch.setattr(module, "RagRequestGuardRuntimeBindingRepository", _Binding)
    monkeypatch.setattr(module, "verify_persisted_bundle_manifest_hash", _verified)
    monkeypatch.setattr(module, "load_verified_guide_retrieval_binding_manifest", _binding)
    return state


@pytest.mark.asyncio
async def test_each_request_issues_distinct_pass_and_revalidates_exact_ref(authority_fakes):
    state = authority_fakes
    evaluator = _Evaluator()
    issuer = GuideRequestAuthorityIssuer(object(), environment_code="LOCAL", evaluator=evaluator)
    first = await issuer.resolve(user=state.user, guide=state.guide)
    second = await issuer.resolve(user=state.user, guide=state.guide)
    assert first.request_guard_runtime_binding_ref != second.request_guard_runtime_binding_ref
    assert first.runtime_guard_decision_ref != second.runtime_guard_decision_ref
    assert len(evaluator.requests) == 2
    assert evaluator.requests[0].guide_id == state.guide.id
    assert evaluator.requests[0].prescription_version_id == state.guide.prescription_version_id
    assert await issuer.revalidate(user=state.user, guide=state.guide, snapshot=first)
    assert len(state.observations) == 2
    assert not await issuer.revalidate(
        user=state.user,
        guide=state.guide,
        snapshot=replace(first, request_guard_runtime_binding_ref=second.request_guard_runtime_binding_ref),
    )


@pytest.mark.asyncio
async def test_fail_is_recorded_but_never_returns_snapshot(authority_fakes):
    state = authority_fakes
    issuer = GuideRequestAuthorityIssuer(
        object(), environment_code="LOCAL", evaluator=_Evaluator(RequestAuthorityDecisionOutcome.FAIL)
    )
    with pytest.raises(GuideRequestAuthorityRejected):
        await issuer.resolve(user=state.user, guide=state.guide)
    assert len(state.observations) == 1
    assert next(iter(state.observations.values())).actual_decision_outcome is RequestAuthorityDecisionOutcome.FAIL


@pytest.mark.asyncio
async def test_ambiguous_binding_fails_before_evaluation(authority_fakes):
    from app.services.guide_sync_runtime_lifecycle import GuideSyncRuntimePreparationError

    state = authority_fakes
    state.bindings.append(state.bindings[0])
    evaluator = _Evaluator()
    issuer = GuideRequestAuthorityIssuer(object(), environment_code="LOCAL", evaluator=evaluator)
    with pytest.raises(GuideSyncRuntimePreparationError):
        await issuer.resolve(user=state.user, guide=state.guide)
    assert evaluator.requests == []
    assert state.observations == {}


@pytest.mark.asyncio
async def test_revalidate_fails_closed_on_bundle_change_or_new_fail(authority_fakes):
    state = authority_fakes
    evaluator = _Evaluator()
    issuer = GuideRequestAuthorityIssuer(object(), environment_code="LOCAL", evaluator=evaluator)
    snapshot = await issuer.resolve(user=state.user, guide=state.guide)
    evaluator.outcome = RequestAuthorityDecisionOutcome.FAIL
    assert not await issuer.revalidate(user=state.user, guide=state.guide, snapshot=snapshot)
    evaluator.outcome = RequestAuthorityDecisionOutcome.PASS
    state.environment.environment_revision += 1
    assert not await issuer.revalidate(user=state.user, guide=state.guide, snapshot=snapshot)
    assert len(state.observations) == 1


@pytest.mark.asyncio
async def test_missing_evaluator_and_changed_version_never_issue_guard(authority_fakes):
    from app.services.guide_sync_runtime_lifecycle import GuideSyncRuntimePreparationError

    state = authority_fakes
    issuer = GuideRequestAuthorityProviderFactory(environment_code="LOCAL", evaluator=None).for_session(object())
    with pytest.raises(GuideSyncRuntimePreparationError, match="authority is unavailable"):
        await issuer.resolve(user=state.user, guide=state.guide)
    assert state.observations == {}

    evaluator = _Evaluator()
    state.prescription.active_version_id = uuid4()
    issuer = GuideRequestAuthorityIssuer(object(), environment_code="LOCAL", evaluator=evaluator)
    with pytest.raises(GuideSyncRuntimePreparationError):
        await issuer.resolve(user=state.user, guide=state.guide)
    assert evaluator.requests == []


@pytest.mark.asyncio
async def test_invalid_evaluation_never_issues_guard(authority_fakes):
    from app.services.guide_sync_runtime_lifecycle import GuideSyncRuntimePreparationError

    state = authority_fakes

    class _Invalid:
        async def evaluate(self, *, session, request):
            return GuideRequestAuthorityEvaluation(RequestAuthorityDecisionOutcome.PASS, ("z", "a"))

    issuer = GuideRequestAuthorityIssuer(object(), environment_code="LOCAL", evaluator=_Invalid())
    with pytest.raises(GuideSyncRuntimePreparationError):
        await issuer.resolve(user=state.user, guide=state.guide)
    assert state.observations == {}
