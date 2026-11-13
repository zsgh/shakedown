"""case external verification

Revision ID: b2d4f6a8c0e1
Revises: a1c3e5f7b9d2
Create Date: 2026-11-04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b2d4f6a8c0e1"
down_revision = "a1c3e5f7b9d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "test_cases",
        sa.Column("verification_mode", sa.String(length=16), server_default="api", nullable=False),
    )
    op.add_column(
        "test_cases",
        sa.Column("verification_status", sa.String(length=24), server_default="unverified", nullable=False),
    )
    op.add_column(
        "test_cases",
        sa.Column("verification", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.add_column("test_cases", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("test_cases", sa.Column("verified_by", sa.String(length=120), nullable=True))
    op.create_index("ix_test_cases_verification_mode", "test_cases", ["verification_mode"], unique=False)

    # 根据关联 finding 的代码位置先做一次合理归类。
    op.execute(
        """
        UPDATE test_cases tc
        SET verification_mode = CASE
            WHEN tc.case_type = 'web' THEN 'frontend'
            WHEN f.code_location->>'file_path' LIKE 'tools/%' THEN 'artifact'
            WHEN f.code_location->>'file_path' LIKE 'scripts/%' THEN 'privileged'
            WHEN f.code_location->>'file_path' LIKE 'internal/privileged/%' THEN 'privileged'
            WHEN f.code_location->>'file_path' LIKE 'cmd/%' THEN 'process'
            WHEN f.code_location->>'file_path' LIKE 'internal/server/%'
                 AND (f.title ~* 'Serve|监听|退出|信号|goroutine|errCh|关停|监控|守护')
                THEN 'process'
            ELSE 'api'
        END
        FROM findings f
        WHERE f.case_id = tc.id
        """
    )

    op.create_table(
        "case_verifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("finding_id", sa.Integer(), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("artifacts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["case_id"], ["test_cases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["finding_id"], ["findings.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_case_verifications_project_id", "case_verifications", ["project_id"], unique=False)
    op.create_index("ix_case_verifications_case_id", "case_verifications", ["case_id"], unique=False)
    op.create_index("ix_case_verifications_status", "case_verifications", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_case_verifications_status", table_name="case_verifications")
    op.drop_index("ix_case_verifications_case_id", table_name="case_verifications")
    op.drop_index("ix_case_verifications_project_id", table_name="case_verifications")
    op.drop_table("case_verifications")
    op.drop_index("ix_test_cases_verification_mode", table_name="test_cases")
    op.drop_column("test_cases", "verified_by")
    op.drop_column("test_cases", "verified_at")
    op.drop_column("test_cases", "verification")
    op.drop_column("test_cases", "verification_status")
    op.drop_column("test_cases", "verification_mode")
