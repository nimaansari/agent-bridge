# -*- coding: utf-8 -*-
"""Add user-editable agent display names.

Revision ID: 015
Revises: 014
Create Date: 2026-05-19
"""

from alembic import op
import sqlalchemy as sa


revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not _has_column(inspector, "workspace_members", "display_name"):
        op.add_column("workspace_members", sa.Column("display_name", sa.Text(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "workspace_members", "display_name"):
        op.drop_column("workspace_members", "display_name")
