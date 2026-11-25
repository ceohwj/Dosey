"""Synthetic runtime Index artifact must identify one verified READY lexical Index."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from app.core.errors import ApiError
from app.models.rag_candidate_index import (
    RagCandidateIndexBuildMode,
    RagCandidateIndexStatus,
    RagCandidateIndexVersion,
)
from app.models.rag_runtime import RagRuntimeReleaseBundle
from app.repositories.rag_candidate_index_repository import (
    CandidateIndexIntegrityCompromisedError,
    ReadyCandidateIndexSourceRef,
    VerifiedReadyCandidateIndexSnapshot,
)
from app.services.candidate_index_binding import (
    _artifact_for,
    _artifact_projection,
    issue_candidate_index_artifact,
    resolve_candidate_index_binding,
)


def _snapshot() -> VerifiedReadyCandidateIndexSnapshot:
    index = MagicMock(spec=RagCandidateIndexVersion)
    index.id = UUID("11111111-1111-4111-8111-111111111111")
    index.index_code = "SYNTH_INDEX"
    index.index_version = "index-v1"
    index.status = RagCandidateIndexStatus.READY
    index.build_mode = RagCandidateIndexBuildMode.LEXICAL_ONLY
    index.catalog_set_id = UUID("22222222-2222-4222-8222-222222222222")
    index.catalog_version = "catalog-v1"
    index.catalog_manifest_hash = "a" * 64
    index.schema_version = "schema-v1"
    index.normalization_version = "norm-v1"
    index.lexical_config_version = "lex-v1"
    index.search_order_version = "order-v1"
    index.candidate_limit = 25
    index.display_limit = 1
    index.embedding_provider = None
    index.embedding_model = None
    index.embedding_model_version = None
    index.embedding_dimension = None
    index.distance_metric = None
    index.member_count = 2
    index.product_identity_count = 1
    index.product_name_count = 1
    index.approved_alias_count = 0
    index.vector_count = 0
    index.member_set_hash = "b" * 64
    index.configuration_hash = "c" * 64
    index.content_hash = "d" * 64
    return VerifiedReadyCandidateIndexSnapshot(
        version=index,
        members=(),
        source_refs=(
            ReadyCandidateIndexSourceRef("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "source-b"),
            ReadyCandidateIndexSourceRef("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "source-a"),
        ),
    )


def _bundle(snapshot: VerifiedReadyCandidateIndexSnapshot) -> RagRuntimeReleaseBundle:
    artifact = _artifact_for(snapshot)
    bundle = MagicMock(spec=RagRuntimeReleaseBundle)
    bundle.candidate_index_ref = artifact.ref
    bundle.candidate_index_version = artifact.version
    bundle.candidate_index_manifest_hash = artifact.manifest_hash
    bundle.catalog_version = snapshot.version.catalog_version
    bundle.catalog_manifest_hash = snapshot.version.catalog_manifest_hash
    return bundle


def _session_with_candidate(snapshot: VerifiedReadyCandidateIndexSnapshot) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = snapshot.version
    session.execute.return_value = result
    return session


def _assert_unavailable(error: ApiError) -> None:
    assert error.status_code == 503
    assert error.code == "CANDIDATE_RESOLVER_UNAVAILABLE"
    assert error.details[0].reason == "INDEX_UNAVAILABLE"
    assert error.details[0].rejected_value is None


def test_projection_covers_index_identity_configuration_content_and_sorted_sources() -> None:
    snapshot = _snapshot()
    projection = _artifact_projection(snapshot)
    assert projection["schema"] == "candidate-index-runtime-binding-v1"
    assert projection["index"] == {
        "id": "11111111-1111-4111-8111-111111111111",
        "index_code": "SYNTH_INDEX",
        "index_version": "index-v1",
        "status": "READY",
        "build_mode": "LEXICAL_ONLY",
        "catalog_set_id": "22222222-2222-4222-8222-222222222222",
        "catalog_version": "catalog-v1",
        "catalog_manifest_hash": "a" * 64,
        "schema_version": "schema-v1",
        "normalization_version": "norm-v1",
        "lexical_config_version": "lex-v1",
        "search_order_version": "order-v1",
        "candidate_limit": 25,
        "display_limit": 1,
        "embedding_provider": None,
        "embedding_model": None,
        "embedding_model_version": None,
        "embedding_dimension": None,
        "distance_metric": None,
        "member_count": 2,
        "product_identity_count": 1,
        "product_name_count": 1,
        "approved_alias_count": 0,
        "vector_count": 0,
        "member_set_hash": "b" * 64,
        "configuration_hash": "c" * 64,
        "content_hash": "d" * 64,
    }
    assert projection["source_refs"] == [
        {"snapshot_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "source_version": "source-a"},
        {"snapshot_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "source_version": "source-b"},
    ]
    assert (
        _artifact_for(snapshot).manifest_hash
        == _artifact_for(replace(snapshot, source_refs=tuple(reversed(snapshot.source_refs)))).manifest_hash
    )


@pytest.mark.parametrize(
    "field,changed",
    [("content_hash", "e" * 64), ("member_set_hash", "e" * 64), ("candidate_limit", 24), ("index_code", "OTHER")],
)
def test_manifest_changes_with_bound_index_metadata(field: str, changed: object) -> None:
    snapshot = _snapshot()
    original_hash = _artifact_for(snapshot).manifest_hash
    setattr(snapshot.version, field, changed)
    assert _artifact_for(snapshot).manifest_hash != original_hash


@pytest.mark.asyncio
async def test_issue_uses_verified_ready_snapshot() -> None:
    snapshot = _snapshot()
    with patch("app.services.candidate_index_binding.RagCandidateIndexRepository") as repository_class:
        repository_class.return_value.get_verified_ready_index_snapshot = AsyncMock(return_value=snapshot)
        artifact = await issue_candidate_index_artifact(
            AsyncMock(), index_code="SYNTH_INDEX", expected_index_version="index-v1"
        )
    assert artifact.ref == "candidate-index-version/11111111-1111-4111-8111-111111111111"
    assert artifact.version == "index-v1"
    assert artifact.manifest_hash == _artifact_for(snapshot).manifest_hash
    repository_class.return_value.get_verified_ready_index_snapshot.assert_awaited_once_with(
        index_code="SYNTH_INDEX", expected_index_version="index-v1"
    )


@pytest.mark.asyncio
async def test_resolve_uses_pinned_uuid_and_reverified_active_index() -> None:
    snapshot = _snapshot()
    session = _session_with_candidate(snapshot)
    with patch("app.services.candidate_index_binding.RagCandidateIndexRepository") as repository_class:
        repository_class.return_value.get_verified_ready_index_snapshot = AsyncMock(return_value=snapshot)
        assert await resolve_candidate_index_binding(session, bundle=_bundle(snapshot)) is snapshot
    repository_class.return_value.get_verified_ready_index_snapshot.assert_awaited_once_with(
        index_code="SYNTH_INDEX", expected_index_version="index-v1"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ref",
    [
        "legacy-index-ref",
        "candidate-index-version/11111111111141118111111111111111",
        "candidate-index-version/11111111-1111-4111-8111-111111111111/extra",
        "candidate-index-version/11111111-1111-4111-8111-11111111111Z",
        "candidate-index-version/11111111-1111-4111-8111-111111111111".upper(),
        None,
    ],
)
async def test_noncanonical_or_opaque_ref_fails_before_db(ref: str | None) -> None:
    snapshot = _snapshot()
    bundle = _bundle(snapshot)
    bundle.candidate_index_ref = ref
    session = _session_with_candidate(snapshot)
    with pytest.raises(ApiError) as raised:
        await resolve_candidate_index_binding(session, bundle=bundle)
    _assert_unavailable(raised.value)
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_rejects_same_version_different_index_uuid() -> None:
    snapshot = _snapshot()
    other = _snapshot()
    other.version.id = UUID("33333333-3333-4333-8333-333333333333")
    session = _session_with_candidate(snapshot)
    with patch("app.services.candidate_index_binding.RagCandidateIndexRepository") as repository_class:
        repository_class.return_value.get_verified_ready_index_snapshot = AsyncMock(return_value=other)
        with pytest.raises(ApiError) as raised:
            await resolve_candidate_index_binding(session, bundle=_bundle(snapshot))
    _assert_unavailable(raised.value)


@pytest.mark.asyncio
async def test_resolve_rejects_same_uuid_version_with_different_code() -> None:
    snapshot = _snapshot()
    other = _snapshot()
    other.version.index_code = "OTHER_INDEX"
    session = _session_with_candidate(other)
    with patch("app.services.candidate_index_binding.RagCandidateIndexRepository") as repository_class:
        repository_class.return_value.get_verified_ready_index_snapshot = AsyncMock(return_value=snapshot)
        with pytest.raises(ApiError) as raised:
            await resolve_candidate_index_binding(session, bundle=_bundle(snapshot))
    _assert_unavailable(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ["version", "hash", "catalog_version", "catalog_hash", "missing_row", "hybrid", "integrity"],
)
async def test_resolve_rejects_incorrect_or_unverified_artifact(mutation: str) -> None:
    snapshot = _snapshot()
    bundle = _bundle(snapshot)
    session = _session_with_candidate(snapshot)
    if mutation == "version":
        bundle.candidate_index_version = "index-v2"
    elif mutation == "hash":
        bundle.candidate_index_manifest_hash = "0" * 64
    elif mutation == "catalog_version":
        bundle.catalog_version = "catalog-v2"
    elif mutation == "catalog_hash":
        bundle.catalog_manifest_hash = "0" * 64
    elif mutation == "missing_row":
        session.execute.return_value.scalar_one_or_none.return_value = None
    elif mutation == "hybrid":
        snapshot.version.build_mode = RagCandidateIndexBuildMode.HYBRID
    with patch("app.services.candidate_index_binding.RagCandidateIndexRepository") as repository_class:
        if mutation == "integrity":
            repository_class.return_value.get_verified_ready_index_snapshot = AsyncMock(
                side_effect=CandidateIndexIntegrityCompromisedError("internal details")
            )
        else:
            repository_class.return_value.get_verified_ready_index_snapshot = AsyncMock(return_value=snapshot)
        with pytest.raises(ApiError) as raised:
            await resolve_candidate_index_binding(session, bundle=bundle)
    _assert_unavailable(raised.value)


@pytest.mark.asyncio
async def test_issue_rejects_hybrid() -> None:
    snapshot = _snapshot()
    snapshot.version.build_mode = RagCandidateIndexBuildMode.HYBRID
    with patch("app.services.candidate_index_binding.RagCandidateIndexRepository") as repository_class:
        repository_class.return_value.get_verified_ready_index_snapshot = AsyncMock(return_value=snapshot)
        with pytest.raises(ApiError) as raised:
            await issue_candidate_index_artifact(
                AsyncMock(), index_code="SYNTH_INDEX", expected_index_version="index-v1"
            )
    _assert_unavailable(raised.value)
