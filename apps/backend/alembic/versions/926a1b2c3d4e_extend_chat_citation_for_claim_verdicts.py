"""Extend chat_citation for claim-level verification verdicts and source coordinates.

Revision ID: 926a1b2c3d4e
Revises: 925a1b2c3d4e
Create Date: 2026-11-26 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "926a1b2c3d4e"
down_revision: str | Sequence[str] | None = "925a1b2c3d4e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE chat_citation IN ACCESS EXCLUSIVE MODE"))
    existing_count = connection.execute(sa.text("SELECT count(*) FROM chat_citation")).scalar_one()
    if existing_count:
        raise RuntimeError(
            f"Cannot extend chat_citation with NOT NULL claim columns: {existing_count} existing rows found."
        )

    op.alter_column("chat_citation", "knowledge_chunk_id", existing_type=sa.CHAR(length=36), nullable=True)
    op.alter_column("chat_citation", "cited_text", existing_type=sa.String(length=2000), nullable=True)

    op.add_column("chat_citation", sa.Column("claim_key", sa.String(length=64), nullable=False))
    op.add_column("chat_citation", sa.Column("claim_kind", sa.String(length=20), nullable=False))
    op.add_column("chat_citation", sa.Column("verdict", sa.String(length=30), nullable=False))
    op.add_column("chat_citation", sa.Column("source_code", sa.String(length=100), nullable=False))
    op.add_column("chat_citation", sa.Column("source_version", sa.String(length=200), nullable=False))
    op.add_column("chat_citation", sa.Column("locator", sa.String(length=500), nullable=False))
    op.add_column("chat_citation", sa.Column("content_sha256", sa.String(length=64), nullable=True))

    op.create_unique_constraint("uq_chat_citation_claim_key", "chat_citation", ["message_id", "claim_key"])
    op.create_check_constraint(
        "chk_chat_citation_claim_kind",
        "chat_citation",
        "claim_kind IN ('CRITICAL', 'NON_CRITICAL')",
    )
    op.create_check_constraint(
        "chk_chat_citation_verdict",
        "chat_citation",
        "verdict IN ('SUPPORTED', 'NEEDS_VERIFICATION')",
    )
    op.create_check_constraint(
        "chk_chat_citation_fail_closed_verdict",
        "chat_citation",
        "NOT (claim_kind = 'CRITICAL' AND verdict = 'NEEDS_VERIFICATION')",
    )
    op.create_check_constraint(
        "chk_chat_citation_supported_shape",
        "chat_citation",
        "(verdict = 'SUPPORTED' AND knowledge_chunk_id IS NOT NULL AND content_sha256 IS NOT NULL) "
        "OR (verdict = 'NEEDS_VERIFICATION')",
    )
    op.create_check_constraint(
        "chk_chat_citation_content_hash",
        "chat_citation",
        "content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "chk_chat_citation_nonblank_fields",
        "chat_citation",
        "length(trim(claim_key)) > 0 AND length(trim(claim_text)) > 0 "
        "AND length(trim(source_code)) > 0 AND length(trim(source_version)) > 0 "
        "AND length(trim(locator)) > 0",
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE chat_citation IN ACCESS EXCLUSIVE MODE"))
    has_nullable_rows = connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM chat_citation WHERE knowledge_chunk_id IS NULL OR cited_text IS NULL)")
    )
    if has_nullable_rows:
        raise RuntimeError("downgrade cannot restore NOT NULL on chat_citation while null rows exist")

    op.drop_constraint("chk_chat_citation_nonblank_fields", "chat_citation", type_="check")
    op.drop_constraint("chk_chat_citation_content_hash", "chat_citation", type_="check")
    op.drop_constraint("chk_chat_citation_supported_shape", "chat_citation", type_="check")
    op.drop_constraint("chk_chat_citation_fail_closed_verdict", "chat_citation", type_="check")
    op.drop_constraint("chk_chat_citation_verdict", "chat_citation", type_="check")
    op.drop_constraint("chk_chat_citation_claim_kind", "chat_citation", type_="check")
    op.drop_constraint("uq_chat_citation_claim_key", "chat_citation", type_="unique")

    op.drop_column("chat_citation", "content_sha256")
    op.drop_column("chat_citation", "locator")
    op.drop_column("chat_citation", "source_version")
    op.drop_column("chat_citation", "source_code")
    op.drop_column("chat_citation", "verdict")
    op.drop_column("chat_citation", "claim_kind")
    op.drop_column("chat_citation", "claim_key")

    op.alter_column("chat_citation", "cited_text", existing_type=sa.String(length=2000), nullable=False)
    op.alter_column("chat_citation", "knowledge_chunk_id", existing_type=sa.CHAR(length=36), nullable=False)
