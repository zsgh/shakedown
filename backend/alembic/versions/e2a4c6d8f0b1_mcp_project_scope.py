"""mcp client project scope

Revision ID: e2a4c6d8f0b1
Revises: d1f3b5c7e9a2
Create Date: 2026-11-10 00:20:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e2a4c6d8f0b1"
down_revision = "d1f3b5c7e9a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mcp_clients",
        sa.Column(
            "project_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("mcp_clients", "project_ids")
