"""case verification evidence gate

Revision ID: b7c2e9f1a4d8
Revises: a8d3f1c5b7e9
Create Date: 2026-11-06 11:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b7c2e9f1a4d8"
down_revision = "a8d3f1c5b7e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "case_verifications",
        sa.Column("evidence_level", sa.String(length=24), nullable=False, server_default="self_reported"),
    )
    op.add_column(
        "case_verifications",
        sa.Column("commit_sha", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "case_verifications",
        sa.Column(
            "artifact_hashes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "case_verifications",
        sa.Column(
            "env_fingerprint",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "case_verifications",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("finding_fixes", sa.Column("verification_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_finding_fixes_verification_id",
        "finding_fixes",
        "case_verifications",
        ["verification_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_finding_fixes_verification_id", "finding_fixes", type_="foreignkey")
    op.drop_column("finding_fixes", "verification_id")
    op.drop_column("case_verifications", "expires_at")
    op.drop_column("case_verifications", "env_fingerprint")
    op.drop_column("case_verifications", "artifact_hashes")
    op.drop_column("case_verifications", "commit_sha")
    op.drop_column("case_verifications", "evidence_level")
