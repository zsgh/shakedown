"""performance runs

Revision ID: f3b5d7e9a1c2
Revises: e2a4c6d8f0b1
Create Date: 2026-11-10 00:30:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f3b5d7e9a1c2"
down_revision = "e2a4c6d8f0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "performance_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False, server_default="GET"),
        sa.Column("path", sa.Text(), nullable=False, server_default=""),
        sa.Column("concurrency", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("iterations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("success_rate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("requests_per_second", sa.Float(), nullable=False, server_default="0"),
        sa.Column("p50_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("p95_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("p99_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("slo_passed", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "slo",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_performance_runs_project_id", "performance_runs", ["project_id"])
    op.create_index("ix_performance_runs_path", "performance_runs", ["path"])


def downgrade() -> None:
    op.drop_index("ix_performance_runs_path", table_name="performance_runs")
    op.drop_index("ix_performance_runs_project_id", table_name="performance_runs")
    op.drop_table("performance_runs")
