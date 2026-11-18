from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import guide_sync_runtime_execution as execution


@pytest.mark.asyncio
async def test_intake_commits_before_execution_and_finalization_commits_after_revalidation(monkeypatch):
    events = []
    session = SimpleNamespace(
        commit=AsyncMock(side_effect=lambda: events.append("commit")), flush=AsyncMock(), get=AsyncMock()
    )
    guide = SimpleNamespace(id=uuid4(), prescription_id=uuid4(), prescription_version_id=uuid4(), profile_id=uuid4())
    user = SimpleNamespace(id=uuid4(), token_version=0, account_status=execution.AccountStatus.ACTIVE, is_active=True)
    session.get.return_value = user
    job = SimpleNamespace(status=None)
    repository = SimpleNamespace(
        session=session,
        lock_if_current_version=AsyncMock(return_value=True),
        mark_release_completed=AsyncMock(return_value=guide),
    )
    snapshot = SimpleNamespace()
    provider = SimpleNamespace(
        resolve=AsyncMock(return_value=snapshot),
        revalidate=AsyncMock(side_effect=lambda **kwargs: events.append("revalidate") or True),
    )
    lifecycle = SimpleNamespace(
        prepare=AsyncMock(return_value=SimpleNamespace(job=job, carrier=SimpleNamespace(job_id=uuid4())))
    )
    monkeypatch.setattr(execution.GuideSyncRuntimeAuthority, "from_snapshot", lambda snapshot: object(), raising=False)

    async def run(**kwargs):
        events.append("execute")
        assert events == ["commit", "execute"]
        return SimpleNamespace(release_decision=execution.GuideRuntimeReleaseDecision.PASS), SimpleNamespace(
            model_name="synthetic", prompt_version="synthetic"
        )

    monkeypatch.setattr(execution, "execute_verified_guide_runtime", run)
    monkeypatch.setattr(
        execution,
        "GuideSyncRuntimeFinalizer",
        lambda session: SimpleNamespace(
            lock_and_check=AsyncMock(return_value=SimpleNamespace(guide=guide, job=job, is_current=True))
        ),
        raising=False,
    )
    coordinator = execution.GuideSyncRuntimeExecution(
        repository=repository, lifecycle=lifecycle, executor_factory=object(), authority_provider=provider
    )
    assert await coordinator.execute(user=user, guide=guide) is guide
    assert events == ["commit", "execute", "revalidate", "commit"]
    assert coordinator.timings.execution_seconds >= 0
    assert lifecycle.prepare.await_args.kwargs["snapshot"] is snapshot


@pytest.mark.asyncio
async def test_prepare_failure_rolls_back_partial_pass_authority(monkeypatch):
    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    guide = SimpleNamespace(id=uuid4(), prescription_id=uuid4(), prescription_version_id=uuid4(), profile_id=uuid4())
    provider = SimpleNamespace(resolve=AsyncMock(return_value=object()))
    lifecycle = SimpleNamespace(
        prepare=AsyncMock(side_effect=execution.GuideSyncRuntimePreparationError("unavailable"))
    )
    monkeypatch.setattr(execution.GuideSyncRuntimeAuthority, "from_snapshot", lambda snapshot: object())
    coordinator = execution.GuideSyncRuntimeExecution(
        repository=SimpleNamespace(session=session, lock_if_current_version=AsyncMock(return_value=True)),
        lifecycle=lifecycle,
        executor_factory=object(),
        authority_provider=provider,
    )
    coordinator._record_intake_failure = AsyncMock()
    with pytest.raises(execution.GuideSyncRuntimePreparationError):
        await coordinator.execute(user=SimpleNamespace(id=uuid4(), token_version=0), guide=guide)
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
    coordinator._record_intake_failure.assert_awaited_once()
