"""Synthetic internal Candidate audit and trusted source capture observations.

Revision ID: 925a1b2c3d4e
Revises: 924a1b2c3d4e

Schema availability does not authorize actual Source use or public activation.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "925a1b2c3d4e"
down_revision = "924a1b2c3d4e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Additive tables only; historical rows are never backfilled or rehashed.
    op.create_table(
        "rag_source_capture_observation",
        sa.Column("capture_observation_id", sa.CHAR(length=36), nullable=False),
        sa.Column("ingestion_run_id", sa.CHAR(length=36), nullable=False),
        sa.Column("source_snapshot_id", sa.CHAR(length=36), nullable=False),
        sa.Column("source_version", sa.String(length=200), nullable=False),
        sa.Column("canonical_checksum", sa.String(length=64), nullable=False),
        sa.Column("source_code", sa.String(length=100), nullable=False),
        sa.Column("endpoint_code", sa.String(length=100), nullable=False),
        sa.Column("operation_code", sa.String(length=100), nullable=False),
        sa.Column("capture_protocol_version", sa.String(length=50), nullable=False),
        sa.Column("issuer_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("request_profile_sha256", sa.String(length=64), nullable=False),
        sa.Column("capture_origin", sa.String(length=20), nullable=False),
        sa.Column("capture_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capture_completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("endpoint_contract_receipt_sha256", sa.String(length=64), nullable=False),
        sa.Column("full_scan_completed", sa.Boolean(), nullable=False),
        sa.Column("primary_key_validation_passed", sa.Boolean(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("observation_content_sha256", sa.String(length=64), nullable=False),
        sa.CheckConstraint("capture_origin = 'LIVE_PROVIDER'", name="chk_rag_source_capture_origin"),
        sa.CheckConstraint(
            "capture_started_at <= capture_completed_at AND capture_completed_at <= recorded_at",
            name="chk_rag_source_capture_time_order",
        ),
        sa.CheckConstraint(
            "full_scan_completed AND primary_key_validation_passed", name="chk_rag_source_capture_validated"
        ),
        sa.CheckConstraint("page_count > 0 AND record_count > 0", name="chk_rag_source_capture_positive_counts"),
        sa.ForeignKeyConstraint(["ingestion_run_id"], ["rag_source_ingestion_run.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_snapshot_id", "source_version"],
            ["rag_source_snapshot.id", "rag_source_snapshot.source_version"],
            name="fk_rag_source_capture_snapshot_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("capture_observation_id"),
        sa.UniqueConstraint("ingestion_run_id", name="uq_rag_source_capture_observation_run"),
    )
    op.create_table(
        "rag_candidate_request_guard_audit",
        sa.Column("request_guard_decision_id", sa.CHAR(length=36), nullable=False),
        sa.Column("artifact_code", sa.String(length=100), nullable=False),
        sa.Column("artifact_version", sa.String(length=50), nullable=False),
        sa.Column("artifact_content_sha256", sa.String(length=64), nullable=False),
        sa.Column("proof_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.CHAR(length=36), nullable=False),
        sa.Column("prescription_version_id", sa.CHAR(length=36), nullable=False),
        sa.Column("prescription_version_medication_id", sa.CHAR(length=36), nullable=False),
        sa.Column("request_operation_code", sa.String(length=100), nullable=False),
        sa.Column("search_id", sa.CHAR(length=36), nullable=True),
        sa.Column("candidate_search_result_id", sa.CHAR(length=36), nullable=True),
        sa.Column("runtime_environment_id", sa.CHAR(length=36), nullable=False),
        sa.Column("environment_revision", sa.Integer(), nullable=False),
        sa.Column("governance_revision_ref", sa.String(length=255), nullable=False),
        sa.Column("safety_epoch", sa.Integer(), nullable=False),
        sa.Column("candidate_index_version_id", sa.CHAR(length=36), nullable=False),
        sa.Column("candidate_index_artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("query_digest", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=10), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("projection", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("proof_manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(outcome = 'PASS' AND search_id IS NOT NULL AND valid_until IS NOT NULL) OR outcome = 'FAIL'",
            name="chk_rag_candidate_guard_audit_pass",
        ),
        sa.CheckConstraint(
            "(request_operation_code = 'medication-candidate.search.create' AND candidate_search_result_id IS NULL) OR (request_operation_code IN ('medication-candidate.confirm','medication-candidate.reject') AND search_id IS NOT NULL AND candidate_search_result_id IS NOT NULL)",
            name="chk_rag_candidate_guard_audit_operation_refs",
        ),
        sa.CheckConstraint(
            "artifact_code = 'candidate_request_guard_audit'", name="chk_rag_candidate_guard_audit_code"
        ),
        sa.CheckConstraint("artifact_content_sha256 ~ '^[0-9a-f]{64}$'", name="chk_rag_candidate_guard_audit_hash"),
        sa.CheckConstraint("artifact_version = '1.0'", name="chk_rag_candidate_guard_audit_version"),
        sa.CheckConstraint(
            "candidate_index_artifact_sha256 ~ '^[0-9a-f]{64}$'", name="chk_rag_candidate_guard_audit_index_hash"
        ),
        sa.CheckConstraint("outcome IN ('PASS','FAIL')", name="chk_rag_candidate_guard_audit_outcome"),
        sa.CheckConstraint("proof_manifest_sha256 ~ '^[0-9a-f]{64}$'", name="chk_rag_candidate_guard_audit_proof_hash"),
        sa.CheckConstraint("query_digest ~ '^[0-9a-f]{64}$'", name="chk_rag_candidate_guard_audit_query_hash"),
        sa.CheckConstraint(
            "environment_revision >= 1 AND safety_epoch >= 1", name="chk_rag_candidate_guard_audit_revisions"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_index_version_id"], ["rag_candidate_index_version.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_search_result_id"], ["medication_candidate_search_result.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["prescription_version_id"], ["prescription_version.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["prescription_version_medication_id", "prescription_version_id"],
            ["prescription_version_medication.id", "prescription_version_medication.prescription_version_id"],
            name="fk_rag_candidate_guard_audit_medication_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["request_guard_decision_id"],
            ["rag_request_guard_runtime_binding.request_guard_decision_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["runtime_environment_id"], ["rag_runtime_environment.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["search_id"], ["medication_candidate_search.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("request_guard_decision_id"),
    )
    op.create_index(
        "idx_rag_candidate_guard_audit_user", "rag_candidate_request_guard_audit", ["user_id"], unique=False
    )


def downgrade() -> None:
    connection = op.get_bind()
    tables = ("rag_candidate_request_guard_audit", "rag_source_capture_observation")
    # Acquire all locks before checking either table to avoid a concurrent insert.
    for table in tables:
        connection.execute(sa.text(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE"))
    for table in tables:
        if connection.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")):
            raise RuntimeError("downgrade would lose immutable Candidate audit or source capture observations")
    op.drop_index("idx_rag_candidate_guard_audit_user", table_name=tables[0])
    for table in tables:
        op.drop_table(table)
