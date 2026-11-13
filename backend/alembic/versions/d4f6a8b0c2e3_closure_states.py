"""closure states: finding disposition + case gap state

Revision ID: d4f6a8b0c2e3
Revises: c3e5a7b9d1f2
Create Date: 2026-11-05 20:30:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d4f6a8b0c2e3"
down_revision = "c3e5a7b9d1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "findings",
        sa.Column(
            "resolution",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "findings",
        sa.Column("assignee", sa.String(length=120), nullable=False, server_default=""),
    )
    op.create_table(
        "case_gap_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("owner", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["test_cases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id", name="uq_case_gap_states_case"),
    )
    op.create_index("ix_case_gap_states_project_id", "case_gap_states", ["project_id"], unique=False)
    op.create_index("ix_case_gap_states_status", "case_gap_states", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_case_gap_states_status", table_name="case_gap_states")
    op.drop_index("ix_case_gap_states_project_id", table_name="case_gap_states")
    op.drop_table("case_gap_states")
    op.drop_column("findings", "assignee")
    op.drop_column("findings", "resolution")
