"""Pure, closed and canonical Candidate request Guard audit projections.

This module validates serialization and internal consistency. The repository checks
persisted authority and the coordinator owns eligibility and expected target sets.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


class CandidateGuardAuditValidationError(ValueError):
    pass


_SHA = re.compile(r"[0-9a-f]{64}\Z")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
_REASONS = frozenset(
    (
        "SOURCE_NOT_ELIGIBLE",
        "APPROVAL_NOT_YET_VALID",
        "APPROVAL_EXPIRED",
        "APPROVAL_REVOKED",
        "FRESHNESS_EXPIRED",
        "SCOPE_DENIED",
        "POLICY_EXPIRED",
        "PRODUCT_NOT_ACTIVE",
    )
)
_OPERATIONS = frozenset(
    ("medication-candidate.search.create", "medication-candidate.confirm", "medication-candidate.reject")
)
_ENVIRONMENTS = frozenset(("LOCAL", "TEST", "CLOSED_DEMO", "PRODUCTION"))


def _closed(value: Any, fields: str, name: str) -> dict:
    expected = set(fields.split())
    if type(value) is not dict or set(value) != expected:
        raise CandidateGuardAuditValidationError(f"{name} must have exact fields")
    return value


def _text(value: Any, name: str, maximum: int) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or value != value.strip()
        or not unicodedata.is_normalized("NFC", value)
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        raise CandidateGuardAuditValidationError(f"{name} must be canonical text")
    return value


def _sha(value: Any, name: str) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise CandidateGuardAuditValidationError(f"{name} must be lowercase SHA-256")
    return value


def _uuid(value: Any, name: str) -> str:
    if type(value) is not str:
        raise CandidateGuardAuditValidationError(f"{name} must be canonical UUID")
    try:
        if str(UUID(value)) != value:
            raise ValueError()
    except ValueError as error:
        raise CandidateGuardAuditValidationError(f"{name} must be canonical UUID") from error
    return value


def _time(value: Any, name: str) -> str:
    if type(value) is not str or _UTC.fullmatch(value) is None:
        raise CandidateGuardAuditValidationError(f"{name} must be UTC microsecond timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as error:
        raise CandidateGuardAuditValidationError(f"{name} invalid timestamp") from error
    return value


def _enum(value: Any, allowed: frozenset[str] | tuple[str, ...], name: str) -> None:
    if type(value) is not str or value not in allowed:
        raise CandidateGuardAuditValidationError(f"{name} invalid enum")


def _reasons(value: Any, outcome: str) -> None:
    if type(value) is not list or any(type(item) is not str or item not in _REASONS for item in value):
        raise CandidateGuardAuditValidationError("invalid reason codes")
    if value != sorted(set(value), key=lambda item: item.encode("utf-8")):
        raise CandidateGuardAuditValidationError("reason codes must be unique and sorted")
    if (outcome == "PASS" and value) or (outcome == "FAIL" and not value):
        raise CandidateGuardAuditValidationError("reason codes disagree with outcome")


def _ref(value: Any, code: str) -> None:
    value = _closed(value, "artifact_code version content_sha256", code)
    if value["artifact_code"] != code:
        raise CandidateGuardAuditValidationError("artifact code mismatch")
    _text(value["version"], "version", 50)
    _sha(value["content_sha256"], "content_sha256")


def _canonical_bytes(value: dict) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    except (TypeError, ValueError, UnicodeError) as error:
        raise CandidateGuardAuditValidationError("non-canonical JSON value") from error


def validate_candidate_guard_proof_manifest(manifest: Any) -> dict:  # noqa: C901
    m = _closed(
        manifest,
        "projection_version runtime_binding_ref scope_manifest_hash resolver_policy_ref retention_policy_ref targets aggregate_outcome",
        "proof",
    )
    if m["projection_version"] != "candidate-request-guard-proof-v1":
        raise CandidateGuardAuditValidationError("unsupported proof version")
    _ref(m["runtime_binding_ref"], "request_guard_runtime_binding")
    _sha(m["scope_manifest_hash"], "scope_manifest_hash")
    _ref(m["resolver_policy_ref"], "candidate_resolver_policy")
    _ref(m["retention_policy_ref"], "candidate_audit_retention_policy")
    _enum(m["aggregate_outcome"], ("PASS", "FAIL"), "aggregate_outcome")
    if type(m["targets"]) is not list or not m["targets"]:
        raise CandidateGuardAuditValidationError("proof targets required")
    keys = []
    for target in m["targets"]:
        t = _closed(
            target,
            "source approval lifecycle freshness scope_policy_ref source_decision_ref member_decisions outcome reason_codes",
            "target",
        )
        s = _closed(
            t["source"],
            "source_code endpoint_code operation_code snapshot_id source_version canonical_sha256 bundle_source_purpose source_use_purpose required selected",
            "source",
        )
        for key in ("source_code", "endpoint_code", "operation_code"):
            _text(s[key], key, 100)
        _uuid(s["snapshot_id"], "snapshot_id")
        _text(s["source_version"], "source_version", 200)
        _sha(s["canonical_sha256"], "canonical_sha256")
        _enum(s["bundle_source_purpose"], ("CATALOG", "CANDIDATE_INDEX_INPUT"), "bundle_source_purpose")
        if (
            s["source_use_purpose"] != "PRODUCT_IDENTIFICATION"
            or type(s["required"]) is not bool
            or type(s["selected"]) is not bool
        ):
            raise CandidateGuardAuditValidationError("invalid source purpose or selection")
        keys.append(
            tuple(
                s[k].encode("utf-8")
                for k in ("source_code", "endpoint_code", "operation_code", "snapshot_id", "source_use_purpose")
            )
        )
        a = _closed(t["approval"], "id version environment purpose valid_from expires_at revoked_at", "approval")
        _uuid(a["id"], "approval id")
        _text(a["version"], "approval version", 80)
        _enum(a["environment"], _ENVIRONMENTS, "approval environment")
        if a["purpose"] != "PRODUCT_IDENTIFICATION":
            raise CandidateGuardAuditValidationError("approval purpose mismatch")
        for key in ("valid_from", "expires_at"):
            _time(a[key], key)
        if a["revoked_at"] is not None:
            _time(a["revoked_at"], "revoked_at")
        lifecycle = _closed(
            t["lifecycle"],
            "source_status endpoint_status endpoint_runtime endpoint_acquisition operation_runtime operation_acquisition snapshot_status verification_seal_id current_selection_sha256 publication_approval_sha256",
            "lifecycle",
        )
        for key, allowed in (
            ("source_status", ("DRAFT", "ACTIVE", "RETIRED", "REVOKED")),
            ("endpoint_status", ("DRAFT", "VERIFIED", "RETIRED", "REVOKED")),
            ("endpoint_runtime", ("DISABLED", "ENABLED")),
            ("operation_runtime", ("DISABLED", "ENABLED")),
            ("endpoint_acquisition", ("PENDING", "APPROVED", "REVOKED")),
            ("operation_acquisition", ("PENDING", "APPROVED", "REVOKED")),
            ("snapshot_status", ("PENDING", "CURRENT", "STALE", "FAILED")),
        ):
            _enum(lifecycle[key], allowed, key)
        _uuid(lifecycle["verification_seal_id"], "verification_seal_id")
        _sha(lifecycle["current_selection_sha256"], "current_selection_sha256")
        if lifecycle["publication_approval_sha256"] is not None:
            _sha(lifecycle["publication_approval_sha256"], "publication_approval_sha256")
        f = _closed(
            t["freshness"],
            "policy_ref capture_observation_id capture_observation_sha256 anchor_kind anchor_at valid_until",
            "freshness",
        )
        _ref(f["policy_ref"], "candidate_source_freshness_policy")
        _uuid(f["capture_observation_id"], "capture_observation_id")
        _sha(f["capture_observation_sha256"], "capture_observation_sha256")
        if f["anchor_kind"] != "LIVE_CAPTURE_COMPLETED_AT":
            raise CandidateGuardAuditValidationError("invalid freshness anchor")
        _time(f["anchor_at"], "anchor_at")
        _time(f["valid_until"], "valid_until")
        _ref(t["scope_policy_ref"], "candidate_source_scope_policy")
        _ref(t["source_decision_ref"], "request_source_decision_authority")
        _enum(t["outcome"], ("PASS", "FAIL"), "target outcome")
        _reasons(t["reason_codes"], t["outcome"])
        if type(t["member_decisions"]) is not list:
            raise CandidateGuardAuditValidationError("member decisions must be list")
        member_keys = []
        for member in t["member_decisions"]:
            d = _closed(
                member,
                "member_kind source_snapshot_member_id content_sha256 member_decision_ref outcome reason_codes",
                "member",
            )
            _enum(d["member_kind"], ("ENDPOINT_OPERATION", "ARTIFACT"), "member_kind")
            _uuid(d["source_snapshot_member_id"], "source_snapshot_member_id")
            _sha(d["content_sha256"], "member content_sha256")
            _ref(d["member_decision_ref"], "request_member_decision_authority")
            _enum(d["outcome"], ("PASS", "FAIL"), "member outcome")
            _reasons(d["reason_codes"], d["outcome"])
            member_keys.append(
                (d["member_kind"].encode(), s["snapshot_id"].encode(), d["source_snapshot_member_id"].encode())
            )
        if member_keys != sorted(set(member_keys)):
            raise CandidateGuardAuditValidationError("member decisions must be unique and sorted")
        if t["outcome"] == "PASS" and any(d["outcome"] != "PASS" for d in t["member_decisions"]):
            raise CandidateGuardAuditValidationError("member outcome disagrees with source")
    if keys != sorted(set(keys)):
        raise CandidateGuardAuditValidationError("targets must be unique and sorted")
    if m["aggregate_outcome"] == "PASS" and any(t["outcome"] != "PASS" for t in m["targets"]):
        raise CandidateGuardAuditValidationError("aggregate outcome mismatch")
    if m["aggregate_outcome"] == "FAIL" and not any(t["outcome"] == "FAIL" for t in m["targets"]):
        raise CandidateGuardAuditValidationError("aggregate outcome mismatch")
    if len(_canonical_bytes(m)) > 65_536:
        raise CandidateGuardAuditValidationError("proof exceeds 65,536 UTF-8 bytes")
    return m


def canonical_candidate_guard_proof_hash(manifest: dict) -> str:
    return hashlib.sha256(_canonical_bytes(validate_candidate_guard_proof_manifest(manifest))).hexdigest()


@dataclass(frozen=True, slots=True)
class CandidateGuardAuditObservation:
    request_guard_decision_id: UUID
    runtime_binding_ref: dict
    user_id: UUID
    prescription_version_id: UUID
    prescription_version_medication_id: UUID
    request_operation_code: str
    search_id: UUID | None
    candidate_search_result_id: UUID | None
    runtime_environment_id: UUID
    environment_revision: int
    governance_revision_ref: str
    safety_epoch: int
    candidate_index_version_id: UUID
    candidate_index_artifact_sha256: str
    query_digest: str
    resolver_policy_sha256: str
    retention_policy_sha256: str
    proof_manifest_sha256: str
    checked_at: datetime
    valid_until: datetime | None
    outcome: str
    reason_codes: tuple[str, ...]


def _dt(value: datetime | None, name: str) -> str | None:
    if value is None:
        return None
    offset = value.utcoffset() if type(value) is datetime and value.tzinfo is not None else None
    if offset is None or offset.total_seconds() != 0:
        raise CandidateGuardAuditValidationError(f"{name} must be aware UTC datetime")
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def candidate_guard_audit_projection(observation: CandidateGuardAuditObservation) -> dict:  # noqa: C901
    if type(observation) is not CandidateGuardAuditObservation:
        raise CandidateGuardAuditValidationError("invalid observation type")
    p: dict[str, Any] = {"projection_version": "candidate-request-guard-audit-v1"}
    for key in (
        "request_guard_decision_id",
        "user_id",
        "prescription_version_id",
        "prescription_version_medication_id",
        "search_id",
        "candidate_search_result_id",
        "runtime_environment_id",
        "candidate_index_version_id",
    ):
        value = getattr(observation, key)
        p[key] = None if value is None else str(value)
        if value is not None and type(value) is not UUID:
            raise CandidateGuardAuditValidationError(f"{key} must be UUID")
    for key in (
        "runtime_binding_ref",
        "request_operation_code",
        "environment_revision",
        "governance_revision_ref",
        "safety_epoch",
        "candidate_index_artifact_sha256",
        "query_digest",
        "resolver_policy_sha256",
        "retention_policy_sha256",
        "proof_manifest_sha256",
        "outcome",
    ):
        p[key] = getattr(observation, key)
    p["checked_at"] = _dt(observation.checked_at, "checked_at")
    p["valid_until"] = _dt(observation.valid_until, "valid_until")
    p["reason_codes"] = list(observation.reason_codes)
    _ref(p["runtime_binding_ref"], "request_guard_runtime_binding")
    _enum(p["request_operation_code"], _OPERATIONS, "request_operation_code")
    for key in ("environment_revision", "safety_epoch"):
        if type(p[key]) is not int or p[key] < 1:
            raise CandidateGuardAuditValidationError(f"{key} must be positive integer")
    _text(p["governance_revision_ref"], "governance_revision_ref", 255)
    for key in (
        "candidate_index_artifact_sha256",
        "query_digest",
        "resolver_policy_sha256",
        "retention_policy_sha256",
        "proof_manifest_sha256",
    ):
        _sha(p[key], key)
    _enum(p["outcome"], ("PASS", "FAIL"), "outcome")
    _reasons(p["reason_codes"], p["outcome"])
    if p["request_operation_code"] == "medication-candidate.search.create":
        if p["candidate_search_result_id"] is not None:
            raise CandidateGuardAuditValidationError("create must not have result")
    elif p["search_id"] is None or p["candidate_search_result_id"] is None:
        raise CandidateGuardAuditValidationError("confirm/reject requires search and result")
    if p["outcome"] == "PASS":
        if p["search_id"] is None or p["valid_until"] is None or p["valid_until"] <= p["checked_at"]:
            raise CandidateGuardAuditValidationError("PASS requires search and future validity")
    return p


def compute_candidate_guard_audit_hash(observation: CandidateGuardAuditObservation) -> str:
    return hashlib.sha256(_canonical_bytes(candidate_guard_audit_projection(observation))).hexdigest()
