"""Immutable, trusted issuer source capture observation (synthetic internal contract)."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.databases import Base
from app.core.db.types import UUIDChar


class RagSourceCaptureObservation(Base):
    __tablename__ = "rag_source_capture_observation"
    __table_args__ = (
        UniqueConstraint("ingestion_run_id", name="uq_rag_source_capture_observation_run"),
        ForeignKeyConstraint(
            ["source_snapshot_id", "source_version"],
            ["rag_source_snapshot.id", "rag_source_snapshot.source_version"],
            name="fk_rag_source_capture_snapshot_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint("capture_origin = 'LIVE_PROVIDER'", name="chk_rag_source_capture_origin"),
        CheckConstraint(
            "full_scan_completed AND primary_key_validation_passed", name="chk_rag_source_capture_validated"
        ),
        CheckConstraint("page_count > 0 AND record_count > 0", name="chk_rag_source_capture_positive_counts"),
        CheckConstraint(
            "capture_started_at <= capture_completed_at AND capture_completed_at <= recorded_at",
            name="chk_rag_source_capture_time_order",
        ),
    )
    capture_observation_id: Mapped[UUID] = mapped_column(UUIDChar(), primary_key=True)
    ingestion_run_id: Mapped[UUID] = mapped_column(
        UUIDChar(), ForeignKey("rag_source_ingestion_run.id", ondelete="RESTRICT"), nullable=False
    )
    source_snapshot_id: Mapped[UUID] = mapped_column(UUIDChar(), nullable=False)
    source_version: Mapped[str] = mapped_column(String(200), nullable=False)
    canonical_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    source_code: Mapped[str] = mapped_column(String(100), nullable=False)
    endpoint_code: Mapped[str] = mapped_column(String(100), nullable=False)
    operation_code: Mapped[str] = mapped_column(String(100), nullable=False)
    capture_protocol_version: Mapped[str] = mapped_column(String(50), nullable=False)
    issuer_ref: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    request_profile_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    capture_origin: Mapped[str] = mapped_column(String(20), nullable=False)
    capture_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    capture_completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint_contract_receipt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    full_scan_completed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    primary_key_validation_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    observation_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
