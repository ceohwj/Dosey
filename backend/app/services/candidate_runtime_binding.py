"""Immutable Search binding to a locked Runtime environment and canonical Bundle."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError, ErrorDetail
from app.models.rag_runtime import RagRuntimeBundleStatus, RagRuntimeEnvironmentStatus
from app.repositories.rag_runtime_repository import RagRuntimeRepository
from app.services.rag_runtime_bundle_build import verify_persisted_bundle_manifest_hash


@dataclass(frozen=True)
class CandidateRuntimeBinding:
    runtime_release_bundle_id: UUID | None
    runtime_release_bundle_manifest_hash: str | None
    runtime_environment_id: UUID | None
    runtime_environment_revision: int | None

    @property
    def complete(self) -> bool:
        return all(
            value is not None
            for value in (
                self.runtime_release_bundle_id,
                self.runtime_release_bundle_manifest_hash,
                self.runtime_environment_id,
                self.runtime_environment_revision,
            )
        )


def _stale() -> ApiError:
    return ApiError(
        status_code=409,
        code="IDENTIFICATION_CONTEXT_STALE",
        message="약품 식별 기준이 최신 상태가 아닙니다. 최신 처방 상태를 다시 확인해 주세요.",
        details=[ErrorDetail(field="candidate_search_result_id", reason="RUNTIME_CHANGED")],
    )


def _unavailable() -> ApiError:
    return ApiError(
        status_code=503,
        code="CANDIDATE_RESOLVER_UNAVAILABLE",
        message="현재 약품 후보 검색 기준을 확인할 수 없습니다.",
        details=[ErrorDetail(field="candidate_search_result_id", reason="RUNTIME_UNAVAILABLE")],
    )


async def require_current_candidate_runtime(
    repository: RagRuntimeRepository,
    *,
    runtime_environment_id: UUID,
    expected_bundle_id: UUID | None = None,
    expected_binding: CandidateRuntimeBinding | None = None,
    verify_manifest: Callable[[AsyncSession, UUID], Awaitable[bool]] = verify_persisted_bundle_manifest_hash,
) -> CandidateRuntimeBinding:
    """Lock environment through caller commit; never infer a legacy Search's binding."""
    if expected_binding is not None and not expected_binding.complete:
        raise _stale()
    environment = await repository.lock_environment_by_id(runtime_environment_id)
    if environment is None or environment.environment_status != RagRuntimeEnvironmentStatus.ACTIVE:
        raise _unavailable()
    bundle_id = environment.active_bundle_id
    bundle_hash = environment.active_bundle_manifest_hash
    if bundle_id is None or bundle_hash is None or environment.environment_revision < 1:
        raise _unavailable()
    actual = CandidateRuntimeBinding(bundle_id, bundle_hash, environment.id, environment.environment_revision)
    if expected_bundle_id is not None and bundle_id != expected_bundle_id:
        raise _stale()
    if expected_binding is not None and actual != expected_binding:
        raise _stale()
    bundle = await repository.get_release_bundle_by_id(bundle_id)
    if (
        bundle is None
        or bundle.bundle_status not in (RagRuntimeBundleStatus.READY, RagRuntimeBundleStatus.RETIRED)
        or bundle.bundle_manifest_hash != bundle_hash
        or bundle.environment_code != environment.environment_code
        or not await verify_manifest(repository.session, bundle_id)
    ):
        raise _unavailable()
    return actual
