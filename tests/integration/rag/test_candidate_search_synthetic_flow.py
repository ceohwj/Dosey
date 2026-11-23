"""Real PostgreSQL synthetic Candidate Search flow in a disposable schema.

Only this test installs a TEST synthetic context. It does not use the app DB or
production Source policy, and every fixture Source is synthetic and local.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Table, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401
from ai_worker.tasks.rag.catalog.types import CatalogFreshnessStatus, CatalogVerificationStatus
from ai_worker.tasks.rag.runtime_bundle_builder import (
    MedicationCatalogBinding,
    RuntimeBundleArtifactKind,
    RuntimeBundleArtifactMemberInput,
    RuntimeBundleBuildDecision,
    RuntimeBundleBuildRequest,
    RuntimeBundleMemberPurpose,
    RuntimeBundleSourceMemberInput,
    RuntimeExecutionManifestInput,
)
from ai_worker.tasks.rag.source_ingestion.snapshot_lifecycle import SnapshotVerificationStatus
from app.apis.v1.medication_candidate_routers import medication_candidate_router
from app.core import config
from app.core.db.databases import Base
from app.core.errors import ApiError, register_exception_handlers
from app.core.no_store_middleware import NoStoreMiddleware
from app.core.validation_trace_middleware import RequestTraceMiddleware
from app.dependencies.security import get_request_user
from app.dependencies.services import get_medication_candidate_service
from app.dtos.medication_candidates import ConfirmMedicationCandidateRequest, RejectMedicationCandidateRequest
from app.models.async_jobs import IdempotencyRecord
from app.models.rag_candidate import (
    MedicationCandidateSearch,
    MedicationCandidateSearchResult,
    MedicationCandidateSearchStatus,
)
from app.models.rag_candidate_index import RagCandidateIndexBuildMode, RagCandidateIndexVersion
from app.models.rag_catalog import (
    RagEntityIdentity,
    RagMedicationIngredient,
    RagMedicationProduct,
    RagMedicationSearchEntryType,
)
from app.models.rag_runtime import RagRuntimeBundleStatus, RagRuntimeEnvironmentStatus
from app.models.rag_source import (
    RagSnapshotVerificationStatus,
    RagSource,
    RagSourceApprovalStatus,
    RagSourceEndpoint,
    RagSourceEndpointLifecycleStatus,
    RagSourceLifecycleStatus,
    RagSourceOperation,
    RagSourceUsageStatus,
    RagVerificationResultStatus,
)
from app.repositories.idempotency_repository import IdempotencyRepository
from app.repositories.medication_candidate_repository import MedicationCandidateRepository
from app.repositories.rag_candidate_index_repository import (
    RagCandidateIndexRepository,
    RagCandidateIndexVersionCreate,
    _recomputed_member_counts,
    _recomputed_member_set_hash,
)
from app.repositories.rag_runtime_repository import RagRuntimeEnvironmentCreate, RagRuntimeRepository
from app.repositories.rag_source_catalog_repository import (
    RagSourceCatalogRepository,
    RagSourceSnapshotVerificationCreate,
)
from app.repositories.rag_source_use_approval_repository import (
    RagSourceUseApprovalRepository,
    SourceUseApprovalCreate,
    SourceUseApprovalRevoke,
)
from app.services.candidate_index_binding import issue_candidate_index_artifact
from app.services.candidate_search import CandidateSearchService
from app.services.candidate_search_resolution import resolve_synthetic_candidate_search
from app.services.candidate_source_guard import SyntheticCandidateContext, SyntheticSourcePolicy
from app.services.idempotency import SyncMutationIdempotencyService, get_default_snapshot_cipher
from app.services.medication_identification import MedicationIdentificationService
from app.services.rag.candidate_policy import CandidateStage, ResolverPolicy
from app.services.rag_runtime_bundle_build import execute_runtime_bundle_build, verify_persisted_bundle_manifest_hash
from app.tests.db_extensions import EXTENSION_SCHEMA, ensure_trigram_extension, ensure_vector_extension
from rag_runtime.runtime_environment import RuntimeEnvironmentCode
from rag_runtime.source_use_approval import SourceUsePurpose
from tests.integration.rag.test_candidate_index_query import (
    _build_member_create,
    _seed_product,
    _seed_source_and_catalog,
)
from tests.integration.rag.test_candidate_search_runtime_binding import _prescription_medication


def _hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


def _tables() -> list[Table]:
    roots = (
        "medication_candidate_search_result",
        "medication_identification",
        "idempotency_record",
        "rag_runtime_environment",
        "rag_runtime_bundle_source",
        "rag_runtime_bundle_citation_approval",
        "rag_candidate_index_member",
        "rag_catalog_set_source",
        "rag_medication_product",
        "rag_source_use_approval",
        "rag_medication_ingredient",
    )
    seen: dict[str, Table] = {}
    stack = [Base.metadata.tables[name] for name in roots]
    while stack:
        table = stack.pop()
        if table.name in seen:
            continue
        seen[table.name] = table
        stack.extend(key.column.table for key in table.foreign_keys)
    return [seen[name] for name in sorted(seen)]


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    is_ci = os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true"
    if (
        config.DB_NAME != "test"
        or config.DB_HOST not in {"127.0.0.1", "localhost"}
        or (not is_ci and (config.DB_PORT != 55439 or config.DB_EXPOSE_PORT != 55439))
    ):
        pytest.fail("synthetic flow requires the dedicated port-55439 test database")
    schema = "candidate_synthetic_" + uuid4().hex
    admin = create_async_engine(config.database_url, poolclass=NullPool, hide_parameters=True)
    engine = create_async_engine(
        config.database_url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"server_settings": {"search_path": f"{schema},public,{EXTENSION_SCHEMA}"}},
    )
    try:
        async with admin.begin() as connection:
            await ensure_vector_extension(connection)
            await ensure_trigram_extension(connection)
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=_tables()))
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


def _policy(*, version: str = "synthetic-resolver-v1") -> ResolverPolicy:
    return ResolverPolicy(
        policy_version=version,
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
    )


async def _seed_flow(sessions: async_sessionmaker[AsyncSession]):
    async with sessions.begin() as session:
        owner, medication = await _prescription_medication(session)
        source_snapshot, catalog = await _seed_source_and_catalog(session)
        source_repository = RagSourceCatalogRepository(session)
        operation = await session.get(RagSourceOperation, source_snapshot.operation_id)
        assert operation is not None
        endpoint = await session.get(RagSourceEndpoint, operation.endpoint_id)
        assert endpoint is not None
        source = await session.get(RagSource, endpoint.source_id)
        assert source is not None
        source.source_code = f"SYNTHETIC_CANDIDATE_{uuid4().hex[:10]}"
        source.lifecycle_status = RagSourceLifecycleStatus.ACTIVE
        endpoint.lifecycle_status = RagSourceEndpointLifecycleStatus.VERIFIED
        endpoint.runtime_status = RagSourceUsageStatus.ENABLED
        endpoint.acquisition_status = RagSourceApprovalStatus.APPROVED
        operation.runtime_status = RagSourceUsageStatus.ENABLED
        operation.acquisition_status = RagSourceApprovalStatus.APPROVED
        now = datetime.now(UTC)
        verification = await source_repository.create_snapshot_verification(
            RagSourceSnapshotVerificationCreate(
                snapshot_id=source_snapshot.id,
                check_name="snapshot-state-seal",
                verification_result=RagVerificationResultStatus.NO_CHANGE,
                verified_at=now - timedelta(minutes=1),
                verified_by="synthetic-owner",
            )
        )
        await source_repository.create_snapshot_verification(
            RagSourceSnapshotVerificationCreate(
                snapshot_id=source_snapshot.id,
                check_name="snapshot-current-selection",
                verification_result=RagVerificationResultStatus.PASSED,
                verified_at=now - timedelta(minutes=1),
                verified_by="synthetic-owner",
            )
        )
        source_snapshot.verification_status = RagSnapshotVerificationStatus.CURRENT
        source_snapshot.verification_seal_id = verification.id
        source_snapshot.verified_at = now - timedelta(minutes=1)
        source_snapshot.effective_at = now - timedelta(minutes=1)
        source_snapshot.collected_at = now - timedelta(minutes=2)
        approval = await RagSourceUseApprovalRepository(session).create_approval(
            SourceUseApprovalCreate(
                source_snapshot_id=source_snapshot.id,
                source_code=source.source_code,
                source_version=source_snapshot.source_version,
                environment=RuntimeEnvironmentCode.TEST,
                purpose=SourceUsePurpose.PRODUCT_IDENTIFICATION,
                approval_version="synthetic-approval-v1",
                valid_from=now - timedelta(minutes=2),
                expires_at=now + timedelta(hours=1),
                actor_id=owner.id,
                evidence_ref="synthetic://candidate/approval",
            )
        )
        product = await _seed_product(
            session,
            snapshot=source_snapshot,
            canonical_code="SYNTH-200001",
            product_name="Synthetic Tablet",
            strength_text="500mg",
            dosage_form="tablet",
            manufacturer_name="Synthetic Manufacturer",
        )
        member = _build_member_create(
            snapshot=source_snapshot,
            catalog_set=catalog,
            product=product,
            entry_type=RagMedicationSearchEntryType.PRODUCT_NAME,
            entry_ref="synthetic:product-name",
            display_text=product.product_name,
            normalized_text=product.product_name,
            member_key=_hash("synthetic-member"),
        )
        members = (member,)
        counts = _recomputed_member_counts(members)
        index_code = f"SYNTHETIC_INDEX_{uuid4().hex[:10]}"
        index_version = "synthetic-index-v1"
        build = await RagCandidateIndexRepository(session).build_index_version(
            version=RagCandidateIndexVersionCreate(
                index_code=index_code,
                index_version=index_version,
                build_mode=RagCandidateIndexBuildMode.LEXICAL_ONLY,
                catalog_set_id=catalog.id,
                catalog_version=catalog.catalog_version,
                catalog_manifest_hash=catalog.envelope_hash,
                schema_version=catalog.schema_version,
                normalization_version=catalog.normalization_version,
                lexical_config_version="synthetic-lexical-v1",
                search_order_version="synthetic-order-v1",
                candidate_limit=10,
                display_limit=1,
                member_count=counts["member_count"],
                product_identity_count=counts["product_identity_count"],
                product_name_count=counts["product_name_count"],
                approved_alias_count=counts["approved_alias_count"],
                vector_count=counts["vector_count"],
                member_set_hash=_recomputed_member_set_hash(members),
                configuration_hash=_hash("synthetic-index-config"),
                content_hash=_hash("synthetic-index-content"),
            ),
            members=members,
        )
        await RagCandidateIndexRepository(session).activate_ready_version(build.version.id)
        artifact = await issue_candidate_index_artifact(
            session, index_code=index_code, expected_index_version=index_version
        )
        source_policy = SyntheticSourcePolicy(
            source_snapshot_id=source_snapshot.id,
            source_code=source.source_code,
            maximum_age=timedelta(hours=2),
        )
        request = RuntimeBundleBuildRequest(
            bundle_key=f"synthetic-candidate-{uuid4().hex[:8]}",
            bundle_version="v1",
            environment_code="TEST",
            execution_manifest=RuntimeExecutionManifestInput(
                manifest_key=f"synthetic-candidate-manifest-{uuid4().hex[:8]}",
                manifest_version="v1",
                schema_version="runtime-manifest-v1",
                git_commit_sha="abcdef1",
                worker_artifact_ref="worker:synthetic:test-only",
            ),
            catalog=MedicationCatalogBinding(
                catalog_version=catalog.catalog_version,
                catalog_manifest_hash=catalog.envelope_hash,
                verification_status=CatalogVerificationStatus.APPROVED,
                freshness_status=CatalogFreshnessStatus.CURRENT,
                is_complete=True,
                source_snapshot_ids=(str(source_snapshot.id),),
            ),
            source_members=(
                RuntimeBundleSourceMemberInput(
                    source_snapshot_id=str(source_snapshot.id),
                    source_purpose=RuntimeBundleMemberPurpose.CATALOG,
                    source_version=source_snapshot.source_version,
                    canonical_checksum=source_snapshot.canonical_checksum,
                    approval_version="synthetic-approval-v1",
                    scope_policy_hash=source_policy.scope_policy_hash,
                    freshness_policy_hash=source_policy.freshness_policy_hash,
                    observed_environment="TEST",
                    verification_status=SnapshotVerificationStatus.CURRENT,
                    rejected_record_count=0,
                    publication_approval_passed=True,
                    freshness_eligible=True,
                    provenance_valid=True,
                    approval_expired=False,
                    revocation_unresolved=False,
                    scope_allowed=True,
                ),
            ),
            artifact_members=(
                RuntimeBundleArtifactMemberInput(
                    artifact_kind=RuntimeBundleArtifactKind.CANDIDATE_INDEX,
                    artifact_ref=artifact.ref,
                    artifact_version=artifact.version,
                    observed_environment="TEST",
                    approval_effective=True,
                    approval_expired=False,
                    revocation_unresolved=False,
                    manifest_hash=artifact.manifest_hash,
                    catalog_version=catalog.catalog_version,
                    catalog_manifest_hash=catalog.envelope_hash,
                ),
            ),
            governance_revision_ref="synthetic-test-v1",
            created_by="candidate-synthetic-integration",
        )
        execution = await execute_runtime_bundle_build(session, request)
        assert execution.outcome.decision is RuntimeBundleBuildDecision.BUILDABLE
        assert execution.persisted is not None
        bundle = execution.persisted.bundle
        bundle.bundle_status = RagRuntimeBundleStatus.READY
        bundle.ready_at = now
        environment = await RagRuntimeRepository(session).create_environment(
            RagRuntimeEnvironmentCreate(
                environment_code="TEST",
                environment_status=RagRuntimeEnvironmentStatus.ACTIVE,
                active_bundle_id=bundle.id,
                active_bundle_manifest_hash=bundle.bundle_manifest_hash,
                governance_revision_ref="synthetic-test-v1",
                safety_epoch=1,
            )
        )
        await session.flush()
        assert await verify_persisted_bundle_manifest_hash(session, bundle.id)
        context = SyntheticCandidateContext(
            runtime_environment_id=environment.id,
            source_policies=(source_policy,),
            resolver_policy=_policy(),
            expected_governance_revision_ref="synthetic-test-v1",
            expected_safety_epoch=1,
            search_ttl=timedelta(minutes=10),
            query_hmac_key="synthetic-test-only-hmac-key-at-least-32-characters",
        )
        return SimpleNamespace(
            owner_id=owner.id,
            medication_id=medication.id,
            product_id=product.id,
            source_id=source_snapshot.id,
            approval_id=approval.id,
            index_id=build.version.id,
            bundle_id=bundle.id,
            environment_id=environment.id,
            context=context,
        )


def _service(session: AsyncSession, context: SyntheticCandidateContext | None) -> CandidateSearchService:
    repository = MedicationCandidateRepository(session)
    return CandidateSearchService(
        repository,
        MedicationIdentificationService(repository),
        SyncMutationIdempotencyService(IdempotencyRepository(session), get_default_snapshot_cipher()),
        context=context,
    )


async def _create(
    sessions: async_sessionmaker[AsyncSession], fixture, *, context: SyntheticCandidateContext | None = None
):
    async with sessions.begin() as session:
        return await _service(session, context or fixture.context).create_candidate_search(
            user=SimpleNamespace(id=fixture.owner_id),
            prescription_version_medication_id=fixture.medication_id,
        )


@pytest.mark.asyncio
async def test_synthetic_ready_get_confirm_replay_then_source_revocation_blocks_current_use(sessions) -> None:
    fixture = await _seed_flow(sessions)
    created = await _create(sessions, fixture)
    assert created.status == MedicationCandidateSearchStatus.READY
    assert created.candidate_search_result_id is not None
    assert created.candidate is not None
    assert created.candidate.product_name == "Synthetic Tablet"
    assert created.expires_at is not None
    assert datetime.now(UTC) < created.expires_at <= datetime.now(UTC) + timedelta(minutes=10)
    async with sessions() as session:
        search = await session.get(MedicationCandidateSearch, created.search_id)
        result = await session.get(MedicationCandidateSearchResult, created.candidate_search_result_id)
        assert search is not None and result is not None
        assert search.candidate_index_version_id == fixture.index_id
        assert search.runtime_release_bundle_id == fixture.bundle_id
        assert result.product_id == fixture.product_id
        assert result.result_score > 0
        assert result.selection_eligible and result.is_displayed

    async with sessions.begin() as session:
        read = await _service(session, fixture.context).get_candidate_search(
            user=SimpleNamespace(id=fixture.owner_id),
            prescription_version_medication_id=fixture.medication_id,
        )
        assert read.search_id == created.search_id
    request = ConfirmMedicationCandidateRequest(
        prescription_version_medication_id=fixture.medication_id,
        candidate_search_result_id=created.candidate_search_result_id,
    )
    key = "synthetic-confirm-" + uuid4().hex
    async with sessions.begin() as session:
        first = await _service(session, fixture.context).confirm_candidate(
            user=SimpleNamespace(id=fixture.owner_id), request=request, idempotency_key=key
        )
        record = await session.scalar(select(IdempotencyRecord))
        assert record is not None
        original_key_snapshot = (record.key_hmac, record.request_hash, record.expires_at, record.response_body_snapshot)
    async with sessions.begin() as session:
        await RagSourceUseApprovalRepository(session).revoke(
            SourceUseApprovalRevoke(
                approval_id=fixture.approval_id,
                revoked_at=datetime.now(UTC),
                revoked_by=fixture.owner_id,
                revoked_reason="SYNTHETIC_TEST_REVOCATION",
            )
        )
    async with sessions.begin() as session:
        replay = await _service(session, None).confirm_candidate(
            user=SimpleNamespace(id=fixture.owner_id), request=request, idempotency_key=key
        )
        assert replay.model_dump(mode="json") == first.model_dump(mode="json")
        record = await session.scalar(select(IdempotencyRecord))
        assert record is not None
        assert (
            record.key_hmac,
            record.request_hash,
            record.expires_at,
            record.response_body_snapshot,
        ) == original_key_snapshot
    async with sessions() as session:
        with pytest.raises(ApiError) as error:
            await _service(session, fixture.context).confirm_candidate(
                user=SimpleNamespace(id=fixture.owner_id), request=request, idempotency_key=key + "-new"
            )
        assert error.value.code in {"CANDIDATE_RESOLVER_UNAVAILABLE", "IDENTIFICATION_CONTEXT_STALE"}
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1


@pytest.mark.asyncio
async def test_revoked_source_blocks_get_and_first_selection_without_changing_ready_history(sessions) -> None:
    fixture = await _seed_flow(sessions)
    created = await _create(sessions, fixture)
    assert created.candidate_search_result_id is not None
    async with sessions.begin() as session:
        await RagSourceUseApprovalRepository(session).revoke(
            SourceUseApprovalRevoke(
                approval_id=fixture.approval_id,
                revoked_at=datetime.now(UTC),
                revoked_by=fixture.owner_id,
                revoked_reason="SYNTHETIC_TEST_REVOCATION",
            )
        )
    async with sessions() as session:
        with pytest.raises(ApiError) as read_error:
            await _service(session, fixture.context).get_candidate_search(
                user=SimpleNamespace(id=fixture.owner_id),
                prescription_version_medication_id=fixture.medication_id,
            )
        assert read_error.value.code == "CANDIDATE_RESOLVER_UNAVAILABLE"
    async with sessions() as session:
        with pytest.raises(ApiError) as select_error:
            await _service(session, fixture.context).confirm_candidate(
                user=SimpleNamespace(id=fixture.owner_id),
                request=ConfirmMedicationCandidateRequest(
                    prescription_version_medication_id=fixture.medication_id,
                    candidate_search_result_id=created.candidate_search_result_id,
                ),
                idempotency_key="revoked-first-selection-" + uuid4().hex,
            )
        assert select_error.value.code == "CANDIDATE_RESOLVER_UNAVAILABLE"
    async with sessions() as session:
        search = await session.get(MedicationCandidateSearch, created.search_id)
        assert search is not None and search.status is MedicationCandidateSearchStatus.READY
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 0


@pytest.mark.asyncio
async def test_reject_replay_survives_source_revocation(sessions) -> None:
    fixture = await _seed_flow(sessions)
    created = await _create(sessions, fixture)
    assert created.candidate_search_result_id is not None
    request = RejectMedicationCandidateRequest(
        search_id=created.search_id,
        candidate_search_result_id=created.candidate_search_result_id,
    )
    key = "synthetic-reject-" + uuid4().hex
    async with sessions.begin() as session:
        first = await _service(session, fixture.context).reject_candidate(
            user=SimpleNamespace(id=fixture.owner_id), request=request, idempotency_key=key
        )
        record = await session.scalar(select(IdempotencyRecord))
        assert record is not None
        original_key_snapshot = (record.key_hmac, record.request_hash, record.expires_at, record.response_body_snapshot)
    async with sessions.begin() as session:
        await RagSourceUseApprovalRepository(session).revoke(
            SourceUseApprovalRevoke(
                approval_id=fixture.approval_id,
                revoked_at=datetime.now(UTC),
                revoked_by=fixture.owner_id,
                revoked_reason="SYNTHETIC_TEST_REVOCATION",
            )
        )
    async with sessions.begin() as session:
        replay = await _service(session, None).reject_candidate(
            user=SimpleNamespace(id=fixture.owner_id), request=request, idempotency_key=key
        )
        assert replay.model_dump(mode="json") == first.model_dump(mode="json")
        record = await session.scalar(select(IdempotencyRecord))
        assert record is not None
        assert (
            record.key_hmac,
            record.request_hash,
            record.expires_at,
            record.response_body_snapshot,
        ) == original_key_snapshot


@pytest.mark.asyncio
async def test_rejected_source_blocks_before_search_insert(sessions) -> None:
    fixture = await _seed_flow(sessions)
    async with sessions.begin() as session:
        await RagSourceUseApprovalRepository(session).revoke(
            SourceUseApprovalRevoke(
                approval_id=fixture.approval_id,
                revoked_at=datetime.now(UTC),
                revoked_by=fixture.owner_id,
                revoked_reason="SYNTHETIC_TEST_REVOCATION",
            )
        )
    with pytest.raises(ApiError) as error:
        await _create(sessions, fixture)
    assert error.value.code == "CANDIDATE_RESOLVER_UNAVAILABLE"
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MedicationCandidateSearch)) == 0


@pytest.mark.asyncio
async def test_same_context_reuses_search_and_policy_change_requires_new_search(sessions) -> None:
    fixture = await _seed_flow(sessions)
    first = await _create(sessions, fixture)
    repeated = await _create(sessions, fixture)
    assert repeated.search_id == first.search_id
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MedicationCandidateSearch)) == 1
    changed_context = SyntheticCandidateContext(
        runtime_environment_id=fixture.context.runtime_environment_id,
        source_policies=fixture.context.source_policies,
        resolver_policy=_policy(version="synthetic-resolver-v2"),
        expected_governance_revision_ref=fixture.context.expected_governance_revision_ref,
        expected_safety_epoch=fixture.context.expected_safety_epoch,
        search_ttl=fixture.context.search_ttl,
        query_hmac_key=fixture.context.query_hmac_key,
    )
    async with sessions() as session:
        with pytest.raises(ApiError) as error:
            await _service(session, changed_context).get_candidate_search(
                user=SimpleNamespace(id=fixture.owner_id),
                prescription_version_medication_id=fixture.medication_id,
            )
        assert error.value.status_code == 409
        assert error.value.code == "IDENTIFICATION_CONTEXT_STALE"
    fresh = await _create(sessions, fixture, context=changed_context)
    assert fresh.search_id != first.search_id
    assert fresh.status == MedicationCandidateSearchStatus.READY


@pytest.mark.asyncio
async def test_bundle_index_hash_drift_fails_closed(sessions) -> None:
    fixture = await _seed_flow(sessions)
    async with sessions.begin() as session:
        index = await session.get(RagCandidateIndexVersion, fixture.index_id)
        assert index is not None
        index.configuration_hash = "f" * 64
    with pytest.raises(ApiError) as error:
        await _create(sessions, fixture)
    assert error.value.code == "CANDIDATE_RESOLVER_UNAVAILABLE"
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MedicationCandidateSearch)) == 0


@pytest.mark.asyncio
async def test_environment_revision_creates_new_search_and_invalidates_old_one(sessions) -> None:
    fixture = await _seed_flow(sessions)
    previous = await _create(sessions, fixture)
    async with sessions.begin() as session:
        environment = await RagRuntimeRepository(session).lock_environment_by_id(fixture.environment_id)
        assert environment is not None
        environment.environment_revision = 2
    current = await _create(sessions, fixture)
    assert current.search_id != previous.search_id
    assert current.status == MedicationCandidateSearchStatus.READY
    async with sessions() as session:
        old = await session.get(MedicationCandidateSearch, previous.search_id)
        new = await session.get(MedicationCandidateSearch, current.search_id)
        assert old is not None and old.status is MedicationCandidateSearchStatus.INVALIDATED_INPUT_CHANGED
        assert new is not None and new.runtime_environment_revision == 2


@pytest.mark.asyncio
async def test_product_status_drift_blocks_ready_read_and_first_selection(sessions) -> None:
    fixture = await _seed_flow(sessions)
    created = await _create(sessions, fixture)
    assert created.candidate_search_result_id is not None
    async with sessions.begin() as session:
        product = await session.get(RagMedicationProduct, fixture.product_id)
        assert product is not None
        product.product_status = "INACTIVE"
    async with sessions() as session:
        with pytest.raises(ApiError) as read_error:
            await _service(session, fixture.context).get_candidate_search(
                user=SimpleNamespace(id=fixture.owner_id),
                prescription_version_medication_id=fixture.medication_id,
            )
        assert read_error.value.code == "CANDIDATE_RESOLVER_UNAVAILABLE"
    async with sessions() as session:
        with pytest.raises(ApiError) as select_error:
            await _service(session, fixture.context).confirm_candidate(
                user=SimpleNamespace(id=fixture.owner_id),
                request=ConfirmMedicationCandidateRequest(
                    prescription_version_medication_id=fixture.medication_id,
                    candidate_search_result_id=created.candidate_search_result_id,
                ),
                idempotency_key="status-drift-first-selection-" + uuid4().hex,
            )
        assert select_error.value.code == "CANDIDATE_RESOLVER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_actual_ingredient_row_is_diagnostic_only(sessions) -> None:
    fixture = await _seed_flow(sessions)
    async with sessions.begin() as session:
        identity = RagEntityIdentity(
            entity_type="INGREDIENT",
            code_system="MFDS_INGREDIENT_CODE",
            canonical_code="SYNTH-I-001",
        )
        session.add(identity)
        await session.flush()
        session.add(
            RagMedicationIngredient(
                source_snapshot_id=fixture.source_id,
                entity_identity_id=identity.id,
                source_record_key="synthetic-ingredient-1",
                ingredient_code_system="MFDS_INGREDIENT_CODE",
                ingredient_code="SYNTH-I-001",
                ingredient_name="합성성분",
                normalized_ingredient_name="합성성분",
            )
        )
    async with sessions.begin() as session:
        index = await session.get(RagCandidateIndexVersion, fixture.index_id)
        assert index is not None
        ingredients = tuple((await session.scalars(select(RagMedicationIngredient))).all())
        assert len(ingredients) == 1
        assert ingredients[0].normalized_ingredient_name == "합성성분"
        snapshot = await RagCandidateIndexRepository(session).get_verified_ready_index_snapshot(
            index_code=index.index_code,
            expected_index_version=index.index_version,
        )
        diagnostic = await resolve_synthetic_candidate_search(
            session,
            snapshot=snapshot,
            medication_name="합성성분",
            strength_text=None,
            policy=fixture.context.resolver_policy,
        )
        assert diagnostic.status is MedicationCandidateSearchStatus.INGREDIENT_ONLY
        assert diagnostic.status_reason == "PRODUCT_NAME_REQUIRED"
        assert diagnostic.results == []


@pytest.mark.asyncio
async def test_positive_lexical_product_hit_is_not_reclassified_as_ingredient_only(sessions) -> None:
    fixture = await _seed_flow(sessions)
    async with sessions.begin() as session:
        identity = RagEntityIdentity(
            entity_type="INGREDIENT",
            code_system="MFDS_INGREDIENT_CODE",
            canonical_code="SYNTH-I-002",
        )
        session.add(identity)
        await session.flush()
        session.add(
            RagMedicationIngredient(
                source_snapshot_id=fixture.source_id,
                entity_identity_id=identity.id,
                source_record_key="synthetic-ingredient-2",
                ingredient_code_system="MFDS_INGREDIENT_CODE",
                ingredient_code="SYNTH-I-002",
                ingredient_name="Synthetic Ingredient",
                normalized_ingredient_name="Synthetic Ingredient",
            )
        )
    async with sessions.begin() as session:
        lexical_score = await session.scalar(select(func.similarity("Synthetic Tablet", "Synthetic Ingredient")))
        assert lexical_score is not None and 0 < lexical_score < 1
        index = await session.get(RagCandidateIndexVersion, fixture.index_id)
        assert index is not None
        snapshot = await RagCandidateIndexRepository(session).get_verified_ready_index_snapshot(
            index_code=index.index_code, expected_index_version=index.index_version
        )
        result = await resolve_synthetic_candidate_search(
            session,
            snapshot=snapshot,
            medication_name="Synthetic Ingredient",
            strength_text=None,
            policy=fixture.context.resolver_policy,
        )
        assert result.status is MedicationCandidateSearchStatus.NO_CANDIDATE
        assert result.results == []


@pytest.mark.asyncio
async def test_http_search_uses_real_synthetic_service_and_public_gate_stays_closed_by_default(
    sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = await _seed_flow(sessions)
    fastapi_app = FastAPI()
    register_exception_handlers(fastapi_app)
    fastapi_app.include_router(medication_candidate_router, prefix="/api/v1")
    fastapi_app.dependency_overrides[get_request_user] = lambda: SimpleNamespace(id=fixture.owner_id)

    async def real_service() -> AsyncIterator[CandidateSearchService]:
        async with sessions.begin() as session:
            yield _service(session, fixture.context)

    fastapi_app.dependency_overrides[get_medication_candidate_service] = real_service
    app = RequestTraceMiddleware(NoStoreMiddleware(fastapi_app))
    path = "/api/v1/medication-candidate-searches"
    payload = {"prescription_version_medication_id": str(fixture.medication_id)}
    monkeypatch.setattr(config, "PUBLIC_TRACK_F_ENABLED", True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(path, json=payload)
        assert created.status_code == 202
        assert created.headers["cache-control"] == "no-store"
        assert created.json()["data"]["status"] == "READY"
        read = await client.get(f"{path}/{fixture.medication_id}")
        assert read.status_code == 200
        assert read.headers["cache-control"] == "no-store"
        assert read.json()["data"]["search_id"] == created.json()["data"]["search_id"]

        class DomainSpy:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def create_candidate_search(self, **_kwargs):
                self.calls.append("create")
                raise AssertionError("public gate must block before domain create")

            async def get_candidate_search(self, **_kwargs):
                self.calls.append("get")
                raise AssertionError("public gate must block before domain read")

        spy = DomainSpy()
        fastapi_app.dependency_overrides[get_medication_candidate_service] = lambda: spy
        monkeypatch.setattr(config, "PUBLIC_TRACK_F_ENABLED", False)
        blocked_create = await client.post(path, json=payload)
        blocked_read = await client.get(f"{path}/{fixture.medication_id}")
        for blocked in (blocked_create, blocked_read):
            assert blocked.status_code == 503
            assert blocked.headers["cache-control"] == "no-store"
            assert blocked.json()["code"] == "SERVICE_UNAVAILABLE"
            assert blocked.json()["details"][0]["reason"] == "PUBLIC_TRACK_F_DISABLED"
        assert spy.calls == []


@pytest.mark.asyncio
async def test_revocation_waits_for_create_guard_lock_then_blocks_later_read(sessions) -> None:
    fixture = await _seed_flow(sessions)
    creating_session = sessions()
    revocation_started = asyncio.Event()

    async def revoke_in_other_session() -> None:
        async with sessions.begin() as session:
            revocation_started.set()
            await RagSourceUseApprovalRepository(session).revoke(
                SourceUseApprovalRevoke(
                    approval_id=fixture.approval_id,
                    revoked_at=datetime.now(UTC),
                    revoked_by=fixture.owner_id,
                    revoked_reason="SYNTHETIC_CONCURRENT_REVOCATION",
                )
            )

    revoking: asyncio.Task[None] | None = None
    try:
        created = await _service(creating_session, fixture.context).create_candidate_search(
            user=SimpleNamespace(id=fixture.owner_id),
            prescription_version_medication_id=fixture.medication_id,
        )
        assert created.status == MedicationCandidateSearchStatus.READY
        revoking = asyncio.create_task(revoke_in_other_session())
        await asyncio.wait_for(revocation_started.wait(), timeout=3)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(revoking), timeout=0.2)
        await creating_session.commit()
        await asyncio.wait_for(revoking, timeout=3)
    finally:
        if revoking is not None and not revoking.done():
            revoking.cancel()
            await asyncio.gather(revoking, return_exceptions=True)
        await creating_session.rollback()
        await creating_session.close()

    async with sessions() as session:
        with pytest.raises(ApiError) as error:
            await _service(session, fixture.context).get_candidate_search(
                user=SimpleNamespace(id=fixture.owner_id),
                prescription_version_medication_id=fixture.medication_id,
            )
        assert error.value.code == "CANDIDATE_RESOLVER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_concurrent_same_prescription_create_reuses_single_search(sessions) -> None:
    fixture = await _seed_flow(sessions)
    first_session = sessions()
    second_started = asyncio.Event()

    async def create_in_other_session():
        async with sessions.begin() as session:
            second_started.set()
            return await _service(session, fixture.context).create_candidate_search(
                user=SimpleNamespace(id=fixture.owner_id),
                prescription_version_medication_id=fixture.medication_id,
            )

    second: asyncio.Task | None = None
    try:
        first = await _service(first_session, fixture.context).create_candidate_search(
            user=SimpleNamespace(id=fixture.owner_id),
            prescription_version_medication_id=fixture.medication_id,
        )
        second = asyncio.create_task(create_in_other_session())
        await asyncio.wait_for(second_started.wait(), timeout=3)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second), timeout=0.2)
        await first_session.commit()
        repeated = await asyncio.wait_for(second, timeout=3)
    finally:
        if second is not None and not second.done():
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
        await first_session.rollback()
        await first_session.close()

    assert repeated.search_id == first.search_id
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MedicationCandidateSearch)) == 1
        assert await session.scalar(select(func.count()).select_from(MedicationCandidateSearchResult)) == 1


@pytest.mark.asyncio
async def test_mismatched_product_identity_blocks_create_without_search(sessions) -> None:
    fixture = await _seed_flow(sessions)
    async with sessions.begin() as session:
        other_identity = RagEntityIdentity(
            entity_type="PRODUCT",
            code_system="MFDS_ITEM_SEQ",
            canonical_code="SYNTH-OTHER-PRODUCT",
        )
        session.add(other_identity)
        await session.flush()
        product = await session.get(RagMedicationProduct, fixture.product_id)
        assert product is not None
        product.entity_identity_id = other_identity.id

    with pytest.raises(ApiError) as error:
        await _create(sessions, fixture)
    assert error.value.code == "CANDIDATE_RESOLVER_UNAVAILABLE"
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MedicationCandidateSearch)) == 0


@pytest.mark.asyncio
async def test_cancelled_resolution_rolls_back_search_and_results(sessions, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = await _seed_flow(sessions)

    async def cancelled_resolution(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr("app.services.candidate_search.resolve_synthetic_candidate_search", cancelled_resolution)
    with pytest.raises(asyncio.CancelledError):
        await _create(sessions, fixture)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MedicationCandidateSearch)) == 0
        assert await session.scalar(select(func.count()).select_from(MedicationCandidateSearchResult)) == 0
