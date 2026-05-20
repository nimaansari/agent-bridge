# -*- coding: utf-8 -*-
"""Reconcile agent current task fields.

Revision ID: 020
Revises: 019
Create Date: 2026-05-20
"""

from alembic import op
import sqlalchemy as sa


revision = "020"
down_revision = "019"
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
    # Keep this reconcile migration non-destructive. The original 019 migration
    # owns the formal downgrade for fresh databases; live deployments may have
    # an older 019 stamped, so dropping here would be unsafe.
    pass
