"""Synthetic internal Candidate request Guard audit extension."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.core.db.databases import Base
from app.core.db.types import UUIDChar


class RagCandidateRequestGuardAudit(Base):
    __tablename__ = "rag_candidate_request_guard_audit"
    __table_args__ = (
        ForeignKeyConstraint(
            ["prescription_version_medication_id", "prescription_version_id"],
            ["prescription_version_medication.id", "prescription_version_medication.prescription_version_id"],
            name="fk_rag_candidate_guard_audit_medication_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint("artifact_code = 'candidate_request_guard_audit'", name="chk_rag_candidate_guard_audit_code"),
        CheckConstraint("artifact_version = '1.0'", name="chk_rag_candidate_guard_audit_version"),
        CheckConstraint("artifact_content_sha256 ~ '^[0-9a-f]{64}$'", name="chk_rag_candidate_guard_audit_hash"),
        CheckConstraint("proof_manifest_sha256 ~ '^[0-9a-f]{64}$'", name="chk_rag_candidate_guard_audit_proof_hash"),
        CheckConstraint(
            "candidate_index_artifact_sha256 ~ '^[0-9a-f]{64}$'", name="chk_rag_candidate_guard_audit_index_hash"
        ),
        CheckConstraint("query_digest ~ '^[0-9a-f]{64}$'", name="chk_rag_candidate_guard_audit_query_hash"),
        CheckConstraint("outcome IN ('PASS','FAIL')", name="chk_rag_candidate_guard_audit_outcome"),
        CheckConstraint(
            "environment_revision >= 1 AND safety_epoch >= 1", name="chk_rag_candidate_guard_audit_revisions"
        ),
        CheckConstraint(
            "(outcome = 'PASS' AND search_id IS NOT NULL AND valid_until IS NOT NULL) OR outcome = 'FAIL'",
            name="chk_rag_candidate_guard_audit_pass",
        ),
        CheckConstraint(
            "(request_operation_code = 'medication-candidate.search.create' AND candidate_search_result_id IS NULL) OR (request_operation_code IN ('medication-candidate.confirm','medication-candidate.reject') AND search_id IS NOT NULL AND candidate_search_result_id IS NOT NULL)",
            name="chk_rag_candidate_guard_audit_operation_refs",
        ),
        Index("idx_rag_candidate_guard_audit_user", "user_id"),
    )

    request_guard_decision_id: Mapped[UUID] = mapped_column(
        UUIDChar(),
        ForeignKey("rag_request_guard_runtime_binding.request_guard_decision_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    artifact_code: Mapped[str] = mapped_column(String(100), nullable=False)
    artifact_version: Mapped[str] = mapped_column(String(50), nullable=False)
    artifact_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    proof_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[UUID] = mapped_column(UUIDChar(), ForeignKey("user.id", ondelete="RESTRICT"), nullable=False)
    prescription_version_id: Mapped[UUID] = mapped_column(
        UUIDChar(), ForeignKey("prescription_version.id", ondelete="RESTRICT"), nullable=False
    )
    prescription_version_medication_id: Mapped[UUID] = mapped_column(UUIDChar(), nullable=False)
    request_operation_code: Mapped[str] = mapped_column(String(100), nullable=False)
    search_id: Mapped[UUID | None] = mapped_column(
        UUIDChar(), ForeignKey("medication_candidate_search.id", ondelete="RESTRICT"), nullable=True
    )
    candidate_search_result_id: Mapped[UUID | None] = mapped_column(
        UUIDChar(), ForeignKey("medication_candidate_search_result.id", ondelete="RESTRICT"), nullable=True
    )
    runtime_environment_id: Mapped[UUID] = mapped_column(
        UUIDChar(), ForeignKey("rag_runtime_environment.id", ondelete="RESTRICT"), nullable=False
    )
    environment_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    governance_revision_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    safety_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_index_version_id: Mapped[UUID] = mapped_column(
        UUIDChar(), ForeignKey("rag_candidate_index_version.id", ondelete="RESTRICT"), nullable=False
    )
    candidate_index_artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    query_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(10), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    projection: Mapped[dict] = mapped_column(JSONB, nullable=False)
    proof_manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
