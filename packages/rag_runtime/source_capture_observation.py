"""Pure, synthetic-only source capture observation projection.

A valid row is evidence of a trusted issuer's assertion. This module cannot prove that
an HTTP request occurred; no application issuer is wired by this module.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_LIMITS = {
    "source_version": 200,
    "source_code": 100,
    "endpoint_code": 100,
    "operation_code": 100,
    "capture_protocol_version": 50,
}
PROJECTION_VERSION = "source-live-capture-observation-v1"


@dataclass(frozen=True, slots=True)
class SourceCaptureIssuerRef:
    artifact_code: str
    version: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class SourceCaptureObservation:
    capture_observation_id: UUID
    ingestion_run_id: UUID
    source_snapshot_id: UUID
    source_version: str
    canonical_checksum: str
    source_code: str
    endpoint_code: str
    operation_code: str
    capture_protocol_version: str
    issuer_ref: SourceCaptureIssuerRef
    request_profile_sha256: str
    capture_origin: str
    capture_started_at: datetime
    capture_completed_at: datetime
    recorded_at: datetime
    raw_manifest_sha256: str
    endpoint_contract_receipt_sha256: str
    full_scan_completed: bool
    primary_key_validation_passed: bool
    page_count: int
    record_count: int

    def __post_init__(self) -> None:
        self._validate_identity()
        self._validate_capture()

    def _validate_identity(self) -> None:
        for name in ("capture_observation_id", "ingestion_run_id", "source_snapshot_id"):
            if type(getattr(self, name)) is not UUID:
                raise ValueError(f"{name} must be UUID")
        for name, maximum in _LIMITS.items():
            _text(getattr(self, name), name, maximum)
        for name in (
            "canonical_checksum",
            "request_profile_sha256",
            "raw_manifest_sha256",
            "endpoint_contract_receipt_sha256",
        ):
            _sha(getattr(self, name), name)
        ref = self.issuer_ref
        if type(ref) is not SourceCaptureIssuerRef or ref.artifact_code != "source_live_capture_issuer":
            raise ValueError("invalid issuer_ref")
        _text(ref.artifact_code, "issuer_ref.artifact_code", 100)
        _text(ref.version, "issuer_ref.version", 50)
        _sha(ref.content_sha256, "issuer_ref.content_sha256")
        if self.capture_origin != "LIVE_PROVIDER":
            raise ValueError("capture_origin must be LIVE_PROVIDER")

    def _validate_capture(self) -> None:
        for name in ("capture_started_at", "capture_completed_at", "recorded_at"):
            _utc(getattr(self, name), name)
        if not self.capture_started_at <= self.capture_completed_at <= self.recorded_at:
            raise ValueError("capture timestamps out of order")
        if self.full_scan_completed is not True or self.primary_key_validation_passed is not True:
            raise ValueError("capture validation incomplete")
        for name in ("page_count", "record_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be positive integer")


def _text(value: object, name: str, maximum: int) -> None:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > maximum
        or not unicodedata.is_normalized("NFC", value)
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        raise ValueError(f"{name} is not canonical text")


def _sha(value: object, name: str) -> None:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise ValueError(f"{name} must be lowercase SHA-256")


def _utc(value: object, name: str) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be aware datetime")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def source_capture_projection(obs: SourceCaptureObservation) -> dict[str, object]:
    if type(obs) is not SourceCaptureObservation:
        raise ValueError("observation must be SourceCaptureObservation")
    return {
        "projection_version": PROJECTION_VERSION,
        "capture_observation_id": str(obs.capture_observation_id),
        "ingestion_run_id": str(obs.ingestion_run_id),
        "source_snapshot_id": str(obs.source_snapshot_id),
        "source_version": obs.source_version,
        "canonical_checksum": obs.canonical_checksum,
        "source_code": obs.source_code,
        "endpoint_code": obs.endpoint_code,
        "operation_code": obs.operation_code,
        "capture_protocol_version": obs.capture_protocol_version,
        "issuer_ref": {
            "artifact_code": obs.issuer_ref.artifact_code,
            "version": obs.issuer_ref.version,
            "content_sha256": obs.issuer_ref.content_sha256,
        },
        "request_profile_sha256": obs.request_profile_sha256,
        "capture_origin": obs.capture_origin,
        "capture_started_at": _utc(obs.capture_started_at, "capture_started_at"),
        "capture_completed_at": _utc(obs.capture_completed_at, "capture_completed_at"),
        "recorded_at": _utc(obs.recorded_at, "recorded_at"),
        "raw_manifest_sha256": obs.raw_manifest_sha256,
        "endpoint_contract_receipt_sha256": obs.endpoint_contract_receipt_sha256,
        "full_scan_completed": obs.full_scan_completed,
        "primary_key_validation_passed": obs.primary_key_validation_passed,
        "page_count": obs.page_count,
        "record_count": obs.record_count,
    }


def compute_source_capture_observation_hash(obs: SourceCaptureObservation) -> str:
    encoded = json.dumps(
        source_capture_projection(obs), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
