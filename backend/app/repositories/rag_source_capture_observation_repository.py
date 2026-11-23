"""Synthetic capture store. The caller owns the transaction and trusted issuer boundary."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rag_source import (
    RagSource,
    RagSourceEndpoint,
    RagSourceIngestionArtifact,
    RagSourceIngestionRun,
    RagSourceOperation,
    RagSourceSnapshot,
)
from app.models.rag_source_capture_observation import RagSourceCaptureObservation as Row
from rag_runtime.source_capture_observation import (
    SourceCaptureIssuerRef,
    SourceCaptureObservation,
    compute_source_capture_observation_hash,
    source_capture_projection,
)


class SourceCaptureConflictError(ValueError):
    """Observation conflicts with persisted Source facts or an existing observation."""


class SourceCaptureIsolationError(ValueError):
    """Capture comparison requires one consistent committed metadata snapshot."""


def raw_manifest_hash(artifacts: list[RagSourceIngestionArtifact]) -> str:
    """Mirror the writer's raw manifest recipe on persisted artifact metadata."""
    if not artifacts or len({a.artifact_key for a in artifacts}) != len(artifacts):
        raise SourceCaptureConflictError("missing or duplicate raw artifact")
    manifest = [
        [a.artifact_key, a.raw_checksum, a.byte_size, a.content_type]
        for a in sorted(artifacts, key=lambda item: item.artifact_key.encode("utf-16-be"))
    ]
    encoded = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RagSourceCaptureObservationRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _verify_source(self, obs: SourceCaptureObservation) -> None:
        result = await self.session.execute(
            select(RagSourceIngestionRun, RagSourceSnapshot, RagSourceOperation, RagSourceEndpoint, RagSource)
            .join(RagSourceSnapshot, RagSourceSnapshot.id == RagSourceIngestionRun.snapshot_id)
            .join(RagSourceOperation, RagSourceOperation.id == RagSourceIngestionRun.operation_id)
            .join(RagSourceEndpoint, RagSourceEndpoint.id == RagSourceOperation.endpoint_id)
            .join(RagSource, RagSource.id == RagSourceEndpoint.source_id)
            .where(RagSourceIngestionRun.id == obs.ingestion_run_id)
        )
        row = result.one_or_none()
        if row is None:
            raise SourceCaptureConflictError("run and snapshot binding unavailable")
        run, snapshot, operation, endpoint, source = row
        if (
            run.run_status not in ("SUCCEEDED", "NO_CHANGE")
            or run.finished_at is None
            or run.failure_code is not None
            or run.validation_reason_code is not None
            or run.started_at != obs.capture_started_at
            or run.finished_at != obs.capture_completed_at
            or snapshot.id != obs.source_snapshot_id
            or snapshot.operation_id != run.operation_id
            or snapshot.source_version != obs.source_version
            or snapshot.canonical_checksum != obs.canonical_checksum
            or source.source_code != obs.source_code
            or endpoint.endpoint_code != obs.endpoint_code
            or operation.operation_code != obs.operation_code
            or snapshot.endpoint_receipt_hash != obs.endpoint_contract_receipt_sha256
            or snapshot.record_count != obs.record_count
        ):
            raise SourceCaptureConflictError("run/operation/snapshot facts mismatch")
        artifacts = list(
            (
                await self.session.scalars(
                    select(RagSourceIngestionArtifact).where(
                        RagSourceIngestionArtifact.ingestion_run_id == obs.ingestion_run_id
                    )
                )
            ).all()
        )
        if any(a.artifact_kind != "RAW_RESPONSE" for a in artifacts):
            raise SourceCaptureConflictError("non-raw artifact in capture run")
        pages = {a.page_number for a in artifacts}
        if (
            len(artifacts) != obs.page_count
            or pages != set(range(1, obs.page_count + 1))
            or raw_manifest_hash(artifacts) != obs.raw_manifest_sha256
        ):
            raise SourceCaptureConflictError("run raw artifact manifest mismatch")
        # NO_CHANGE has its own raw artifacts; the original snapshot hash is irrelevant.
        if run.run_status == "SUCCEEDED" and snapshot.raw_manifest_checksum != obs.raw_manifest_sha256:
            raise SourceCaptureConflictError("snapshot raw manifest mismatch")

    async def record(self, observation: SourceCaptureObservation) -> str:
        projection = source_capture_projection(observation)
        digest = compute_source_capture_observation_hash(observation)
        isolation = await self.session.scalar(text("SELECT current_setting('transaction_isolation')"))
        if isolation not in ("repeatable read", "serializable"):
            raise SourceCaptureIsolationError("capture record requires REPEATABLE READ or SERIALIZABLE")
        await self._verify_source(observation)
        fields = {key: getattr(observation, key) for key in projection if key != "projection_version"}
        fields["issuer_ref"] = projection["issuer_ref"]
        fields["observation_content_sha256"] = digest
        await self.session.execute(insert(Row).values(**fields).on_conflict_do_nothing())
        stored = await self.get_exact(observation.capture_observation_id, digest)
        if stored != observation:
            raise SourceCaptureConflictError("observation identity or semantic conflict")
        return digest

    async def get_exact(self, capture_observation_id: UUID, expected_sha256: str) -> SourceCaptureObservation | None:
        if type(capture_observation_id) is not UUID or type(expected_sha256) is not str or len(expected_sha256) != 64:
            return None
        row = await self.session.get(Row, capture_observation_id)
        if row is None:
            return None
        try:
            obs = SourceCaptureObservation(
                capture_observation_id=row.capture_observation_id,
                ingestion_run_id=row.ingestion_run_id,
                source_snapshot_id=row.source_snapshot_id,
                source_version=row.source_version,
                canonical_checksum=row.canonical_checksum,
                source_code=row.source_code,
                endpoint_code=row.endpoint_code,
                operation_code=row.operation_code,
                capture_protocol_version=row.capture_protocol_version,
                issuer_ref=SourceCaptureIssuerRef(**row.issuer_ref),
                request_profile_sha256=row.request_profile_sha256,
                capture_origin=row.capture_origin,
                capture_started_at=row.capture_started_at,
                capture_completed_at=row.capture_completed_at,
                recorded_at=row.recorded_at,
                raw_manifest_sha256=row.raw_manifest_sha256,
                endpoint_contract_receipt_sha256=row.endpoint_contract_receipt_sha256,
                full_scan_completed=row.full_scan_completed,
                primary_key_validation_passed=row.primary_key_validation_passed,
                page_count=row.page_count,
                record_count=row.record_count,
            )
        except (TypeError, ValueError, AttributeError):
            return None
        digest = compute_source_capture_observation_hash(obs)
        return obs if digest == expected_sha256 == row.observation_content_sha256 else None
