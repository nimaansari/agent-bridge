# -*- coding: utf-8 -*-
"""Add durable handoff attempts.

Revision ID: 016
Revises: 015
Create Date: 2026-05-19
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _has_table(inspector, "handoff_attempts"):
        return
    op.create_table(
        "handoff_attempts",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("target_agent", sa.Text(), nullable=False),
        sa.Column("attempt_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("reply_message_id", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("runtime", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("superseded_by_attempt_id", sa.Text(), nullable=True),
        sa.Column("attempt_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=True),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["message_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "session_id", "message_id", "target_agent", "attempt_id"),
    )
    op.create_index(
        "idx_handoff_attempts_workspace_session_message",
        "handoff_attempts",
        ["workspace_id", "session_id", "message_id"],
    )
    op.create_index(
        "idx_handoff_attempts_agent_status",
        "handoff_attempts",
        ["workspace_id", "target_agent", "status"],
    )
    op.create_index(
        "idx_handoff_attempts_lease",
        "handoff_attempts",
        ["workspace_id", "status", "lease_expires_at"],
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _has_table(inspector, "handoff_attempts"):
        op.drop_index("idx_handoff_attempts_lease", table_name="handoff_attempts")
        op.drop_index("idx_handoff_attempts_agent_status", table_name="handoff_attempts")
        op.drop_index("idx_handoff_attempts_workspace_session_message", table_name="handoff_attempts")
        op.drop_table("handoff_attempts")
