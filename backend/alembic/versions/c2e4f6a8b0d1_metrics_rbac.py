"""RBAC tenant + shared metric counters

Revision ID: c2e4f6a8b0d1
Revises: b5d7f9a1c3e4
Create Date: 2026-11-11 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c2e4f6a8b0d1"
down_revision = "b5d7f9a1c3e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("tenant", sa.String(length=64), nullable=False, server_default="default"),
    )
    op.create_index("ix_projects_tenant", "projects", ["tenant"])
    op.add_column(
        "mcp_clients",
        sa.Column("tenant", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "mcp_clients",
        sa.Column("actor", sa.String(length=120), nullable=False, server_default=""),
    )
    op.create_table(
        "metric_counters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("labels", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("labels_key", sa.String(length=500), nullable=False),
        sa.Column("value", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("name", "labels_key", name="uq_metric_counter_name_labels"),
    )
    op.create_index("ix_metric_counters_name", "metric_counters", ["name"])


def downgrade() -> None:
    op.drop_index("ix_metric_counters_name", table_name="metric_counters")
    op.drop_table("metric_counters")
    op.drop_column("mcp_clients", "actor")
    op.drop_column("mcp_clients", "tenant")
    op.drop_index("ix_projects_tenant", table_name="projects")
    op.drop_column("projects", "tenant")
