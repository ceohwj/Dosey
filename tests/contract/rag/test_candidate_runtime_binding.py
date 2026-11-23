from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.core.errors import ApiError
from app.models.rag_candidate import MedicationCandidateSearch
from app.repositories.medication_candidate_repository import MedicationCandidateRepository
from app.services.candidate_runtime_binding import CandidateRuntimeBinding, require_current_candidate_runtime
from app.services.medication_identification import MedicationIdentificationService


@pytest.fixture
def ids():
    return SimpleNamespace(environment=uuid4(), bundle=uuid4(), index=uuid4())


def runtime(ids, *, revision=1, status="ACTIVE", manifest_hash="a" * 64):
    environment = SimpleNamespace(
        id=ids.environment,
        environment_code="LOCAL",
        environment_status=status,
        environment_revision=revision,
        active_bundle_id=ids.bundle,
        active_bundle_manifest_hash=manifest_hash,
    )
    bundle = SimpleNamespace(
        id=ids.bundle,
        environment_code="LOCAL",
        bundle_manifest_hash=manifest_hash,
        bundle_status="READY",
    )
    repository = SimpleNamespace(
        lock_environment_by_id=AsyncMock(return_value=environment),
        get_release_bundle_by_id=AsyncMock(return_value=bundle),
        session=object(),
    )
    return repository


@pytest.mark.asyncio
async def test_binding_derives_actual_runtime_and_reuses_same_context(ids):
    repository = runtime(ids)
    binding = await require_current_candidate_runtime(
        repository,
        runtime_environment_id=ids.environment,
        expected_bundle_id=ids.bundle,
        verify_manifest=AsyncMock(return_value=True),
    )
    assert binding == CandidateRuntimeBinding(ids.bundle, "a" * 64, ids.environment, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("revision,bundle_id,hash_value", [(2, None, None), (1, uuid4(), None), (1, None, "b" * 64)])
async def test_change_and_aba_require_fresh_search(ids, revision, bundle_id, hash_value):
    repository = runtime(ids, revision=revision, manifest_hash=hash_value or "a" * 64)
    if bundle_id:
        repository.lock_environment_by_id.return_value.active_bundle_id = bundle_id
    old = CandidateRuntimeBinding(ids.bundle, "a" * 64, ids.environment, 1)
    with pytest.raises(ApiError) as error:
        await require_current_candidate_runtime(
            repository,
            runtime_environment_id=ids.environment,
            expected_binding=old,
            verify_manifest=AsyncMock(return_value=True),
        )
    assert error.value.status_code == 409
    assert error.value.code == "IDENTIFICATION_CONTEXT_STALE"


@pytest.mark.asyncio
async def test_unavailable_and_corrupt_runtime_fail_closed(ids):
    for status, valid in [("SUSPENDED", True), ("ACTIVE", False)]:
        repository = runtime(ids, status=status)
        with pytest.raises(ApiError) as error:
            await require_current_candidate_runtime(
                repository,
                runtime_environment_id=ids.environment,
                verify_manifest=AsyncMock(return_value=valid),
            )
        assert error.value.status_code == 503
        assert error.value.code == "CANDIDATE_RESOLVER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_missing_or_mismatched_bundle_fails_closed(ids):
    for defect in ("missing", "environment", "hash", "status"):
        repository = runtime(ids)
        bundle = repository.get_release_bundle_by_id.return_value
        if defect == "missing":
            repository.get_release_bundle_by_id.return_value = None
        elif defect == "environment":
            bundle.environment_code = "PRODUCTION"
        elif defect == "hash":
            bundle.bundle_manifest_hash = "b" * 64
        else:
            bundle.bundle_status = "FAILED"
        with pytest.raises(ApiError) as error:
            await require_current_candidate_runtime(
                repository,
                runtime_environment_id=ids.environment,
                verify_manifest=AsyncMock(return_value=True),
            )
        assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_missing_legacy_binding_fails_closed(ids):
    repository = runtime(ids)
    with pytest.raises(ApiError) as error:
        await require_current_candidate_runtime(
            repository,
            runtime_environment_id=ids.environment,
            expected_binding=CandidateRuntimeBinding(ids.bundle, None, None, None),
            verify_manifest=AsyncMock(return_value=True),
        )
    assert error.value.status_code == 409


def test_search_reuse_requires_exact_revision_even_after_aba(ids):
    search = SimpleNamespace(
        query_digest="digest",
        runtime_release_bundle_id=ids.bundle,
        candidate_index_version_id=ids.index,
        runtime_release_bundle_manifest_hash="a" * 64,
        runtime_environment_id=ids.environment,
        runtime_environment_revision=1,
    )
    same = CandidateRuntimeBinding(ids.bundle, "a" * 64, ids.environment, 1)
    changed = CandidateRuntimeBinding(ids.bundle, "a" * 64, ids.environment, 3)
    context = dict(query_digest="digest", runtime_release_bundle_id=ids.bundle, candidate_index_version_id=ids.index)
    assert MedicationIdentificationService._has_same_search_context(search, binding=same, **context)
    assert not MedicationIdentificationService._has_same_search_context(search, binding=changed, **context)


@pytest.mark.asyncio
async def test_historical_ownership_query_has_no_active_or_verified_conditions(ids):
    statements = []

    async def scalar(statement):
        statements.append(str(statement))
        return None

    repository = MedicationCandidateRepository(SimpleNamespace(scalar=scalar))
    assert await repository.get_result_selection_owned(candidate_search_result_id=uuid4(), user_id=uuid4()) is None
    assert len(statements) == 1
    assert "profile_type" in statements[0]
    assert "active_version_id" not in statements[0]
    assert "verification_status" not in statements[0]


def test_model_runtime_constraints_use_postgresql_hash_pattern():
    constraints = {item.name: item for item in MedicationCandidateSearch.__table__.constraints if item.name}
    expression = constraints["chk_medication_candidate_search_runtime_hash"].sqltext
    compiled = str(expression.compile(dialect=postgresql.dialect()))
    assert "[0-9a-f]{64}" in compiled
    assert "~" in compiled
    assert len(MedicationCandidateSearch.__table__.columns) > 0


@pytest.mark.asyncio
async def test_validate_runtime_checks_owned_selection_before_environment_lock(ids, monkeypatch):
    calls = []
    search = SimpleNamespace(
        runtime_release_bundle_id=ids.bundle,
        runtime_release_bundle_manifest_hash="a" * 64,
        runtime_environment_id=ids.environment,
        runtime_environment_revision=1,
        candidate_index_version_id=ids.index,
    )

    async def owned_selection(**_):
        calls.append("selection")
        return SimpleNamespace(search=search)

    runtime_repository = runtime(ids)

    async def lock(environment_id):
        calls.append("environment")
        return runtime_repository.lock_environment_by_id.return_value

    runtime_repository.lock_environment_by_id.side_effect = lock
    candidate_repository = SimpleNamespace(session=object(), get_result_selection_for_update_owned=owned_selection)
    monkeypatch.setattr("app.services.medication_identification.RagRuntimeRepository", lambda _: runtime_repository)

    async def validate(repository, **kwargs):
        return await require_current_candidate_runtime(
            repository, **kwargs, verify_manifest=AsyncMock(return_value=True)
        )

    monkeypatch.setattr("app.services.medication_identification.require_current_candidate_runtime", validate)
    await MedicationIdentificationService(candidate_repository).validate_candidate_runtime(
        candidate_search_result_id=uuid4(), user_id=uuid4()
    )
    assert calls == ["selection", "environment"]


@pytest.mark.asyncio
async def test_record_search_pins_runtime_after_search_lock_and_replaces_aba_search(ids, monkeypatch):
    calls = []
    old = SimpleNamespace(
        query_digest="digest",
        runtime_release_bundle_id=ids.bundle,
        candidate_index_version_id=ids.index,
        runtime_release_bundle_manifest_hash="a" * 64,
        runtime_environment_id=ids.environment,
        runtime_environment_revision=1,
        expires_at=None,
    )

    async def medication(**_):
        calls.append("prescription")
        return SimpleNamespace(medication_name="synthetic", strength_text=None)

    async def search(**_):
        calls.append("search")
        return old

    async def binding(*_, **__):
        calls.append("environment")
        return CandidateRuntimeBinding(ids.bundle, "a" * 64, ids.environment, 3)

    repository = SimpleNamespace(
        session=object(),
        get_medication_for_candidate_search_owned=medication,
        get_active_search_for_update=search,
        get_latest_identification=AsyncMock(return_value=None),
        invalidate_input_changed=AsyncMock(),
        create_search=AsyncMock(return_value=SimpleNamespace(id=uuid4())),
    )
    monkeypatch.setattr("app.services.medication_identification.RagRuntimeRepository", lambda _: object())
    monkeypatch.setattr("app.services.medication_identification.require_current_candidate_runtime", binding)
    result = await MedicationIdentificationService(repository).record_candidate_search(
        prescription_version_medication_id=uuid4(),
        user_id=uuid4(),
        query_digest="digest",
        runtime_release_bundle_id=ids.bundle,
        candidate_index_version_id=ids.index,
        expires_at=None,
        runtime_environment_id=ids.environment,
    )
    assert not result.is_reused
    assert calls == ["prescription", "search", "environment"]
    repository.invalidate_input_changed.assert_awaited_once()
    assert repository.create_search.await_args.kwargs["runtime_environment_revision"] == 3

    old.runtime_environment_revision = 3
    repository.invalidate_input_changed.reset_mock()
    repository.create_search.reset_mock()
    reused = await MedicationIdentificationService(repository).record_candidate_search(
        prescription_version_medication_id=uuid4(),
        user_id=uuid4(),
        query_digest="digest",
        runtime_release_bundle_id=ids.bundle,
        candidate_index_version_id=ids.index,
        expires_at=None,
        runtime_environment_id=ids.environment,
    )
    assert reused.is_reused
    assert reused.search is old
    repository.invalidate_input_changed.assert_not_awaited()
    repository.create_search.assert_not_awaited()
