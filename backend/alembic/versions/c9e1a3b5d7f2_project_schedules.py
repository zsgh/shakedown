"""project schedules

Revision ID: c9e1a3b5d7f2
Revises: b7c2e9f1a4d8
Create Date: 2026-11-10 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c9e1a3b5d7f2"
down_revision = "b7c2e9f1a4d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_schedules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("interval_seconds", sa.Integer(), nullable=False, server_default="86400"),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_id", sa.Integer(), nullable=True),
        sa.Column("last_status", sa.String(length=24), nullable=False, server_default=""),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "policy",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_project_schedules_project_id", "project_schedules", ["project_id"])
    op.create_index("ix_project_schedules_next_run_at", "project_schedules", ["next_run_at"])
    op.create_index("ix_project_schedules_due", "project_schedules", ["enabled", "next_run_at"])


def downgrade() -> None:
    op.drop_index("ix_project_schedules_due", table_name="project_schedules")
    op.drop_index("ix_project_schedules_next_run_at", table_name="project_schedules")
    op.drop_index("ix_project_schedules_project_id", table_name="project_schedules")
    op.drop_table("project_schedules")
