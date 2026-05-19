# -*- coding: utf-8 -*-
"""Reconcile PostgreSQL schema with current ORM models.

Revision ID: 014
Revises: 013
Create Date: 2026-05-19
"""

from alembic import op
import sqlalchemy as sa


revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_column(inspector, "channels", "title_manually_set"):
        op.add_column(
            "channels",
            sa.Column("title_manually_set", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        )

    if not _has_column(inspector, "channels", "resume_from"):
        op.add_column("channels", sa.Column("resume_from", sa.Text(), nullable=True))

    if not _has_column(inspector, "workspace_members", "description"):
        op.add_column("workspace_members", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_column(inspector, "workspace_members", "description"):
        op.drop_column("workspace_members", "description")
    if _has_column(inspector, "channels", "resume_from"):
        op.drop_column("channels", "resume_from")
    if _has_column(inspector, "channels", "title_manually_set"):
        op.drop_column("channels", "title_manually_set")
