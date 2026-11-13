"""finding triage fields

Revision ID: 9f2c7a1d4b6e
Revises: e2bf709e95de
Create Date: 2026-11-02 13:10:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
import sqlalchemy.dialects.postgresql as postgresql


revision = "9f2c7a1d4b6e"
down_revision = "e2bf709e95de"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("findings", sa.Column("cluster_key", sa.String(length=255), nullable=True))
    op.add_column(
        "findings",
        sa.Column(
            "triage_status",
            sa.String(length=32),
            nullable=False,
            server_default="untriaged",
        ),
    )
    op.add_column(
        "findings",
        sa.Column("triage", postgresql.JSONB(astext_type=sa.Text()), nullable=True, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column("findings", sa.Column("duplicate_of", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_findings_cluster_key"), "findings", ["cluster_key"], unique=False)
    op.create_index(op.f("ix_findings_triage_status"), "findings", ["triage_status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_findings_triage_status"), table_name="findings")
    op.drop_index(op.f("ix_findings_cluster_key"), table_name="findings")
    op.drop_column("findings", "duplicate_of")
    op.drop_column("findings", "triage")
    op.drop_column("findings", "triage_status")
    op.drop_column("findings", "cluster_key")
