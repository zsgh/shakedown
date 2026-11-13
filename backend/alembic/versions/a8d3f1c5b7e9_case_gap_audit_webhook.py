"""case gap audit + webhook delivery

Revision ID: a8d3f1c5b7e9
Revises: d4f6a8b0c2e3
Create Date: 2026-11-06 10:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a8d3f1c5b7e9"
down_revision = "d4f6a8b0c2e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "case_gap_audits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("to_status", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("owner", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("actor", sa.String(length=120), nullable=False, server_default="local"),
        sa.Column("source", sa.String(length=24), nullable=False, server_default="manual"),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["test_cases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_case_gap_audits_project_id", "case_gap_audits", ["project_id"], unique=False)
    op.create_index("ix_case_gap_audits_case_id", "case_gap_audits", ["case_id"], unique=False)
    op.create_index(
        "ix_case_gap_audits_project_created", "case_gap_audits", ["project_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_case_gap_audits_case_created", "case_gap_audits", ["case_id", "created_at"], unique=False
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_webhook_deliveries_project_id", "webhook_deliveries", ["project_id"], unique=False)
    op.create_index("ix_webhook_deliveries_event", "webhook_deliveries", ["event"], unique=False)
    op.create_index("ix_webhook_deliveries_status", "webhook_deliveries", ["status"], unique=False)
    op.create_index(
        "ix_webhook_deliveries_project_created", "webhook_deliveries", ["project_id", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_project_created", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_status", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_event", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_project_id", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("ix_case_gap_audits_case_created", table_name="case_gap_audits")
    op.drop_index("ix_case_gap_audits_project_created", table_name="case_gap_audits")
    op.drop_index("ix_case_gap_audits_case_id", table_name="case_gap_audits")
    op.drop_index("ix_case_gap_audits_project_id", table_name="case_gap_audits")
    op.drop_table("case_gap_audits")
