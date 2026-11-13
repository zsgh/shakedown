"""finding list indexes

Revision ID: a1c3e5f7b9d2
Revises: f5b8d0e2a3c4
Create Date: 2026-11-04
"""
from __future__ import annotations

from alembic import op

revision = "a1c3e5f7b9d2"
down_revision = "f5b8d0e2a3c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_findings_project_fix_created",
        "findings",
        ["project_id", "fix_status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_findings_project_created",
        "findings",
        ["project_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_findings_project_created", table_name="findings")
    op.drop_index("ix_findings_project_fix_created", table_name="findings")
