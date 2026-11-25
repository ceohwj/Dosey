"""Bind Candidate Search to an exact Runtime environment revision and Bundle.

Revision ID: 924a1b2c3d4e
Revises: 923a1b2c3d4e
"""

import sqlalchemy as sa
from alembic import op

revision = "924a1b2c3d4e"
down_revision = "923a1b2c3d4e"
branch_labels = None
depends_on = None

TABLE = "medication_candidate_search"


def upgrade() -> None:
    # Historical NULL and Bundle-ID-only rows retain their actual provenance.
    op.add_column(TABLE, sa.Column("runtime_release_bundle_manifest_hash", sa.String(64), nullable=True))
    op.add_column(TABLE, sa.Column("runtime_environment_id", sa.CHAR(36), nullable=True))
    op.add_column(TABLE, sa.Column("runtime_environment_revision", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "chk_medication_candidate_search_runtime_binding_complete",
        TABLE,
        "(runtime_release_bundle_manifest_hash IS NULL AND runtime_environment_id IS NULL "
        "AND runtime_environment_revision IS NULL) OR "
        "(runtime_release_bundle_manifest_hash IS NOT NULL AND runtime_environment_id IS NOT NULL "
        "AND runtime_environment_revision IS NOT NULL AND runtime_release_bundle_id IS NOT NULL "
        "AND candidate_index_version_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "chk_medication_candidate_search_runtime_hash",
        TABLE,
        "runtime_release_bundle_manifest_hash IS NULL OR runtime_release_bundle_manifest_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "chk_medication_candidate_search_runtime_revision",
        TABLE,
        "runtime_environment_revision IS NULL OR runtime_environment_revision >= 1",
    )
    op.create_foreign_key(
        "fk_medication_candidate_search_runtime_bundle_manifest",
        TABLE,
        "rag_runtime_release_bundle",
        ["runtime_release_bundle_id", "runtime_release_bundle_manifest_hash"],
        ["id", "bundle_manifest_hash"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_medication_candidate_search_runtime_environment",
        TABLE,
        "rag_runtime_environment",
        ["runtime_environment_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {TABLE} WHERE runtime_environment_id IS NOT NULL)")):
        raise RuntimeError("downgrade would lose authoritative Candidate Runtime bindings")
    op.drop_constraint("fk_medication_candidate_search_runtime_environment", TABLE, type_="foreignkey")
    op.drop_constraint("fk_medication_candidate_search_runtime_bundle_manifest", TABLE, type_="foreignkey")
    op.drop_constraint("chk_medication_candidate_search_runtime_revision", TABLE, type_="check")
    op.drop_constraint("chk_medication_candidate_search_runtime_hash", TABLE, type_="check")
    op.drop_constraint("chk_medication_candidate_search_runtime_binding_complete", TABLE, type_="check")
    op.drop_column(TABLE, "runtime_environment_revision")
    op.drop_column(TABLE, "runtime_environment_id")
    op.drop_column(TABLE, "runtime_release_bundle_manifest_hash")
