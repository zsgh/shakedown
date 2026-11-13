"""finding fix lifecycle

Revision ID: f5b8d0e2a3c4
Revises: e4a7c9d1f2b3
Create Date: 2026-11-04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f5b8d0e2a3c4"
down_revision = "e4a7c9d1f2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "findings",
        sa.Column("fix_status", sa.String(length=24), server_default="open", nullable=False),
    )
    op.add_column("findings", sa.Column("fix_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "findings",
        sa.Column("fix_claim", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.create_index("ix_findings_fix_status", "findings", ["fix_status"], unique=False)

    op.create_table(
        "finding_fixes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("finding_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column("pr_url", sa.Text(), nullable=False),
        sa.Column("files", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("test_command", sa.Text(), nullable=False),
        sa.Column("test_result", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["finding_id"], ["findings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_finding_fixes_project_id", "finding_fixes", ["project_id"], unique=False)
    op.create_index("ix_finding_fixes_finding_id", "finding_fixes", ["finding_id"], unique=False)
    op.create_index("ix_finding_fixes_status", "finding_fixes", ["status"], unique=False)
    op.create_index("ix_finding_fixes_finding_created", "finding_fixes", ["finding_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_finding_fixes_finding_created", table_name="finding_fixes")
    op.drop_index("ix_finding_fixes_status", table_name="finding_fixes")
    op.drop_index("ix_finding_fixes_finding_id", table_name="finding_fixes")
    op.drop_index("ix_finding_fixes_project_id", table_name="finding_fixes")
    op.drop_table("finding_fixes")
    op.drop_index("ix_findings_fix_status", table_name="findings")
    op.drop_column("findings", "fix_claim")
    op.drop_column("findings", "fix_verified_at")
    op.drop_column("findings", "fix_status")
