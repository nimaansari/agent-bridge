# -*- coding: utf-8 -*-
"""Add agent current task fields.

Revision ID: 019
Revises: 018
Create Date: 2026-05-20
"""

from alembic import op
import sqlalchemy as sa


revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def _columns(inspector, table_name: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = _columns(inspector, "workspace_members")
    if "current_task" not in cols:
        op.add_column("workspace_members", sa.Column("current_task", sa.Text(), nullable=True))
    if "task_status" not in cols:
        op.add_column("workspace_members", sa.Column("task_status", sa.Text(), nullable=False, server_default=sa.text("'idle'")))
    if "task_updated_at" not in cols:
        op.add_column("workspace_members", sa.Column("task_updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = _columns(inspector, "workspace_members")
    if "task_updated_at" in cols:
        op.drop_column("workspace_members", "task_updated_at")
    if "task_status" in cols:
        op.drop_column("workspace_members", "task_status")
    if "current_task" in cols:
        op.drop_column("workspace_members", "current_task")
