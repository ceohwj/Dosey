"""Frozen synthetic capture projection, hash, and input validation."""

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator

from rag_runtime.source_capture_observation import (
    SourceCaptureIssuerRef,
    SourceCaptureObservation,
    compute_source_capture_observation_hash,
    source_capture_projection,
)

_SCHEMA = json.loads(Path("docs/contracts/targets/post-mvp-1/candidate-request-guard-audit-v1.schema.json").read_text())
_NOW = datetime(2026, 11, 4, tzinfo=UTC)


def example(**changes):
    values = dict(
        capture_observation_id=UUID(int=1),
        ingestion_run_id=UUID(int=2),
        source_snapshot_id=UUID(int=3),
        source_version="synthetic-v1",
        canonical_checksum="a" * 64,
        source_code="SYNTHETIC",
        endpoint_code="EP",
        operation_code="LIST",
        capture_protocol_version="v1",
        issuer_ref=SourceCaptureIssuerRef("source_live_capture_issuer", "1.0", "b" * 64),
        request_profile_sha256="c" * 64,
        capture_origin="LIVE_PROVIDER",
        capture_started_at=_NOW,
        capture_completed_at=_NOW + timedelta(seconds=1),
        recorded_at=_NOW + timedelta(seconds=2),
        raw_manifest_sha256="d" * 64,
        endpoint_contract_receipt_sha256="e" * 64,
        full_scan_completed=True,
        primary_key_validation_passed=True,
        page_count=1,
        record_count=1,
    )
    values.update(changes)
    return SourceCaptureObservation(**values)


def test_projection_matches_frozen_schema_and_utc_hash():
    obs = example(
        capture_started_at=_NOW.astimezone(timezone(timedelta(hours=9))),
        capture_completed_at=(_NOW + timedelta(seconds=1)).astimezone(timezone(timedelta(hours=9))),
    )
    projection = source_capture_projection(obs)
    Draft202012Validator({"$defs": _SCHEMA["$defs"], "$ref": "#/$defs/capture_projection"}).validate(projection)
    assert set(projection) == set(_SCHEMA["$defs"]["capture_projection"]["required"])
    assert projection["capture_started_at"] == "2026-11-04T00:00:00.000000Z"
    assert compute_source_capture_observation_hash(obs) == compute_source_capture_observation_hash(example())


@pytest.mark.parametrize(
    "change",
    [
        {"capture_origin": "REPLAY"},
        {"page_count": True},
        {"record_count": 0},
        {"full_scan_completed": False},
        {"primary_key_validation_passed": False},
        {"capture_started_at": _NOW.replace(tzinfo=None)},
        {"capture_completed_at": _NOW - timedelta(seconds=1)},
        {"recorded_at": _NOW},
        {"raw_manifest_sha256": "A" * 64},
        {"source_code": " e\u0301 "},
        {"issuer_ref": SourceCaptureIssuerRef("wrong", "1", "b" * 64)},
    ],
)
def test_rejects_incomplete_or_noncanonical_capture(change):
    with pytest.raises(ValueError):
        example(**change)


def test_no_unknown_fields_or_caller_owned_digest():
    with pytest.raises(TypeError):
        example(observation_content_sha256="a" * 64)
    with pytest.raises(TypeError):
        example(raw_response="patient data")
