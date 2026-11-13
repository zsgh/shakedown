"""project members

Revision ID: b5d7f9a1c3e4
Revises: a4c6e8f0b2d3
Create Date: 2026-11-10 00:50:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "b5d7f9a1c3e4"
down_revision = "a4c6e8f0b2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False, server_default="viewer"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("project_id", "actor", name="uq_project_member_actor"),
    )
    op.create_index("ix_project_members_project_id", "project_members", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_project_members_project_id", table_name="project_members")
    op.drop_table("project_members")
