"""Synchronous 201 orchestration with committed intake and finalization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.async_jobs import AiJobStatus
from app.models.guides import Guide, GuideGenerationStatus
from app.models.users import AccountStatus, User
from app.repositories.guide_repository import GuideRepository
from app.services.guide_intake import GuideRuntimeContextSnapshot
from app.services.guide_request_authority import GuideRequestAuthorityRejected
from app.services.guide_runtime_execution import execute_verified_guide_runtime
from app.services.guide_runtime_request import GuideRuntimeRequestCarrier
from app.services.guide_sync_runtime_finalizer import GuideSyncRuntimeFinalizer
from app.services.guide_sync_runtime_lifecycle import (
    GuideSyncRuntimeAuthority,
    GuideSyncRuntimeLifecycleProducer,
    GuideSyncRuntimePreparationError,
)
from rag_runtime.guide_release_projection import (
    GUIDE_RUNTIME_RELEASE_PROJECTION_CARRIER_VERSION,
    GuideRuntimeApprovedFallback,
    GuideRuntimeFallbackCode,
    GuideRuntimeReleaseDecision,
    GuideRuntimeReleaseProjectionCarrier,
)
from rag_runtime.guide_runtime_execution import GuideRuntimeExecutorFactoryPort


class GuideSyncRuntimeAuthorityProvider(Protocol):
    async def resolve(self, *, user: User, guide: Guide) -> GuideRuntimeContextSnapshot: ...

    async def revalidate(self, *, user: User, guide: Guide, snapshot: GuideRuntimeContextSnapshot) -> bool: ...


class GuideSyncRuntimeAuthorityProviderFactory(Protocol):
    def for_session(self, session: AsyncSession) -> GuideSyncRuntimeAuthorityProvider: ...


class GuideSyncRuntimeVersionConflictError(RuntimeError):
    """A pinned execution authority changed before publication."""


@dataclass(frozen=True, slots=True)
class GuideSyncRuntimeTimings:
    intake_seconds: float = 0.0
    execution_seconds: float = 0.0
    finalize_seconds: float = 0.0


class GuideSyncRuntimeExecution:
    def __init__(
        self,
        *,
        repository: GuideRepository,
        lifecycle: GuideSyncRuntimeLifecycleProducer,
        executor_factory: GuideRuntimeExecutorFactoryPort,
        authority_provider: GuideSyncRuntimeAuthorityProvider,
    ) -> None:
        self._repository = repository
        self._lifecycle = lifecycle
        self._executor_factory = executor_factory
        self._authority_provider = authority_provider
        self.timings = GuideSyncRuntimeTimings()

    async def execute(self, *, user: User, guide: Guide) -> Guide:
        session = self._repository.session
        user_id, guide_id = user.id, guide.id
        token_version = user.token_version
        guide_identity = (guide.id, guide.prescription_id, guide.prescription_version_id, guide.profile_id)
        lease_token = f"guide-sync:{uuid4()}"
        start = perf_counter()
        try:
            if not await self._repository.lock_if_current_version(guide=guide):
                raise GuideSyncRuntimePreparationError("Guide prescription is no longer current")
            snapshot = await self._authority_provider.resolve(user=user, guide=guide)
            preparation = await self._lifecycle.prepare(
                user=user,
                guide=guide,
                authority=GuideSyncRuntimeAuthority.from_snapshot(snapshot),
                snapshot=snapshot,
            )
            preparation.job.status = AiJobStatus.PROCESSING
            preparation.job.lease_token = lease_token
            preparation.job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            await session.commit()
        except GuideRequestAuthorityRejected:
            # Only an explicit evaluator FAIL retains its audit observation.
            guide.generation_status = GuideGenerationStatus.FAILED
            guide.error_code = "GUIDE_RUNTIME_UNAVAILABLE"
            guide.error_message = "현재 서비스를 사용할 수 없습니다. 잠시 후 다시 시도해 주세요."
            guide.completed_at = datetime.now(UTC)
            await session.commit()
            raise
        except Exception:
            await session.rollback()
            await self._record_intake_failure(guide_identity)
            raise
        except BaseException:
            await session.rollback()
            raise
        intake_end = perf_counter()
        execution_end = intake_end
        try:
            # Intake has committed. No request-session transaction or lock spans this await.
            projection, provenance = await execute_verified_guide_runtime(
                factory=self._executor_factory,
                runtime_request=preparation.carrier,
                evaluation_time=datetime.now(UTC),
            )
            execution_end = perf_counter()
            state = await GuideSyncRuntimeFinalizer(session).lock_and_check(
                user_id=user_id,
                guide_id=guide_id,
                request=preparation.carrier,
            )
            if state.job.status != AiJobStatus.PROCESSING or state.job.lease_token != lease_token:
                raise GuideSyncRuntimePreparationError("Sync execution lease is no longer owned")
            current_user = await session.get(User, user_id)
            current = (
                state.is_current
                and state.job.lease_token == lease_token
                and state.job.lease_expires_at is not None
                and state.job.lease_expires_at > datetime.now(UTC)
                and projection.release_decision is not GuideRuntimeReleaseDecision.STALE
                and current_user is not None
                and current_user.account_status == AccountStatus.ACTIVE
                and current_user.is_active
                and current_user.token_version == token_version
                and await self._authority_provider.revalidate(
                    user=current_user,
                    guide=state.guide,
                    snapshot=snapshot,
                )
            )
            if not current:
                projection = GuideRuntimeReleaseProjectionCarrier(
                    contract_version=GUIDE_RUNTIME_RELEASE_PROJECTION_CARRIER_VERSION,
                    release_decision=GuideRuntimeReleaseDecision.STALE,
                    is_current=False,
                    answer=None,
                    fallback=GuideRuntimeApprovedFallback(
                        code=GuideRuntimeFallbackCode.EXECUTION_CONTEXT_STALE,
                        text="현재는 승인된 안내를 제공할 수 없습니다. 의사 또는 약사와 상담하세요.",
                    ),
                    citations=(),
                )
            completed_at = datetime.now(UTC)
            completed = await self._repository.mark_release_completed(
                state.guide,
                projection=projection,
                model_name=provenance.model_name,
                prompt_version=provenance.prompt_version,
                completed_at=completed_at,
            )
            state.job.status = AiJobStatus.COMPLETED if current else AiJobStatus.STALE
            state.job.completed_at = completed_at
            state.job.lease_token = None
            state.job.lease_expires_at = None
            await session.commit()
            if not current:
                raise GuideSyncRuntimeVersionConflictError
            return completed
        except GuideSyncRuntimeVersionConflictError:
            raise
        except BaseException:
            execution_end = max(execution_end, perf_counter())
            await session.rollback()
            await self._record_execution_failure(
                user_id=user_id,
                guide_id=guide_id,
                request=preparation.carrier,
                lease_token=lease_token,
            )
            raise
        finally:
            self.timings = GuideSyncRuntimeTimings(
                intake_seconds=intake_end - start,
                execution_seconds=execution_end - intake_end,
                finalize_seconds=perf_counter() - execution_end,
            )

    async def _record_intake_failure(self, identity: tuple[UUID, UUID, UUID, UUID]) -> None:
        session = self._repository.session
        guide_id, prescription_id, version_id, profile_id = identity
        # A rolled back, including its newly created Guide. Persist no Guard/Job from it.
        failed = Guide(
            id=guide_id,
            prescription_id=prescription_id,
            prescription_version_id=version_id,
            profile_id=profile_id,
            citations=[],
            generation_status=GuideGenerationStatus.FAILED,
            error_code="GUIDE_RUNTIME_UNAVAILABLE",
            error_message="현재 서비스를 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.",
            completed_at=datetime.now(UTC),
        )
        session.add(failed)
        await session.commit()

    async def _record_execution_failure(
        self,
        *,
        user_id: UUID,
        guide_id: UUID,
        request: GuideRuntimeRequestCarrier,
        lease_token: str,
    ) -> None:
        session = self._repository.session
        # The same ordered locks and exact lease protect compensating writes.
        try:
            failed_state = await GuideSyncRuntimeFinalizer(session).lock_and_check(
                user_id=user_id,
                guide_id=guide_id,
                request=request,
            )
        except GuideSyncRuntimePreparationError:
            await session.rollback()
        else:
            failed_guide, failed_job = failed_state.guide, failed_state.job
            if failed_job.status == AiJobStatus.PROCESSING and failed_job.lease_token == lease_token:
                failed_guide.generation_status = GuideGenerationStatus.FAILED
                failed_guide.error_code = "GUIDE_RUNTIME_UNAVAILABLE"
                failed_guide.error_message = "현재 서비스를 사용할 수 없습니다. 잠시 후 다시 시도해 주세요."
                failed_guide.completed_at = datetime.now(UTC)
                failed_job.status = AiJobStatus.FAILED
                failed_job.lease_token = None
                failed_job.lease_expires_at = None
                failed_job.failure_code = "DEPENDENCY_UNAVAILABLE"
                failed_job.failure_detail = "GUIDE_RUNTIME_UNAVAILABLE"
                failed_job.completed_at = failed_guide.completed_at
                await session.commit()
            else:
                await session.rollback()
