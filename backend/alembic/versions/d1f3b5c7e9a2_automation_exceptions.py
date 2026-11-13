"""automation exceptions

Revision ID: d1f3b5c7e9a2
Revises: c9e1a3b5d7f2
Create Date: 2026-11-10 00:10:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d1f3b5c7e9a2"
down_revision = "c9e1a3b5d7f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "automation_exceptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("target_type", sa.String(length=24), nullable=False, server_default=""),
        sa.Column("target_id", sa.Integer(), nullable=True),
        sa.Column("severity", sa.String(length=16), nullable=False, server_default="medium"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("owner", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_automation_exceptions_project_id", "automation_exceptions", ["project_id"])
    op.create_index("ix_automation_exceptions_kind", "automation_exceptions", ["kind"])
    op.create_index("ix_automation_exceptions_status", "automation_exceptions", ["status"])
    op.create_index("ix_automation_exceptions_target_id", "automation_exceptions", ["target_id"])
    op.create_index(
        "ix_automation_exceptions_project_status",
        "automation_exceptions",
        ["project_id", "status", "severity"],
    )
    op.create_index(
        "ix_automation_exceptions_target",
        "automation_exceptions",
        ["target_type", "target_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_automation_exceptions_target", table_name="automation_exceptions")
    op.drop_index("ix_automation_exceptions_project_status", table_name="automation_exceptions")
    op.drop_index("ix_automation_exceptions_target_id", table_name="automation_exceptions")
    op.drop_index("ix_automation_exceptions_status", table_name="automation_exceptions")
    op.drop_index("ix_automation_exceptions_kind", table_name="automation_exceptions")
    op.drop_index("ix_automation_exceptions_project_id", table_name="automation_exceptions")
    op.drop_table("automation_exceptions")
