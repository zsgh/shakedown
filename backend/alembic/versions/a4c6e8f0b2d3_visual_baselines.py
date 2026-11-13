"""visual baselines

Revision ID: a4c6e8f0b2d3
Revises: f3b5d7e9a1c2
Create Date: 2026-11-10 00:40:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a4c6e8f0b2d3"
down_revision = "f3b5d7e9a1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "visual_baselines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("test_cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False, server_default="default"),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("screenshot_path", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("project_id", "case_id", "name", name="uq_visual_baseline_case_name"),
    )
    op.create_index("ix_visual_baselines_project_id", "visual_baselines", ["project_id"])
    op.create_index("ix_visual_baselines_case_id", "visual_baselines", ["case_id"])


def downgrade() -> None:
    op.drop_index("ix_visual_baselines_case_id", table_name="visual_baselines")
    op.drop_index("ix_visual_baselines_project_id", table_name="visual_baselines")
    op.drop_table("visual_baselines")
