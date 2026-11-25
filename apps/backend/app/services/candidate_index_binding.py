"""Verified Candidate Index artifact binding for synthetic internal searches.

The artifact hash is a runtime binding over a verified READY index. It is not
the index build's content, configuration, or member-set hash.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError, ErrorDetail
from app.models.rag_candidate_index import (
    RagCandidateIndexBuildMode,
    RagCandidateIndexStatus,
    RagCandidateIndexVersion,
)
from app.models.rag_runtime import RagRuntimeReleaseBundle
from app.repositories.rag_candidate_index_repository import (
    CandidateIndexReadError,
    RagCandidateIndexRepository,
    VerifiedReadyCandidateIndexSnapshot,
)

_REF_PREFIX = "candidate-index-version/"
_ARTIFACT_SCHEMA = "candidate-index-runtime-binding-v1"


@dataclass(frozen=True, slots=True)
class CandidateIndexArtifact:
    ref: str
    version: str
    manifest_hash: str


def _unavailable() -> ApiError:
    return ApiError(
        status_code=503,
        code="CANDIDATE_RESOLVER_UNAVAILABLE",
        message="현재 약품 후보 검색 기준을 확인할 수 없습니다.",
        details=[ErrorDetail(field="candidate_index", reason="INDEX_UNAVAILABLE")],
    )


def _parse_ref(ref: str | None) -> UUID:
    if ref is None or not ref.startswith(_REF_PREFIX):
        raise _unavailable()
    raw_id = ref[len(_REF_PREFIX) :]
    try:
        index_id = UUID(raw_id)
    except (ValueError, AttributeError) as exc:
        raise _unavailable() from exc
    if str(index_id) != raw_id:
        raise _unavailable()
    return index_id


def _artifact_projection(snapshot: VerifiedReadyCandidateIndexSnapshot) -> dict[str, object]:
    """Frozen v1 projection of the persisted Index identity and metadata."""
    index = snapshot.version
    return {
        "schema": _ARTIFACT_SCHEMA,
        "index": {
            "id": str(index.id),
            "index_code": index.index_code,
            "index_version": index.index_version,
            "status": index.status.value,
            "build_mode": index.build_mode.value,
            "catalog_set_id": str(index.catalog_set_id),
            "catalog_version": index.catalog_version,
            "catalog_manifest_hash": index.catalog_manifest_hash,
            "schema_version": index.schema_version,
            "normalization_version": index.normalization_version,
            "lexical_config_version": index.lexical_config_version,
            "search_order_version": index.search_order_version,
            "candidate_limit": index.candidate_limit,
            "display_limit": index.display_limit,
            "embedding_provider": index.embedding_provider,
            "embedding_model": index.embedding_model,
            "embedding_model_version": index.embedding_model_version,
            "embedding_dimension": index.embedding_dimension,
            "distance_metric": index.distance_metric,
            "member_count": index.member_count,
            "product_identity_count": index.product_identity_count,
            "product_name_count": index.product_name_count,
            "approved_alias_count": index.approved_alias_count,
            "vector_count": index.vector_count,
            "member_set_hash": index.member_set_hash,
            "configuration_hash": index.configuration_hash,
            "content_hash": index.content_hash,
        },
        "source_refs": [
            {"snapshot_id": ref.snapshot_id, "source_version": ref.source_version}
            for ref in sorted(snapshot.source_refs, key=lambda ref: (ref.snapshot_id, ref.source_version))
        ],
    }


def _artifact_for(snapshot: VerifiedReadyCandidateIndexSnapshot) -> CandidateIndexArtifact:
    index = snapshot.version
    if index.status != RagCandidateIndexStatus.READY or index.build_mode != RagCandidateIndexBuildMode.LEXICAL_ONLY:
        raise _unavailable()
    canonical = json.dumps(
        _artifact_projection(snapshot),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return CandidateIndexArtifact(
        ref=f"{_REF_PREFIX}{index.id}",
        version=index.index_version,
        manifest_hash=hashlib.sha256(canonical).hexdigest(),
    )


async def issue_candidate_index_artifact(
    session: AsyncSession,
    *,
    index_code: str,
    expected_index_version: str,
) -> CandidateIndexArtifact:
    """Issue an artifact only for the currently active verified READY lexical index."""
    if not index_code or not expected_index_version:
        raise _unavailable()
    try:
        snapshot = await RagCandidateIndexRepository(session).get_verified_ready_index_snapshot(
            index_code=index_code,
            expected_index_version=expected_index_version,
        )
        return _artifact_for(snapshot)
    except (CandidateIndexReadError, SQLAlchemyError, TypeError, ValueError) as exc:
        raise _unavailable() from exc


async def resolve_candidate_index_binding(
    session: AsyncSession,
    *,
    bundle: RagRuntimeReleaseBundle,
) -> VerifiedReadyCandidateIndexSnapshot:
    """Resolve the exact pinned artifact; never substitute another active index."""
    index_id = _parse_ref(bundle.candidate_index_ref)
    expected_version = bundle.candidate_index_version
    expected_hash = bundle.candidate_index_manifest_hash
    if not expected_version or not expected_hash:
        raise _unavailable()
    try:
        candidate = (
            await session.execute(
                select(RagCandidateIndexVersion)
                .where(RagCandidateIndexVersion.id == index_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if candidate is None or candidate.index_version != expected_version:
            raise _unavailable()
        snapshot = await RagCandidateIndexRepository(session).get_verified_ready_index_snapshot(
            index_code=candidate.index_code,
            expected_index_version=expected_version,
        )
        artifact = _artifact_for(snapshot)
        if (
            snapshot.version.id != index_id
            or snapshot.version.index_code != candidate.index_code
            or snapshot.version.index_version != expected_version
            or snapshot.version.catalog_version != bundle.catalog_version
            or snapshot.version.catalog_manifest_hash != bundle.catalog_manifest_hash
            or artifact.ref != bundle.candidate_index_ref
            or artifact.version != expected_version
            or not hmac.compare_digest(artifact.manifest_hash, expected_hash)
        ):
            raise _unavailable()
        return snapshot
    except (CandidateIndexReadError, SQLAlchemyError, TypeError, ValueError) as exc:
        raise _unavailable() from exc
