"""Read the pinned Sync Guide authority under the caller's finalization transaction."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.async_jobs import AiJob, AiJobStatus, AiJobType
from app.models.guides import Guide
from app.models.prescriptions import Prescription, PrescriptionVersionMedication
from app.models.rag_candidate import MedicationIdentification, MedicationIdentificationStatus
from app.models.rag_runtime import RagRuntimeEnvironment, RagRuntimeEnvironmentStatus
from app.models.users import User
from app.repositories.profile_ownership import owned_by_self
from app.repositories.rag_runtime_repository import RagRuntimeRepository
from app.services.guide_runtime_request import (
    GuideRuntimeRequestCarrier,
    load_verified_guide_runtime_request_carrier,
)
from app.services.guide_sync_runtime_lifecycle import GuideSyncRuntimePreparationError

_UNAVAILABLE = "Sync Guide runtime ownership or identity is unavailable"


@dataclass(frozen=True, slots=True)
class GuideSyncRuntimeFinalizationState:
    guide: Guide
    job: AiJob
    is_current: bool


class GuideSyncRuntimeFinalizer:
    """Lock owned rows in global order and assess the exact committed request.

    The caller owns the transaction and the STALE/PASS publication decision. This
    checker acquires User SHARE, Prescription, AI Job, Guide, then Environment
    row locks; it
    does not commit, mutate status, or reselect a runtime authority.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_and_check(
        self,
        *,
        user_id: UUID,
        guide_id: UUID,
        request: GuideRuntimeRequestCarrier,
    ) -> GuideSyncRuntimeFinalizationState:
        if request.guide_id != guide_id:
            raise GuideSyncRuntimePreparationError(_UNAVAILABLE)

        # The request session is reused after intake commit with
        # expire_on_commit=False. Clear A's identity map before any C readback.
        self._session.expire_all()

        # Account withdrawal takes User FOR UPDATE before deleting children.
        # Hold a compatible SHARE lock until C commits so ownership cannot
        # disappear between the SELF check and publication.
        user = await self._session.scalar(select(User).where(User.id == user_id).with_for_update(read=True, of=User))
        if user is None:
            raise GuideSyncRuntimePreparationError(_UNAVAILABLE)

        # The Guide join identifies the prescription without locking the domain
        # row before the prescription. SELF ownership is checked in the same SQL.
        prescription = await self._session.scalar(
            select(Prescription)
            .join(Guide, Guide.prescription_id == Prescription.id)
            .where(
                Guide.id == guide_id,
                owned_by_self(Prescription.profile_id, user_id),
                Guide.profile_id == Prescription.profile_id,
            )
            .with_for_update(of=Prescription)
        )
        if prescription is None:
            raise GuideSyncRuntimePreparationError(_UNAVAILABLE)

        job = await self._session.scalar(
            select(AiJob)
            .where(AiJob.id == request.job_id, AiJob.user_id == user_id, AiJob.job_type == AiJobType.GUIDE)
            .with_for_update(of=AiJob)
        )
        if job is None:
            raise GuideSyncRuntimePreparationError(_UNAVAILABLE)

        guide = await self._session.scalar(
            select(Guide)
            .where(
                Guide.id == guide_id,
                Guide.prescription_id == prescription.id,
                Guide.profile_id == prescription.profile_id,
                owned_by_self(Guide.profile_id, user_id),
            )
            .with_for_update(of=Guide)
        )
        if guide is None or guide.ai_job_id != job.id:
            raise GuideSyncRuntimePreparationError(_UNAVAILABLE)

        if (
            job.status != AiJobStatus.PROCESSING
            or job.prescription_version_id != request.prescription_version_id
            or guide.prescription_version_id != request.prescription_version_id
            or prescription.active_version_id != request.prescription_version_id
        ):
            return GuideSyncRuntimeFinalizationState(guide, job, False)

        # Environment is held through the caller's commit. No new authority is
        # selected here; only the frozen ID, revision and pointer are checked.
        environment = await self._session.scalar(
            select(RagRuntimeEnvironment)
            .where(RagRuntimeEnvironment.id == request.runtime_environment_id)
            .with_for_update(of=RagRuntimeEnvironment)
        )
        if environment is None or (
            environment.environment_status,
            environment.environment_revision,
            environment.active_bundle_id,
            environment.active_bundle_manifest_hash,
        ) != (
            RagRuntimeEnvironmentStatus.ACTIVE,
            request.runtime_environment_revision,
            request.runtime_release_bundle_id,
            request.runtime_release_bundle_manifest_hash,
        ):
            return GuideSyncRuntimeFinalizationState(guide, job, False)

        # Exact readback checks persisted execution context (including full
        # Request Guard ref, patient digest and scope hash), canonical bundle,
        # retrieval binding, and pinned member query snapshots against request.
        readback = await load_verified_guide_runtime_request_carrier(self._session, request.job_id)
        if readback != request:
            return GuideSyncRuntimeFinalizationState(guide, job, False)

        manifest = await RagRuntimeRepository(self._session).get_execution_manifest_by_id(
            request.runtime_execution_manifest_id
        )
        if manifest is None or manifest.manifest_hash != request.runtime_execution_manifest_hash:
            return GuideSyncRuntimeFinalizationState(guide, job, False)

        # Same-version membership is compared by immutable IDs. This deliberately
        # avoids latest Identification, medication names, and product catalog.
        rows = await self._session.execute(
            select(PrescriptionVersionMedication.id, MedicationIdentification.id)
            .outerjoin(
                MedicationIdentification,
                and_(
                    MedicationIdentification.prescription_version_medication_id == PrescriptionVersionMedication.id,
                    MedicationIdentification.status == MedicationIdentificationStatus.MATCHED,
                ),
            )
            .where(PrescriptionVersionMedication.prescription_version_id == request.prescription_version_id)
        )
        current = {(identification_id, medication_id) for medication_id, identification_id in rows.all()}
        pinned = {
            (item.medication_identification_id, item.prescription_version_medication_id)
            for item in request.identifications
        }
        return GuideSyncRuntimeFinalizationState(
            guide,
            job,
            bool(current) and None not in {identification_id for identification_id, _ in current} and current == pinned,
        )
