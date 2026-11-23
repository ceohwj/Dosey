"""The synthetic coordinator keeps the public boundary closed and serializes Search work."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.core import config
from app.core.errors import ApiError
from app.dtos.medication_candidates import (
    ConfirmMedicationCandidateRequest,
    CreateMedicationCandidateSearchRequest,
    MedicationCandidateSearchData,
)
from app.dtos.medication_candidates import (
    MedicationCandidateSearchStatus as DtoSearchStatus,
)
from app.models.rag_candidate import MedicationCandidateSearchStatus
from app.services.candidate_runtime_binding import CandidateRuntimeBinding
from app.services.candidate_search import CandidateSearchService
from app.services.candidate_search_resolution import CandidateSearchResolution
from app.services.idempotency import SyncMutationResult


def _service(*, context: object | None = None) -> tuple[CandidateSearchService, AsyncMock, AsyncMock]:
    repository = AsyncMock()
    repository.session = AsyncMock()
    identification = AsyncMock()
    service = CandidateSearchService(repository, identification, AsyncMock(), context=context)  # type: ignore[arg-type]
    return service, repository, identification


@pytest.mark.asyncio
async def test_default_context_denies_create_before_repositories() -> None:
    service, repository, _ = _service()
    with pytest.raises(ApiError) as raised:
        await service.create_candidate_search(
            user=SimpleNamespace(id=uuid4()),  # type: ignore[arg-type]
            prescription_version_medication_id=uuid4(),
        )
    assert raised.value.status_code == 503
    assert raised.value.code == "CANDIDATE_RESOLVER_UNAVAILABLE"
    repository.get_medication_for_candidate_search_owned.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_success_replays_before_current_context_guard_and_fresh_mutation_fails_closed() -> None:
    service, repository, _ = _service()
    search_id, result_id, medication_id, user_id = uuid4(), uuid4(), uuid4(), uuid4()
    repository.get_result_selection_owned.return_value = SimpleNamespace(
        search=SimpleNamespace(
            id=search_id,
            prescription_version_medication_id=medication_id,
            runtime_release_bundle_id=None,
            runtime_release_bundle_manifest_hash=None,
            runtime_environment_id=None,
            runtime_environment_revision=None,
        ),
        result=SimpleNamespace(id=result_id),
    )
    execute_mock = cast(AsyncMock, service._idempotency_service.execute)
    response = {
        "identification_id": str(uuid4()),
        "prescription_version_medication_id": str(medication_id),
        "status": "MATCHED",
        "source": "USER_SELECTED",
        "product_id": str(uuid4()),
        "confirmed_at": datetime.now(config.TIMEZONE).isoformat(),
    }
    execute_mock.return_value = SyncMutationResult(200, response, is_replay=True)
    request = ConfirmMedicationCandidateRequest(
        prescription_version_medication_id=medication_id,
        candidate_search_result_id=result_id,
    )
    replay = await service.confirm_candidate(
        user=SimpleNamespace(id=user_id),  # type: ignore[arg-type]
        request=request,
        idempotency_key="synthetic-replay-key-0001",
    )
    assert str(replay.identification_id) == response["identification_id"]
    assert execute_mock.await_args is not None
    assert execute_mock.await_args.kwargs["mutate"] is not None
    repository.get_result_selection_for_update_owned.assert_not_awaited()

    async def execute_mutation(**kwargs: object) -> object:
        await kwargs["mutate"]()  # type: ignore[operator]
        raise AssertionError("fresh mutation must fail closed before a success response")

    execute_mock.side_effect = execute_mutation
    with pytest.raises(ApiError) as raised:
        await service.confirm_candidate(
            user=SimpleNamespace(id=user_id),  # type: ignore[arg-type]
            request=request,
            idempotency_key="synthetic-fresh-key-0001",
        )
    assert raised.value.status_code == 503
    assert raised.value.code == "CANDIDATE_RESOLVER_UNAVAILABLE"
    repository.get_result_selection_for_update_owned.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_post_flag_blocks_before_service_even_with_valid_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.apis.v1.medication_candidate_routers import create_medication_candidate_search

    monkeypatch.setattr(config, "PUBLIC_TRACK_F_ENABLED", False)
    service = AsyncMock()
    with pytest.raises(ApiError) as raised:
        await create_medication_candidate_search(
            CreateMedicationCandidateSearchRequest(prescription_version_medication_id=uuid4()),
            SimpleNamespace(id=uuid4()),  # type: ignore[arg-type]
            service,
        )
    assert raised.value.status_code == 503
    assert raised.value.details[0].reason == "PUBLIC_TRACK_F_DISABLED"
    service.create_candidate_search.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_locks_parent_and_active_search_before_runtime_and_finalizes_in_one_call() -> None:
    user = SimpleNamespace(id=uuid4())
    medication = SimpleNamespace(id=uuid4(), medication_name="합성약", strength_text="1mg", display_order=1)
    bundle_id, environment_id, index_id = uuid4(), uuid4(), uuid4()
    query_digest = "a" * 64
    context = SimpleNamespace(
        runtime_environment_id=environment_id,
        resolver_policy=object(),
        query_digest=lambda **_: query_digest,
    )
    service, repository, identification = _service(context=context)
    search = SimpleNamespace(id=uuid4(), status=MedicationCandidateSearchStatus.RUNNING)
    repository.get_medication_for_candidate_search_owned.return_value = medication
    repository.get_active_search_for_update.return_value = None
    identification.record_candidate_search.return_value = SimpleNamespace(search=search, is_reused=False)
    binding = CandidateRuntimeBinding(bundle_id, "b" * 64, environment_id, 1)
    snapshot = SimpleNamespace(version=SimpleNamespace(id=index_id))
    events: list[str] = []

    async def medication_read(**_: object) -> object:
        events.append("parent")
        return medication

    async def active_search_read(**_: object) -> None:
        events.append("search")

    async def runtime_read(**_: object) -> object:
        events.append("runtime")
        return binding, snapshot

    async def search_record(**_: object) -> object:
        events.append("record")
        return SimpleNamespace(search=search, is_reused=False)

    async def resolver(*_: object, **__: object) -> CandidateSearchResolution:
        events.append("resolve")
        return CandidateSearchResolution(MedicationCandidateSearchStatus.NO_CANDIDATE, None, [])

    async def final_guard(**_: object) -> None:
        events.append("final-guard")

    async def finalize(**_: object) -> None:
        events.append("finalize")

    repository.get_medication_for_candidate_search_owned.side_effect = medication_read
    repository.get_active_search_for_update.side_effect = active_search_read
    service._binding_and_index = AsyncMock(side_effect=runtime_read)  # type: ignore[method-assign]
    service._validate_search = AsyncMock(side_effect=final_guard)  # type: ignore[method-assign]
    identification.record_candidate_search.side_effect = search_record
    identification.finalize_candidate_search.side_effect = finalize
    receipt = SimpleNamespace(expires_at=datetime.now(config.TIMEZONE) + timedelta(minutes=2))
    expected = MedicationCandidateSearchData(
        search_id=search.id,
        prescription_version_medication_id=medication.id,
        medication_index=1,
        status=DtoSearchStatus.NO_CANDIDATE,
        candidate_search_result_id=None,
        candidate=None,
        expires_at=receipt.expires_at,
    )
    with (
        patch("app.services.candidate_search.SyntheticCandidateSourceGuard") as guard_class,
        patch("app.services.candidate_search.resolve_synthetic_candidate_search", side_effect=resolver),
        patch(
            "app.services.medication_candidates.MedicationCandidateService.get_candidate_search", new_callable=AsyncMock
        ) as get_search,
    ):
        guard_class.return_value.validate = AsyncMock(return_value=receipt)
        get_search.return_value = expected
        actual = await service.create_candidate_search(
            user=user,  # type: ignore[arg-type]
            prescription_version_medication_id=medication.id,
        )

    assert actual == expected
    assert events == ["parent", "search", "runtime", "record", "resolve", "final-guard", "finalize"]
    identification.record_candidate_search.assert_awaited_once_with(
        prescription_version_medication_id=medication.id,
        user_id=user.id,
        query_digest=query_digest,
        runtime_release_bundle_id=bundle_id,
        candidate_index_version_id=index_id,
        expires_at=receipt.expires_at,
        runtime_environment_id=environment_id,
    )
    guard_class.return_value.validate.assert_awaited_once()


@pytest.mark.asyncio
async def test_reused_ready_search_skips_resolution_and_finalization() -> None:
    medication = SimpleNamespace(id=uuid4(), medication_name="합성약", strength_text=None)
    user = SimpleNamespace(id=uuid4())
    environment_id = uuid4()
    context = SimpleNamespace(
        runtime_environment_id=environment_id,
        resolver_policy=object(),
        query_digest=lambda **_: "c" * 64,
    )
    service, repository, identification = _service(context=context)
    repository.get_medication_for_candidate_search_owned.return_value = medication
    search = SimpleNamespace(id=uuid4(), status=MedicationCandidateSearchStatus.READY)
    identification.record_candidate_search.return_value = SimpleNamespace(search=search, is_reused=True)
    service._binding_and_index = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            CandidateRuntimeBinding(uuid4(), "b" * 64, environment_id, 1),
            SimpleNamespace(version=SimpleNamespace(id=uuid4())),
        )
    )
    service._validate_search = AsyncMock()  # type: ignore[method-assign]
    expected = MedicationCandidateSearchData(
        search_id=search.id,
        prescription_version_medication_id=medication.id,
        medication_index=1,
        status=DtoSearchStatus.READY,
        candidate_search_result_id=None,
        candidate=None,
        expires_at=None,
    )
    with (
        patch("app.services.candidate_search.SyntheticCandidateSourceGuard") as guard_class,
        patch("app.services.candidate_search.resolve_synthetic_candidate_search", new_callable=AsyncMock) as resolve,
        patch(
            "app.services.medication_candidates.MedicationCandidateService.get_candidate_search", new_callable=AsyncMock
        ) as get_search,
    ):
        guard_class.return_value.validate = AsyncMock(
            return_value=SimpleNamespace(expires_at=datetime.now(config.TIMEZONE))
        )
        get_search.return_value = expected
        actual = await service.create_candidate_search(user=user, prescription_version_medication_id=medication.id)  # type: ignore[arg-type]
    assert actual == expected
    resolve.assert_not_awaited()
    identification.finalize_candidate_search.assert_not_awaited()
    service._validate_search.assert_awaited_once_with(
        search=search, medication=medication, user_id=user.id, operation="read"
    )
