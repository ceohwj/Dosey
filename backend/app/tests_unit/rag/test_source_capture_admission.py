from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.repositories.rag_source_capture_observation_repository import SourceCaptureConflictError
from app.services.source_capture_admission import SyntheticPrivateAcquisition, SyntheticSourceCaptureAdmission
from rag_runtime.source_capture_observation import SourceCaptureIssuerRef, SourceCaptureObservation


class RecordingRepository:
    def __init__(self):
        self.calls = []

    async def record(self, observation):
        self.calls.append(observation)
        return "a" * 64


def _case():
    started = datetime(2026, 11, 4, tzinfo=UTC)
    issuer = SourceCaptureIssuerRef("source_live_capture_issuer", "2.0", "c" * 64)
    obs = SourceCaptureObservation(
        capture_observation_id=uuid4(),
        ingestion_run_id=uuid4(),
        source_snapshot_id=uuid4(),
        source_version="v1",
        canonical_checksum="b" * 64,
        source_code="SYN_PRIVATE",
        endpoint_code="EP",
        operation_code="LIST",
        capture_protocol_version="v2",
        issuer_ref=issuer,
        request_profile_sha256="d" * 64,
        capture_origin="LIVE_PROVIDER",
        capture_started_at=started,
        capture_completed_at=started + timedelta(seconds=1),
        recorded_at=started + timedelta(seconds=2),
        raw_manifest_sha256="e" * 64,
        endpoint_contract_receipt_sha256="f" * 64,
        full_scan_completed=True,
        primary_key_validation_passed=True,
        page_count=1,
        record_count=1,
    )
    context = SyntheticPrivateAcquisition(
        new_run_id=obs.ingestion_run_id,
        capture_observation_id=obs.capture_observation_id,
        source_snapshot_id=obs.source_snapshot_id,
        source_code=obs.source_code,
        endpoint_code=obs.endpoint_code,
        operation_code=obs.operation_code,
        issuer_ref=issuer,
        request_profile_sha256=obs.request_profile_sha256,
        capture_started_at=obs.capture_started_at,
        capture_completed_at=obs.capture_completed_at,
        recorded_at=obs.recorded_at,
        origin="SIMULATED_LIVE",
    )
    return obs, context


@pytest.mark.asyncio
async def test_admits_original_v2_and_exact_retry():
    obs, context = _case()
    repo = RecordingRepository()
    service = SyntheticSourceCaptureAdmission(repo, issuer_ref=context.issuer_ref)
    assert await service.record(obs, context) == "a" * 64
    assert await service.record(obs, context) == "a" * 64
    assert repo.calls == [obs, obs]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("observation_change", "context_change"),
    [
        ({"capture_protocol_version": "v1"}, {}),
        ({"ingestion_run_id": uuid4()}, {}),
        ({"capture_observation_id": uuid4()}, {}),
        ({"request_profile_sha256": "0" * 64}, {}),
        ({"recorded_at": datetime(2026, 11, 5, tzinfo=UTC)}, {}),
        ({}, {"origin": "RAW_REPLAY"}),
        ({}, {"new_run_id": uuid4()}),
        ({}, {"source_code": "MFDS"}),
        ({}, {"issuer_ref": SourceCaptureIssuerRef("source_live_capture_issuer", "2.1", "c" * 64)}),
    ],
)
async def test_rejects_untrusted_or_mismatched_attempt(observation_change, context_change):
    obs, context = _case()
    repo = RecordingRepository()
    service = SyntheticSourceCaptureAdmission(repo, issuer_ref=context.issuer_ref)
    with pytest.raises(SourceCaptureConflictError):
        await service.record(replace(obs, **observation_change), replace(context, **context_change))
    assert repo.calls == []


@pytest.mark.asyncio
async def test_rejects_unregistered_issuer_pin_and_real_source():
    obs, context = _case()
    repo = RecordingRepository()
    service = SyntheticSourceCaptureAdmission(
        repo, issuer_ref=SourceCaptureIssuerRef("source_live_capture_issuer", "2.0", "0" * 64)
    )
    with pytest.raises(SourceCaptureConflictError):
        await service.record(obs, context)
    with pytest.raises(SourceCaptureConflictError):
        await SyntheticSourceCaptureAdmission(repo, issuer_ref=context.issuer_ref).record(
            replace(obs, source_code="MFDS"), replace(context, source_code="MFDS")
        )
    assert repo.calls == []
