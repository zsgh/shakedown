"""integration issues for GitHub / Jira

Revision ID: c8f4d2a6b1e9
Revises: 9f2c7a1d4b6e
Create Date: 2026-11-04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c8f4d2a6b1e9"
down_revision = "9f2c7a1d4b6e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integration_issues",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("group_key", sa.String(length=512), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("external_key", sa.String(length=200), nullable=False),
        sa.Column("external_url", sa.Text(), nullable=False),
        sa.Column("finding_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "provider", "group_key", name="uq_integration_issue_group"),
    )
    op.create_index("ix_integration_issues_project_id", "integration_issues", ["project_id"], unique=False)
    op.create_index("ix_integration_issues_provider", "integration_issues", ["provider"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_integration_issues_provider", table_name="integration_issues")
    op.drop_index("ix_integration_issues_project_id", table_name="integration_issues")
    op.drop_table("integration_issues")
