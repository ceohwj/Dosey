from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.models.async_jobs import AiJobStatus, AiJobType
from app.models.rag_runtime import RagRuntimeEnvironmentStatus
from app.services.guide_sync_runtime_lifecycle import GuideSyncRuntimePreparationError


def _request():
    return SimpleNamespace(
        guide_id=uuid4(),
        job_id=uuid4(),
        prescription_version_id=uuid4(),
        runtime_environment_id=uuid4(),
        runtime_environment_revision=1,
        runtime_release_bundle_id=uuid4(),
        runtime_release_bundle_manifest_hash="a" * 64,
        runtime_execution_manifest_id=uuid4(),
        runtime_execution_manifest_hash="b" * 64,
        identifications=(),
    )


@pytest.mark.asyncio
async def test_foreign_or_missing_prescription_fails_closed_before_job_lock():
    from app.services.guide_sync_runtime_finalizer import GuideSyncRuntimeFinalizer

    session = SimpleNamespace(scalar=AsyncMock(side_effect=[object(), None]), expire_all=Mock())
    request = _request()
    with pytest.raises(GuideSyncRuntimePreparationError):
        await GuideSyncRuntimeFinalizer(session).lock_and_check(
            user_id=uuid4(), guide_id=request.guide_id, request=request
        )
    assert session.scalar.await_count == 2


@pytest.mark.asyncio
async def test_missing_user_fails_before_prescription_lock():
    from app.services.guide_sync_runtime_finalizer import GuideSyncRuntimeFinalizer

    request = _request()
    session = SimpleNamespace(scalar=AsyncMock(return_value=None), expire_all=Mock())
    with pytest.raises(GuideSyncRuntimePreparationError):
        await GuideSyncRuntimeFinalizer(session).lock_and_check(
            user_id=uuid4(), guide_id=request.guide_id, request=request
        )
    assert session.scalar.await_count == 1
    assert 'FOR SHARE OF "user"' in str(session.scalar.await_args.args[0].compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
async def test_changed_environment_pointer_returns_stale_without_publishing(monkeypatch):
    from app.services import guide_sync_runtime_finalizer as module

    request = _request()
    prescription = SimpleNamespace(id=uuid4(), active_version_id=request.prescription_version_id, profile_id=uuid4())
    job = SimpleNamespace(
        id=request.job_id,
        user_id=uuid4(),
        job_type=AiJobType.GUIDE,
        status=AiJobStatus.PROCESSING,
        prescription_version_id=request.prescription_version_id,
    )
    guide = SimpleNamespace(
        id=request.guide_id,
        prescription_id=prescription.id,
        profile_id=prescription.profile_id,
        ai_job_id=job.id,
        prescription_version_id=request.prescription_version_id,
    )
    environment = SimpleNamespace(
        id=request.runtime_environment_id,
        environment_status=RagRuntimeEnvironmentStatus.ACTIVE,
        environment_revision=2,
        active_bundle_id=request.runtime_release_bundle_id,
        active_bundle_manifest_hash=request.runtime_release_bundle_manifest_hash,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[object(), prescription, job, guide, environment]), expire_all=Mock()
    )
    monkeypatch.setattr(module, "load_verified_guide_runtime_request_carrier", AsyncMock(return_value=request))
    state = await module.GuideSyncRuntimeFinalizer(session).lock_and_check(
        user_id=job.user_id, guide_id=guide.id, request=request
    )
    assert state.guide is guide and state.job is job and state.is_current is False
    assert session.scalar.await_count == 5


@pytest.mark.asyncio
async def test_guide_identity_must_match_request_before_any_lock():
    from app.services.guide_sync_runtime_finalizer import GuideSyncRuntimeFinalizer

    session = SimpleNamespace(scalar=AsyncMock(), expire_all=Mock())
    request = _request()
    with pytest.raises(GuideSyncRuntimePreparationError):
        await GuideSyncRuntimeFinalizer(session).lock_and_check(user_id=uuid4(), guide_id=uuid4(), request=request)
    session.scalar.assert_not_awaited()
    session.expire_all.assert_not_called()


@pytest.mark.asyncio
async def test_terminal_job_cannot_finalize_again():
    from app.services.guide_sync_runtime_finalizer import GuideSyncRuntimeFinalizer

    request = _request()
    prescription = SimpleNamespace(id=uuid4(), profile_id=uuid4(), active_version_id=request.prescription_version_id)
    job = SimpleNamespace(
        id=request.job_id,
        user_id=uuid4(),
        job_type=AiJobType.GUIDE,
        status=AiJobStatus.COMPLETED,
        prescription_version_id=request.prescription_version_id,
    )
    guide = SimpleNamespace(
        id=request.guide_id,
        prescription_id=prescription.id,
        profile_id=prescription.profile_id,
        ai_job_id=job.id,
        prescription_version_id=request.prescription_version_id,
    )
    session = SimpleNamespace(scalar=AsyncMock(side_effect=[object(), prescription, job, guide]), expire_all=Mock())
    state = await GuideSyncRuntimeFinalizer(session).lock_and_check(
        user_id=job.user_id, guide_id=guide.id, request=request
    )
    assert state.is_current is False
    assert session.scalar.await_count == 4
    locked_sql = [str(call.args[0].compile(dialect=postgresql.dialect())) for call in session.scalar.await_args_list]
    assert 'FOR SHARE OF "user"' in locked_sql[0]
    assert "FOR UPDATE OF prescription" in locked_sql[1]
    assert "profile_type" in locked_sql[1]
    assert "FOR UPDATE OF ai_job" in locked_sql[2]
    assert "ai_job.user_id" in locked_sql[2]
    assert "FOR UPDATE OF guide" in locked_sql[3]


@pytest.mark.asyncio
async def test_exact_pinned_membership_is_required_for_current(monkeypatch):
    from app.services import guide_sync_runtime_finalizer as module

    request = _request()
    medication_id, identification_id = uuid4(), uuid4()
    request.identifications = (
        SimpleNamespace(
            prescription_version_medication_id=medication_id, medication_identification_id=identification_id
        ),
    )
    prescription = SimpleNamespace(id=uuid4(), profile_id=uuid4(), active_version_id=request.prescription_version_id)
    job = SimpleNamespace(
        id=request.job_id,
        user_id=uuid4(),
        job_type=AiJobType.GUIDE,
        status=AiJobStatus.PROCESSING,
        prescription_version_id=request.prescription_version_id,
    )
    guide = SimpleNamespace(
        id=request.guide_id,
        prescription_id=prescription.id,
        profile_id=prescription.profile_id,
        ai_job_id=job.id,
        prescription_version_id=request.prescription_version_id,
    )
    environment = SimpleNamespace(
        id=request.runtime_environment_id,
        environment_status=RagRuntimeEnvironmentStatus.ACTIVE,
        environment_revision=1,
        active_bundle_id=request.runtime_release_bundle_id,
        active_bundle_manifest_hash=request.runtime_release_bundle_manifest_hash,
    )
    manifest = SimpleNamespace(
        id=request.runtime_execution_manifest_id, manifest_hash=request.runtime_execution_manifest_hash
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[object(), prescription, job, guide, environment]),
        expire_all=Mock(),
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(scalar_one_or_none=lambda: manifest),
                SimpleNamespace(all=lambda: [(medication_id, identification_id)]),
            ]
        ),
    )
    monkeypatch.setattr(module, "load_verified_guide_runtime_request_carrier", AsyncMock(return_value=request))
    state = await module.GuideSyncRuntimeFinalizer(session).lock_and_check(
        user_id=job.user_id, guide_id=guide.id, request=request
    )
    assert state.is_current is True

    session.scalar = AsyncMock(side_effect=[object(), prescription, job, guide, environment])
    session.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(scalar_one_or_none=lambda: manifest),
            SimpleNamespace(all=lambda: [(medication_id, None)]),
        ]
    )
    state = await module.GuideSyncRuntimeFinalizer(session).lock_and_check(
        user_id=job.user_id, guide_id=guide.id, request=request
    )
    assert state.is_current is False
