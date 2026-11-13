"""test data assets + read-only DSN + test files

Revision ID: c7d9e1f3a5b7
Revises: c2e4f6a8b0d1
Create Date: 2026-11-13 16:30:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c7d9e1f3a5b7"
down_revision = "c2e4f6a8b0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("target_read_dsn_encrypted", sa.Text(), nullable=True),
    )

    op.create_table(
        "test_datasets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("kind", sa.String(length=24), nullable=False, server_default="generated"),
        sa.Column(
            "schema_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("seed", sa.Integer(), nullable=False, server_default="42"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="active"),
        sa.Column(
            "meta_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "name", name="uq_test_datasets_project_name"),
    )
    op.create_index("ix_test_datasets_project_id", "test_datasets", ["project_id"], unique=False)
    op.create_index("ix_test_datasets_status", "test_datasets", ["status"], unique=False)
    op.create_index("ix_test_datasets_project_status", "test_datasets", ["project_id", "status"], unique=False)

    op.create_table(
        "test_data_rows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["dataset_id"], ["test_datasets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dataset_id", "row_index", name="uq_test_data_rows_dataset_index"),
    )
    op.create_index("ix_test_data_rows_project_id", "test_data_rows", ["project_id"], unique=False)
    op.create_index("ix_test_data_rows_dataset_id", "test_data_rows", ["dataset_id"], unique=False)
    op.create_index(
        "ix_test_data_rows_dataset_index", "test_data_rows", ["dataset_id", "row_index"], unique=False
    )

    op.create_table(
        "case_data_bindings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="single"),
        sa.Column("row_index", sa.Integer(), nullable=True),
        sa.Column(
            "overrides",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["test_cases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dataset_id"], ["test_datasets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id", "dataset_id", name="uq_case_data_bindings_case_dataset"),
    )
    op.create_index("ix_case_data_bindings_project_id", "case_data_bindings", ["project_id"], unique=False)
    op.create_index("ix_case_data_bindings_case_id", "case_data_bindings", ["case_id"], unique=False)
    op.create_index("ix_case_data_bindings_dataset_id", "case_data_bindings", ["dataset_id"], unique=False)

    op.create_table(
        "test_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("filename", sa.String(length=300), nullable=False),
        sa.Column("content_type", sa.String(length=200), nullable=False, server_default="application/octet-stream"),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("storage_path", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "meta_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "name", name="uq_test_files_project_name"),
    )
    op.create_index("ix_test_files_project_id", "test_files", ["project_id"], unique=False)


def downgrade() -> None:
    op.drop_table("test_files")
    op.drop_table("case_data_bindings")
    op.drop_table("test_data_rows")
    op.drop_table("test_datasets")
    op.drop_column("projects", "target_read_dsn_encrypted")
