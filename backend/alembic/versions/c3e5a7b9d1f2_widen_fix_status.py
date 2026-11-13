"""widen finding fix status

Revision ID: c3e5a7b9d1f2
Revises: b2d4f6a8c0e1
Create Date: 2026-11-04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "c3e5a7b9d1f2"
down_revision = "b2d4f6a8c0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("findings", "fix_status", type_=sa.String(length=40), existing_nullable=False)
    op.alter_column("finding_fixes", "status", type_=sa.String(length=40), existing_nullable=False)


def downgrade() -> None:
    op.alter_column("findings", "fix_status", type_=sa.String(length=24), existing_nullable=False)
    op.alter_column("finding_fixes", "status", type_=sa.String(length=24), existing_nullable=False)
