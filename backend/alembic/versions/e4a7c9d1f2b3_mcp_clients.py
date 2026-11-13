"""mcp clients

Revision ID: e4a7c9d1f2b3
Revises: c8f4d2a6b1e9
Create Date: 2026-11-04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e4a7c9d1f2b3"
down_revision = "c8f4d2a6b1e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_clients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=24), nullable=False),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mcp_clients_token_hash", "mcp_clients", ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_mcp_clients_token_hash", table_name="mcp_clients")
    op.drop_table("mcp_clients")
