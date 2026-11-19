"""CR-A Sync 201 integration against a real isolated PostgreSQL schema.

The policy evaluator and Worker executor are synthetic. Bundle, retrieval
binding, request issuer, lifecycle, finalizer, persistence and HTTP are real.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import platform
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import main as main_module
from app.core.db.databases import get_db_session
from app.dependencies.security import get_request_user
from app.dependencies.services import get_consent_gate_service, get_guide_generator
from app.main import fastapi_app
from app.models.async_jobs import AiJob, AiJobStatus
from app.models.guides import Guide, GuideCitation, GuideGenerationStatus
from app.models.prescriptions import Prescription
from app.models.rag_candidate import (
    MedicationIdentification,
    MedicationIdentificationSource,
    MedicationIdentificationStatus,
)
from app.models.rag_request_guard_runtime_binding import RagRequestGuardRuntimeBinding
from app.models.rag_runtime import RagRuntimeEnvironment, RagRuntimeExecutionManifest
from app.models.users import User
from app.repositories.prescription_repository import PrescriptionRepository
from app.services.guide_ai.generator import GuideGenerator
from app.services.guide_request_authority import (
    GuideRequestAuthorityEvaluation,
    GuideRequestAuthorityInput,
    GuideRequestAuthorityIssuer,
)
from app.services.user_consents import ConsentGateService
from app.tests.services.test_guides import _UnusedGuideProvider
from rag_runtime.guide_release_projection import (
    GUIDE_RUNTIME_RELEASE_PROJECTION_CARRIER_VERSION,
    GuideRuntimeApprovedAnswer,
    GuideRuntimeCitationSourceType,
    GuideRuntimeReleaseDecision,
    GuideRuntimeReleaseProjectionCarrier,
    GuideRuntimeVerifiedCitation,
)
from rag_runtime.guide_runtime_execution import (
    GuideRuntimeExecutionFailure,
    GuideRuntimeExecutionRequest,
    GuideRuntimeExecutionResult,
    GuideRuntimeProviderProvenance,
)
from rag_runtime.request_authority import RequestAuthorityDecisionOutcome
from scripts.benchmark_guide_sync_cra import PhaseTimes, run_benchmark

if TYPE_CHECKING:
    from tests.fixtures.guide_sync_cra import CraRuntimeFixture

pytestmark = pytest.mark.asyncio
pytest_plugins = ["tests.fixtures.guide_sync_cra"]
_bootstrap: main_module.GuideRuntimeBootstrap | None = None


def build_cra_bootstrap() -> main_module.GuideRuntimeBootstrap:
    assert _bootstrap is not None
    return _bootstrap


class SyntheticEvaluator:
    """Explicit trusted-policy stand-in; it never reads live medical policy."""

    def __init__(self) -> None:
        self.outcome = RequestAuthorityDecisionOutcome.PASS
        self.scopes = ("GUIDE",)
        self.patient_digest: str | None = None
        self.calls: list[GuideRequestAuthorityInput] = []
        self.on_evaluate: Callable[[GuideRequestAuthorityInput], Awaitable[None]] | None = None

    async def evaluate(
        self, *, session: AsyncSession, request: GuideRequestAuthorityInput
    ) -> GuideRequestAuthorityEvaluation:
        _ = session
        self.calls.append(request)
        if self.on_evaluate is not None:
            await self.on_evaluate(request)
        return GuideRequestAuthorityEvaluation(self.outcome, self.scopes, self.patient_digest)


class IssuerFactory:
    def __init__(self, evaluator: SyntheticEvaluator) -> None:
        self.evaluator = evaluator

    def for_session(self, session: AsyncSession) -> GuideRequestAuthorityIssuer:
        return GuideRequestAuthorityIssuer(session, environment_code="LOCAL", evaluator=self.evaluator)


class SyntheticExecutor:
    def __init__(
        self,
        fixture: CraRuntimeFixture,
        on_execute: Callable[[GuideRuntimeExecutionRequest], Awaitable[None]] | None = None,
        failure: GuideRuntimeExecutionFailure | None = None,
    ) -> None:
        self.fixture = fixture
        self.on_execute = on_execute
        self.failure = failure
        self.delay_ms = 0
        self.calls: list[GuideRuntimeExecutionRequest] = []

    def create(self) -> SyntheticExecutor:
        return self

    async def execute(self, request: GuideRuntimeExecutionRequest) -> GuideRuntimeExecutionResult:
        self.calls.append(request)
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000)
        if self.on_execute is not None:
            await self.on_execute(request)
        if self.failure is not None:
            return GuideRuntimeExecutionResult.failed(self.failure)
        source = self.fixture
        projection = GuideRuntimeReleaseProjectionCarrier(
            contract_version=GUIDE_RUNTIME_RELEASE_PROJECTION_CARRIER_VERSION,
            release_decision=GuideRuntimeReleaseDecision.PASS,
            is_current=True,
            answer=GuideRuntimeApprovedAnswer(
                claim_action_texts=("합성 근거로 확인된 복약 정보를 의료진과 확인하세요.",),
                uncertainty_text="변경 사항은 의료진에게 확인하세요.",
                consultation_text="이상 반응이 있으면 의료진과 상담하세요.",
            ),
            fallback=None,
            citations=(
                GuideRuntimeVerifiedCitation(
                    card_target_ref="card:cra-synthetic",
                    claim_key="claim:cra-synthetic",
                    evidence_key="evidence:cra-synthetic",
                    source_type=GuideRuntimeCitationSourceType.LIFESTYLE_GUIDELINE,
                    source_snapshot_id=source.knowledge_snapshot.id,
                    source_snapshot_member_id=source.knowledge_member.id,
                    source_code=source.knowledge_source_code,
                    source_version=source.knowledge_snapshot.source_version,
                    locator=source.knowledge_member.locator,
                    content_sha256=source.knowledge_member.content_sha256,
                    display_order=1,
                ),
            ),
        )
        return GuideRuntimeExecutionResult.succeeded(
            projection,
            GuideRuntimeProviderProvenance(model_name="synthetic-cra", prompt_version="synthetic-cra-v1"),
        )


@dataclass
class CraCase:
    data: CraRuntimeFixture
    sessions: async_sessionmaker[AsyncSession]
    client: AsyncClient
    evaluator: SyntheticEvaluator
    executor: SyntheticExecutor
    active_sessions: list[AsyncSession]

    async def post(self):
        return await self.client.post("/api/v1/guides", json={"prescription_id": str(self.data.prescription.id)})


@pytest_asyncio.fixture
async def cra_case(
    cra_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[CraCase]:
    global _bootstrap
    from tests.fixtures.guide_sync_cra import seed_cra_runtime

    async with cra_session_factory.begin() as session:
        data = await seed_cra_runtime(session)

    evaluator = SyntheticEvaluator()
    executor = SyntheticExecutor(data)
    active_sessions: list[AsyncSession] = []
    consent_gate = AsyncMock(spec=ConsentGateService)
    generator = GuideGenerator(provider=_UnusedGuideProvider(), model="unused", timeout_seconds=1.0)
    openai_client = SimpleNamespace(close=AsyncMock())

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with cra_session_factory() as session:
            active_sessions.append(session)
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                active_sessions.remove(session)

    async def override_user():
        return data.user

    _bootstrap = main_module.GuideRuntimeBootstrap(
        provider_dependencies=object(),
        authority_provider=IssuerFactory(evaluator),
    )
    monkeypatch.setattr(main_module, "get_email_sender", lambda: object())
    monkeypatch.setattr(main_module, "AsyncOpenAI", lambda **_kwargs: openai_client)
    monkeypatch.setattr(main_module, "close_database", AsyncMock())
    monkeypatch.setattr(main_module, "build_production_guide_runtime_executor_factory", lambda *_a, **_k: executor)
    monkeypatch.setattr(main_module.config, "GUIDE_RUNTIME_ENABLED", True)
    monkeypatch.setattr(main_module.config, "GUIDE_RUNTIME_BOOTSTRAP_FACTORY", f"{__name__}:build_cra_bootstrap")
    fastapi_app.dependency_overrides[get_db_session] = override_db
    fastapi_app.dependency_overrides[get_request_user] = override_user
    fastapi_app.dependency_overrides[get_consent_gate_service] = lambda: consent_gate
    fastapi_app.dependency_overrides[get_guide_generator] = lambda: generator
    try:
        async with main_module.lifespan(fastapi_app):
            async with AsyncClient(transport=ASGITransport(app=main_module.app), base_url="http://test") as client:
                yield CraCase(data, cra_session_factory, client, evaluator, executor, active_sessions)
    finally:
        _bootstrap = None
        for dependency in (get_db_session, get_request_user, get_consent_gate_service, get_guide_generator):
            fastapi_app.dependency_overrides.pop(dependency, None)
        for key in (
            "guide_runtime_provider_dependencies",
            "guide_sync_runtime_authority_provider",
            "guide_runtime_executor_factory",
            "openai_client",
        ):
            if hasattr(fastapi_app.state, key):
                delattr(fastapi_app.state, key)


async def test_sync_201_persists_and_rediscovery_reads_same_release(cra_case: CraCase) -> None:
    created = await cra_case.post()
    assert created.status_code == 201, created.text
    payload = created.json()["data"]
    rediscovered = await cra_case.client.get(f"/api/v1/guides/{payload['guide_id']}")
    assert rediscovered.status_code == 200
    assert rediscovered.json()["data"] == payload
    assert payload["generation_status"] == "COMPLETED"
    assert payload["release_decision"] == "PASS"
    assert payload["model_name"] == "synthetic-cra"
    assert len(payload["citations"]) == 1
    assert created.headers["cache-control"] == "no-store"
    assert rediscovered.headers["cache-control"] == "no-store"
    assert len(cra_case.executor.calls) == 1
    async with cra_case.sessions() as session:
        guide = await session.get(Guide, UUID(payload["guide_id"]))
        assert guide is not None and guide.ai_job_id is not None
        job = await session.get(AiJob, guide.ai_job_id)
        assert job is not None and job.status == AiJobStatus.COMPLETED


async def test_two_requests_have_distinct_persisted_request_guard_decisions(cra_case: CraCase) -> None:
    first = await cra_case.post()
    second = await cra_case.post()
    assert first.status_code == second.status_code == 201
    assert first.json()["data"]["guide_id"] != second.json()["data"]["guide_id"]
    async with cra_case.sessions() as session:
        guards = (await session.scalars(select(RagRequestGuardRuntimeBinding))).all()
    assert len(guards) == 2
    assert guards[0].request_guard_decision_id != guards[1].request_guard_decision_id
    assert guards[0].artifact_content_sha256 != guards[1].artifact_content_sha256


async def test_policy_fail_audit_commits_and_executor_is_not_called(cra_case: CraCase) -> None:
    cra_case.evaluator.outcome = RequestAuthorityDecisionOutcome.FAIL
    response = await cra_case.post()
    assert response.status_code == 503, response.text
    assert response.headers["cache-control"] == "no-store"
    assert cra_case.executor.calls == []
    async with cra_case.sessions() as session:
        guards = (await session.scalars(select(RagRequestGuardRuntimeBinding))).all()
        assert len(guards) == 1
        assert guards[0].actual_decision_outcome == RequestAuthorityDecisionOutcome.FAIL


async def test_preflight_failure_rolls_back_guard_and_job_but_records_failed_guide(cra_case: CraCase) -> None:
    async with cra_case.sessions.begin() as session:
        identification = await session.scalar(
            select(MedicationIdentification).where(
                MedicationIdentification.prescription_version_medication_id == cra_case.data.medication_id
            )
        )
        assert identification is not None
        await session.delete(identification)

    response = await cra_case.post()
    assert response.status_code != 201, response.text
    assert cra_case.executor.calls == []
    async with cra_case.sessions() as session:
        guards = (await session.scalars(select(RagRequestGuardRuntimeBinding))).all()
        jobs = (await session.scalars(select(AiJob).where(AiJob.user_id == cra_case.data.user.id))).all()
        guides = (await session.scalars(select(Guide))).all()
    assert guards == []
    assert jobs == []
    assert len(guides) == 1 and guides[0].generation_status == GuideGenerationStatus.FAILED


async def test_execution_runs_after_intake_commit_without_environment_lock(cra_case: CraCase) -> None:
    async def check(request: GuideRuntimeExecutionRequest) -> None:
        assert not cra_case.active_sessions[0].in_transaction()
        async with cra_case.sessions.begin() as concurrent:
            environment = await concurrent.scalar(
                select(RagRuntimeEnvironment)
                .where(RagRuntimeEnvironment.id == cra_case.data.environment.id)
                .with_for_update(nowait=True)
            )
            assert environment is not None
            guard = await concurrent.scalar(
                select(RagRequestGuardRuntimeBinding).where(
                    RagRequestGuardRuntimeBinding.request_guard_decision_id
                    == UUID(request.runtime_request.runtime_guard_decision_ref)
                )
            )
            assert guard is not None
        await asyncio.sleep(0.05)

    cra_case.executor.on_execute = check
    response = await cra_case.post()
    assert response.status_code == 201, response.text


async def test_environment_revision_change_during_execution_blocks_release(cra_case: CraCase) -> None:
    async def change(_request: GuideRuntimeExecutionRequest) -> None:
        async with cra_case.sessions.begin() as concurrent:
            environment = await concurrent.scalar(
                select(RagRuntimeEnvironment)
                .where(RagRuntimeEnvironment.id == cra_case.data.environment.id)
                .with_for_update(nowait=True)
            )
            assert environment is not None
            environment.environment_revision += 1

    cra_case.executor.on_execute = change
    response = await cra_case.post()
    assert response.status_code == 409, response.text
    async with cra_case.sessions() as session:
        guide = await session.scalar(select(Guide).order_by(Guide.requested_at.desc()).limit(1))
        assert guide is not None
        assert guide.generation_status != GuideGenerationStatus.COMPLETED or guide.release_decision != "PASS"


@pytest.mark.parametrize("changed_pin", ["prescription_version", "execution_manifest", "identification"])
async def test_pinned_authority_change_during_execution_persists_stale_release(
    cra_case: CraCase, changed_pin: str
) -> None:
    async def change(_request: GuideRuntimeExecutionRequest) -> None:
        async with cra_case.sessions.begin() as concurrent:
            if changed_pin == "prescription_version":
                prescription = await concurrent.get(Prescription, cra_case.data.prescription.id)
                assert prescription is not None
                await PrescriptionRepository(concurrent).create_version(
                    prescription=prescription,
                    prescribed_date=prescription.prescribed_date,
                    confirmed_at=datetime.now(UTC),
                    medications=[{"medication_name": "변경된 합성 식별약", "display_order": 1}],
                )
            elif changed_pin == "execution_manifest":
                manifest = await concurrent.scalar(
                    select(RagRuntimeExecutionManifest).where(
                        RagRuntimeExecutionManifest.id == cra_case.data.bundle.execution_manifest_id
                    )
                )
                assert manifest is not None
                # The exact hash and Bundle pointer are FK-pinned. Mutating an
                # input under the unchanged hash makes canonical readback fail.
                manifest.worker_artifact_ref = "worker:synthetic:changed-after-intake"
            else:
                identification = await concurrent.scalar(
                    select(MedicationIdentification).where(
                        MedicationIdentification.prescription_version_medication_id == cra_case.data.medication_id
                    )
                )
                assert identification is not None
                identification.status = MedicationIdentificationStatus.UNRESOLVED
                identification.source = MedicationIdentificationSource.USER_REJECTED
                identification.product_id = None
                identification.code_system = None
                identification.canonical_code = None
                identification.confirmed_at = None
                identification.rejected_at = datetime.now(UTC)
                identification.decision_reason = "USER_REJECTED_DISPLAYED_CANDIDATE"

    cra_case.executor.on_execute = change
    response = await cra_case.post()
    assert response.status_code == 409, response.text
    async with cra_case.sessions() as session:
        guide = await session.scalar(select(Guide).order_by(Guide.requested_at.desc()).limit(1))
        assert guide is not None and guide.ai_job_id is not None
        job = await session.get(AiJob, guide.ai_job_id)
        citations = (await session.scalars(select(GuideCitation).where(GuideCitation.guide_id == guide.id))).all()
        assert guide.release_decision == "STALE"
        assert guide.release_is_current is False
        assert guide.content is None
        assert citations == []
        assert job is not None and job.status == AiJobStatus.STALE


@pytest.mark.parametrize("changed_fact", ["patient_digest", "source_scope", "decision"])
async def test_evaluator_fact_change_during_execution_blocks_release(cra_case: CraCase, changed_fact: str) -> None:
    async def change(_request: GuideRuntimeExecutionRequest) -> None:
        if changed_fact == "patient_digest":
            cra_case.evaluator.patient_digest = "a" * 64
        elif changed_fact == "source_scope":
            cra_case.evaluator.scopes = ("GUIDE", "PATIENT_CITATION")
        else:
            cra_case.evaluator.outcome = RequestAuthorityDecisionOutcome.FAIL

    cra_case.executor.on_execute = change
    response = await cra_case.post()
    assert response.status_code == 409, response.text
    assert len(cra_case.evaluator.calls) >= 2
    async with cra_case.sessions() as session:
        guide = await session.scalar(select(Guide).order_by(Guide.requested_at.desc()).limit(1))
        assert guide is not None and guide.release_decision != "PASS"


async def test_user_deactivation_during_execution_blocks_release(cra_case: CraCase) -> None:
    async def deactivate(_request: GuideRuntimeExecutionRequest) -> None:
        async with cra_case.sessions.begin() as concurrent:
            user = await concurrent.get(User, cra_case.data.user.id, with_for_update=True)
            assert user is not None
            user.is_active = False

    cra_case.executor.on_execute = deactivate
    response = await cra_case.post()
    assert response.status_code == 409, response.text
    async with cra_case.sessions() as session:
        guide = await session.scalar(select(Guide).order_by(Guide.requested_at.desc()).limit(1))
        assert guide is not None and guide.release_decision != "PASS"


async def test_finalization_holds_user_lock_while_policy_revalidates(cra_case: CraCase) -> None:
    async def attempt_concurrent_update(_request: GuideRequestAuthorityInput) -> None:
        if len(cra_case.evaluator.calls) != 2:
            return
        async with cra_case.sessions() as concurrent:
            await concurrent.execute(text("SET LOCAL lock_timeout = '50ms'"))
            with pytest.raises(DBAPIError) as blocked:
                await concurrent.execute(
                    update(User).where(User.id == cra_case.data.user.id).values(name="차단된 합성 변경")
                )
            assert getattr(blocked.value.orig, "sqlstate", None) == "55P03"
            await concurrent.rollback()

    cra_case.evaluator.on_evaluate = attempt_concurrent_update
    response = await cra_case.post()
    assert response.status_code == 201, response.text
    assert len(cra_case.evaluator.calls) == 2


async def test_provider_failure_is_saved_without_public_release(cra_case: CraCase) -> None:
    cra_case.executor.failure = GuideRuntimeExecutionFailure.RELEASE_UNAVAILABLE
    response = await cra_case.post()
    assert response.status_code == 503, response.text
    async with cra_case.sessions() as session:
        guide = await session.scalar(select(Guide).order_by(Guide.requested_at.desc()).limit(1))
        assert guide is not None and guide.ai_job_id is not None
        job = await session.get(AiJob, guide.ai_job_id)
        assert job is not None and job.status == AiJobStatus.FAILED
        assert guide.release_decision != "PASS"


async def test_late_provider_failure_does_not_overwrite_terminal_job(cra_case: CraCase) -> None:
    async def terminalize(request: GuideRuntimeExecutionRequest) -> None:
        async with cra_case.sessions.begin() as concurrent:
            job = await concurrent.get(AiJob, request.runtime_request.job_id, with_for_update=True)
            assert job is not None
            job.status = AiJobStatus.FAILED
            job.lease_token = None
            job.lease_expires_at = None
            job.failure_code = "DEPENDENCY_UNAVAILABLE"
            job.failure_detail = "synthetic-prior-terminal"
            job.completed_at = datetime.now(UTC)

    cra_case.executor.on_execute = terminalize
    cra_case.executor.failure = GuideRuntimeExecutionFailure.RELEASE_UNAVAILABLE
    response = await cra_case.post()
    assert response.status_code == 503, response.text
    async with cra_case.sessions() as session:
        job = await session.scalar(select(AiJob).where(AiJob.user_id == cra_case.data.user.id))
        assert job is not None
        assert job.status == AiJobStatus.FAILED
        assert job.failure_detail == "synthetic-prior-terminal"


@pytest.mark.skipif(os.environ.get("DOSEY_CRA_BENCHMARK") != "1", reason="explicit benchmark opt-in")
async def test_benchmark_sync_201_synthetic_executor(cra_case: CraCase, monkeypatch: pytest.MonkeyPatch) -> None:
    """75 HTTP requests on real PG; excludes retrieval, live LLM, and medical quality."""

    from app.services.guide_sync_runtime_execution import GuideSyncRuntimeExecution

    observed: list[PhaseTimes] = []
    original = GuideSyncRuntimeExecution.execute

    async def observed_execute(self, *, user, guide):
        try:
            return await original(self, user=user, guide=guide)
        finally:
            timing = self.timings
            observed.append(PhaseTimes(timing.intake_seconds, timing.execution_seconds, timing.finalize_seconds))

    monkeypatch.setattr(GuideSyncRuntimeExecution, "execute", observed_execute)

    def read_latest() -> PhaseTimes:
        return observed[-1]

    source_paths = (
        Path("backend/app/services/guide_sync_runtime_execution.py"),
        Path("backend/app/services/guide_sync_runtime_finalizer.py"),
        Path("backend/app/services/guide_request_authority.py"),
    )
    source_hash = hashlib.sha256()
    for source_path in source_paths:
        source_hash.update(str(source_path).encode() + b"\0" + source_path.read_bytes())
    async with cra_case.sessions() as session:
        pg_version = await session.scalar(text("SHOW server_version"))
    assert isinstance(pg_version, str)

    result = await run_benchmark(
        client=cra_case.client,
        prescription_id=str(cra_case.data.prescription.id),
        set_executor_delay_ms=lambda delay: setattr(cra_case.executor, "delay_ms", delay),
        read_phase_times=read_latest,
        output_path=Path("/private/tmp/dosey-cra-20261104-benchmark.json"),
        metadata={
            "source_sha256": source_hash.hexdigest(),
            "python_version": platform.python_version(),
            "postgresql_version": pg_version,
            "concurrency": 1,
            "http_transport": "httpx.ASGITransport in-process",
            "database": "dedicated PostgreSQL test schema",
            "provider": "synthetic executor; zero OpenAI calls",
        },
    )
    assert len(result["cases"]) == 60
