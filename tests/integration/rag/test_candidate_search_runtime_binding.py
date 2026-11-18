"""PostgreSQL Candidate binding, canonical Bundle, adapter replay, and row-lock tests.

The lock-only test substitutes Bundle verification; the adapter flow uses a persisted
canonical Bundle. Synthetic Source/build inputs and fixture activation do not establish
Candidate Index artifact mapping or Source approval. Each test drops only its random schema.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
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
from app.core import config
from app.core.db.databases import Base
from app.core.errors import ApiError
from app.dtos.medication_candidates import ConfirmMedicationCandidateRequest, RejectMedicationCandidateRequest
from app.models.async_jobs import IdempotencyRecord
from app.models.medical_documents import MedicalDocument
from app.models.ocr import OcrJob
from app.models.prescriptions import Prescription, PrescriptionVersion, PrescriptionVersionMedication
from app.models.profiles import Profile, ProfileType
from app.models.rag_candidate import (
    MedicationCandidateSearch,
    MedicationCandidateSearchStatus,
    MedicationIdentification,
)
from app.models.rag_runtime import RagRuntimeBundleStatus, RagRuntimeEnvironmentStatus
from app.models.users import User
from app.repositories.idempotency_repository import IdempotencyRepository
from app.repositories.medication_candidate_repository import (
    MedicationCandidateRepository,
    MedicationCandidateResultCreate,
)
from app.repositories.rag_runtime_repository import RagRuntimeEnvironmentCreate, RagRuntimeRepository
from app.repositories.rag_source_catalog_repository import (
    RagSourceCatalogRepository,
    RagSourceCreate,
    RagSourceEndpointCreate,
    RagSourceOperationCreate,
    RagSourceSnapshotCreate,
)
from app.services.candidate_runtime_binding import require_current_candidate_runtime
from app.services.idempotency import SyncMutationIdempotencyService, get_default_snapshot_cipher
from app.services.medication_candidates import MedicationCandidateService
from app.services.medication_identification import MedicationIdentificationService
from app.services.rag_runtime_bundle_build import execute_runtime_bundle_build, verify_persisted_bundle_manifest_hash
from app.tests.fixtures.prescription_fingerprint import fingerprint_values


@pytest_asyncio.fixture
async def isolated_environment():
    if "test" not in config.DB_NAME.lower():
        pytest.fail("Candidate lock test requires a dedicated test database")
    schema = "candidate_env_lock_" + uuid4().hex
    environment_id, bundle_a, bundle_b = uuid4(), uuid4(), uuid4()
    engine = create_async_engine(config.database_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            await connection.execute(
                text(
                    "CREATE TABLE rag_runtime_environment ("
                    "id CHAR(36) PRIMARY KEY, environment_code VARCHAR(50) NOT NULL, "
                    "environment_status VARCHAR(20) NOT NULL, active_bundle_id CHAR(36), "
                    "active_bundle_manifest_hash VARCHAR(64), environment_revision INTEGER NOT NULL, "
                    "governance_revision_ref VARCHAR(255), safety_epoch INTEGER NOT NULL DEFAULT 1, "
                    "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO rag_runtime_environment "
                    "(id, environment_code, environment_status, active_bundle_id, "
                    "active_bundle_manifest_hash, environment_revision) "
                    "VALUES (:id, 'LOCAL', 'ACTIVE', :bundle, :hash, 1)"
                ),
                {"id": str(environment_id), "bundle": str(bundle_a), "hash": "a" * 64},
            )
        yield engine, schema, environment_id, bundle_a, bundle_b
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def _session(engine, schema: str) -> AsyncSession:
    session = AsyncSession(engine, expire_on_commit=False)
    await session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    return session


def _repository(session: AsyncSession, bundle_id: UUID, manifest_hash: str) -> RagRuntimeRepository:
    repository = RagRuntimeRepository(session)
    repository.get_release_bundle_by_id = AsyncMock(
        return_value=SimpleNamespace(
            id=bundle_id, bundle_manifest_hash=manifest_hash, bundle_status="READY", environment_code="LOCAL"
        )
    )
    return repository


@pytest.mark.asyncio
async def test_environment_transition_waits_for_validator_and_aba_stays_stale(isolated_environment) -> None:
    engine, schema, environment_id, bundle_a, bundle_b = isolated_environment
    first_session = await _session(engine, schema)
    transition_task = None
    transition_ready = asyncio.Event()
    try:
        old = await require_current_candidate_runtime(
            _repository(first_session, bundle_a, "a" * 64),
            runtime_environment_id=environment_id,
            verify_manifest=AsyncMock(return_value=True),
        )
        assert old.runtime_environment_revision == 1

        async def transition_to_b() -> None:
            async with await _session(engine, schema) as session:
                transition_ready.set()
                row = await RagRuntimeRepository(session).lock_environment_by_id(environment_id)
                assert row is not None
                row.active_bundle_id = bundle_b
                row.active_bundle_manifest_hash = "b" * 64
                row.environment_revision = 2
                await session.commit()

        transition_task = asyncio.create_task(transition_to_b())
        await asyncio.wait_for(transition_ready.wait(), timeout=3)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(transition_task), timeout=0.2)
        assert not transition_task.done()  # PostgreSQL FOR UPDATE is held until first commit.
        await first_session.commit()
        await asyncio.wait_for(transition_task, timeout=3)

        async with await _session(engine, schema) as session:
            row = await RagRuntimeRepository(session).lock_environment_by_id(environment_id)
            assert row is not None
            row.active_bundle_id = bundle_a
            row.active_bundle_manifest_hash = "a" * 64
            row.environment_revision = 3
            await session.commit()

        async with await _session(engine, schema) as session:
            repository = _repository(session, bundle_a, "a" * 64)
            with pytest.raises(ApiError) as error:
                await require_current_candidate_runtime(
                    repository,
                    runtime_environment_id=environment_id,
                    expected_binding=old,
                    verify_manifest=AsyncMock(return_value=True),
                )
            assert error.value.status_code == 409
            assert error.value.code == "IDENTIFICATION_CONTEXT_STALE"
            current = await require_current_candidate_runtime(
                repository,
                runtime_environment_id=environment_id,
                verify_manifest=AsyncMock(return_value=True),
            )
            assert current.runtime_release_bundle_id == old.runtime_release_bundle_id
            assert current.runtime_release_bundle_manifest_hash == old.runtime_release_bundle_manifest_hash
            assert current.runtime_environment_revision == 3
    finally:
        if transition_task is not None and not transition_task.done():
            transition_task.cancel()
            await asyncio.gather(transition_task, return_exceptions=True)
        await first_session.close()


def _candidate_tables() -> list[Table]:
    roots = (
        "medication_candidate_search_result",
        "medication_identification",
        "idempotency_record",
        "rag_runtime_environment",
        "rag_runtime_bundle_source",
        "rag_runtime_bundle_citation_approval",
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
async def full_candidate_schema() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if "test" not in config.DB_NAME.lower():
        pytest.fail("Candidate adapter integration requires a dedicated test database")
    schema = "candidate_bound_flow_" + uuid4().hex
    admin = create_async_engine(config.database_url, poolclass=NullPool, hide_parameters=True)
    engine = create_async_engine(
        config.database_url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=_candidate_tables()))
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


async def _source_snapshot(session: AsyncSession, label: str):
    suffix = uuid4().hex[:10]
    repository = RagSourceCatalogRepository(session)
    source = await repository.create_source(
        RagSourceCreate(source_code=f"BIND_{label}_{suffix}", display_name="Synthetic source")
    )
    endpoint = await repository.create_endpoint(
        RagSourceEndpointCreate(source_id=source.id, endpoint_code="PRODUCT_LIST", display_name="Product list")
    )
    operation = await repository.create_operation(
        RagSourceOperationCreate(endpoint_id=endpoint.id, operation_code="LIST_PRODUCTS", display_name="List products")
    )
    return await repository.create_snapshot(
        RagSourceSnapshotCreate(
            operation_id=operation.id,
            source_version=f"api:2026-10-23:{suffix}",
            raw_manifest_checksum="a" * 64,
            canonical_checksum="b" * 64,
            schema_version="schema-v1",
            parser_version="parser-v1",
            normalization_version="normalization-v1",
            canonicalization_spec_version="canonical-v1",
            record_count=1,
            rejected_record_count=0,
            collected_at=datetime.now(UTC),
        )
    )


def _member(snapshot, purpose: RuntimeBundleMemberPurpose) -> RuntimeBundleSourceMemberInput:
    return RuntimeBundleSourceMemberInput(
        source_snapshot_id=str(snapshot.id),
        source_purpose=purpose,
        source_version=snapshot.source_version,
        canonical_checksum=snapshot.canonical_checksum,
        approval_version="synthetic-approval-v1",
        scope_policy_hash="c" * 64,
        freshness_policy_hash="d" * 64,
        observed_environment="LOCAL",
        verification_status=SnapshotVerificationStatus.CURRENT,
        rejected_record_count=0,
        publication_approval_passed=True,
        freshness_eligible=True,
        provenance_valid=True,
        approval_expired=False,
        revocation_unresolved=False,
        scope_allowed=True,
    )


async def _build_bundle(session: AsyncSession) -> tuple[UUID, str, UUID]:
    catalog = await _source_snapshot(session, "CATALOG")
    knowledge = await _source_snapshot(session, "KNOWLEDGE")
    unique = uuid4().hex
    request = RuntimeBundleBuildRequest(
        bundle_key="candidate-binding-test",
        bundle_version=unique,
        environment_code="LOCAL",
        execution_manifest=RuntimeExecutionManifestInput(
            manifest_key="candidate-binding-test",
            manifest_version=unique,
            schema_version="runtime-manifest-v1",
            git_commit_sha="abcdef1",
            worker_artifact_ref="worker:synthetic",
        ),
        catalog=MedicationCatalogBinding(
            catalog_version="synthetic-catalog-v1",
            catalog_manifest_hash="9" * 64,
            verification_status=CatalogVerificationStatus.APPROVED,
            freshness_status=CatalogFreshnessStatus.CURRENT,
            is_complete=True,
            source_snapshot_ids=(str(catalog.id),),
        ),
        source_members=(
            _member(catalog, RuntimeBundleMemberPurpose.CATALOG),
            _member(knowledge, RuntimeBundleMemberPurpose.KNOWLEDGE),
        ),
        artifact_members=(
            RuntimeBundleArtifactMemberInput(
                artifact_kind=RuntimeBundleArtifactKind.CANDIDATE_INDEX,
                artifact_ref="candidate-index:synthetic",
                artifact_version="1.0.0",
                observed_environment="LOCAL",
                approval_effective=True,
                approval_expired=False,
                revocation_unresolved=False,
                manifest_hash="e" * 64,
                catalog_version="synthetic-catalog-v1",
                catalog_manifest_hash="9" * 64,
            ),
        ),
        created_by="candidate-binding-test",
    )
    execution = await execute_runtime_bundle_build(session, request)
    assert execution.outcome.decision is RuntimeBundleBuildDecision.BUILDABLE
    assert execution.persisted is not None
    bundle = execution.persisted.bundle
    bundle.bundle_status = RagRuntimeBundleStatus.READY
    bundle.ready_at = datetime.now(UTC)
    environment = await RagRuntimeRepository(session).create_environment(
        RagRuntimeEnvironmentCreate(environment_code="LOCAL", environment_status=RagRuntimeEnvironmentStatus.ACTIVE)
    )
    environment.active_bundle_id = bundle.id
    environment.active_bundle_manifest_hash = bundle.bundle_manifest_hash
    await session.flush()
    assert await verify_persisted_bundle_manifest_hash(session, bundle.id)
    return bundle.id, bundle.bundle_manifest_hash, environment.id


async def _prescription_medication(session: AsyncSession) -> tuple[User, PrescriptionVersionMedication]:
    user = User(
        email=f"candidate-bound-{uuid4().hex[:10]}@example.com",
        hashed_password="synthetic-hash",
        name="Synthetic Owner",
    )
    session.add(user)
    await session.flush()
    profile = Profile(user_id=user.id, profile_type=ProfileType.SELF, display_name=user.name)
    session.add(profile)
    await session.flush()
    document = MedicalDocument(
        uploaded_by=user.id,
        profile_id=profile.id,
        original_file_name="synthetic-prescription.jpg",
        object_key=f"synthetic/{uuid4()}.jpg",
        file_mime_type="image/jpeg",
        file_size_bytes=100,
    )
    session.add(document)
    await session.flush()
    job = OcrJob(document_id=document.id)
    session.add(job)
    await session.flush()
    prescription_id, version_id = uuid4(), uuid4()
    prescribed_date = date.today()
    confirmed_at = datetime.now(config.TIMEZONE)
    prescription = Prescription(
        id=prescription_id,
        active_version_id=version_id,
        document_id=document.id,
        source_ocr_job_id=job.id,
        profile_id=profile.id,
        prescribed_date=prescribed_date,
        confirmed_at=confirmed_at,
    )
    version = PrescriptionVersion(
        **fingerprint_values(
            prescribed_date,
            [{"medication_name": "Synthetic Tablet", "strength_text": "500mg", "display_order": 1}],
        ),
        id=version_id,
        prescription_id=prescription_id,
        version_number=1,
        prescribed_date=prescribed_date,
        confirmed_at=confirmed_at,
    )
    medication = PrescriptionVersionMedication(
        medication_count=1,
        prescription_version_id=version_id,
        medication_name="Synthetic Tablet",
        strength_text="500mg",
        display_order=1,
    )
    session.add_all([prescription, version, medication])
    await session.flush()
    return user, medication


def _candidate_service(session: AsyncSession) -> MedicationCandidateService:
    repository = MedicationCandidateRepository(session)
    return MedicationCandidateService(
        repository,
        MedicationIdentificationService(repository),
        SyncMutationIdempotencyService(IdempotencyRepository(session), get_default_snapshot_cipher()),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm", "reject"])
async def test_bound_candidate_adapter_replays_then_rejects_new_key_after_revision_aba(
    full_candidate_schema: async_sessionmaker[AsyncSession], action: str
) -> None:
    sessions = full_candidate_schema
    async with sessions.begin() as session:
        bundle_id, bundle_hash, environment_id = await _build_bundle(session)
        owner, medication = await _prescription_medication(session)
        identification = MedicationIdentificationService(MedicationCandidateRepository(session))
        search = (
            await identification.record_candidate_search(
                prescription_version_medication_id=medication.id,
                user_id=owner.id,
                query_digest="synthetic-query-digest",
                runtime_release_bundle_id=bundle_id,
                candidate_index_version_id=uuid4(),
                runtime_environment_id=environment_id,
                expires_at=datetime.now(config.TIMEZONE) + timedelta(minutes=10),
            )
        ).search
        result = await identification.finalize_candidate_search(
            search_id=search.id,
            user_id=owner.id,
            status=MedicationCandidateSearchStatus.READY,
            results=[
                MedicationCandidateResultCreate(
                    product_id=uuid4(),
                    code_system="MFDS_ITEM_SEQ",
                    canonical_code="200012345",
                    product_name="Synthetic Tablet",
                    strength_text="500mg",
                    dosage_form="tablet",
                    manufacturer_name="Synthetic Manufacturer",
                    product_status="ACTIVE",
                    result_rank=1,
                    result_score=0.95,
                    result_method="PRODUCT_NAME",
                    is_displayed=True,
                    selection_eligible=True,
                )
            ],
        )
        result_id = result.results[0].id
        owner_id, medication_id, search_id = owner.id, medication.id, search.id
    async with sessions() as session:
        persisted = await session.get(MedicationCandidateSearch, search_id)
        assert persisted is not None
        assert (
            persisted.runtime_release_bundle_id,
            persisted.runtime_release_bundle_manifest_hash,
            persisted.runtime_environment_id,
            persisted.runtime_environment_revision,
        ) == (bundle_id, bundle_hash, environment_id, 1)
        assert await verify_persisted_bundle_manifest_hash(session, bundle_id)

    key = f"candidate-bound-{action}-{uuid4().hex}"
    request = (
        ConfirmMedicationCandidateRequest(
            prescription_version_medication_id=medication_id,
            candidate_search_result_id=result_id,
        )
        if action == "confirm"
        else RejectMedicationCandidateRequest(
            search_id=search_id,
            candidate_search_result_id=result_id,
        )
    )

    async def attempt(idempotency_key: str):
        async with sessions() as session:
            adapter = _candidate_service(session)
            try:
                if action == "confirm":
                    response = await adapter.confirm_candidate(
                        user=SimpleNamespace(id=owner_id), request=request, idempotency_key=idempotency_key
                    )
                else:
                    response = await adapter.reject_candidate(
                        user=SimpleNamespace(id=owner_id), request=request, idempotency_key=idempotency_key
                    )
                await session.commit()
                return response.model_dump(mode="json")
            except Exception:
                await session.rollback()
                raise

    first_response = await attempt(key)
    assert first_response["prescription_version_medication_id"] == str(medication_id)
    # Synthetic fixture transition: A -> suspended -> A. The production transition
    # approval protocol is a separate gate; only its revision fencing is under test.
    for status, revision in ((RagRuntimeEnvironmentStatus.SUSPENDED, 2), (RagRuntimeEnvironmentStatus.ACTIVE, 3)):
        async with sessions.begin() as session:
            environment = await RagRuntimeRepository(session).lock_environment_by_id(environment_id)
            assert environment is not None
            environment.environment_status = status
            environment.environment_revision = revision

    assert await attempt(key) == first_response
    with pytest.raises(ApiError) as error:
        await attempt(key + "-new")
    assert error.value.status_code == 409
    assert error.value.code == "IDENTIFICATION_CONTEXT_STALE"
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MedicationIdentification)) == 1
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1
