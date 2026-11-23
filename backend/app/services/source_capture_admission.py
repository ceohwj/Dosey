"""Internal synthetic capture admission after a complete Source writer commit.

Only a trusted server coordinator may supply the acquisition facts and use the
capture issuer DB role. This value object is consistency data, not a capability
or proof that a provider was contacted. No public request path wires this seam.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.repositories.rag_source_capture_observation_repository import (
    RagSourceCaptureObservationRepository,
    SourceCaptureConflictError,
)
from rag_runtime.source_capture_observation import SourceCaptureIssuerRef, SourceCaptureObservation


@dataclass(frozen=True, slots=True)
class SyntheticPrivateAcquisition:
    """Original server-held facts for one newly persisted synthetic attempt."""

    new_run_id: UUID
    capture_observation_id: UUID
    source_snapshot_id: UUID
    source_code: str
    endpoint_code: str
    operation_code: str
    issuer_ref: SourceCaptureIssuerRef
    request_profile_sha256: str
    capture_started_at: datetime
    capture_completed_at: datetime
    recorded_at: datetime
    origin: str  # SIMULATED_LIVE or RAW_REPLAY; never inferred from a DB row.


class SyntheticSourceCaptureAdmission:
    """Checks a v2 observation before the existing transactional store verifies it."""

    def __init__(self, repository: RagSourceCaptureObservationRepository, *, issuer_ref: SourceCaptureIssuerRef):
        self.repository = repository
        self.issuer_ref = issuer_ref

    async def record(self, observation: SourceCaptureObservation, acquisition: SyntheticPrivateAcquisition) -> str:
        if type(observation) is not SourceCaptureObservation or type(acquisition) is not SyntheticPrivateAcquisition:
            raise SourceCaptureConflictError("capture requires trusted synthetic acquisition facts")
        if (
            acquisition.origin != "SIMULATED_LIVE"
            or not acquisition.source_code.startswith("SYN_")
            or type(acquisition.issuer_ref) is not SourceCaptureIssuerRef
            or acquisition.issuer_ref != self.issuer_ref
            or observation.capture_protocol_version != "v2"
            or observation.issuer_ref != self.issuer_ref
            or observation.capture_origin != "LIVE_PROVIDER"
            or observation.ingestion_run_id != acquisition.new_run_id
            or observation.capture_observation_id != acquisition.capture_observation_id
            or observation.source_snapshot_id != acquisition.source_snapshot_id
            or observation.source_code != acquisition.source_code
            or observation.endpoint_code != acquisition.endpoint_code
            or observation.operation_code != acquisition.operation_code
            or observation.request_profile_sha256 != acquisition.request_profile_sha256
            or observation.capture_started_at != acquisition.capture_started_at
            or observation.capture_completed_at != acquisition.capture_completed_at
            or observation.recorded_at != acquisition.recorded_at
        ):
            raise SourceCaptureConflictError("capture admission facts mismatch")
        return await self.repository.record(observation)
