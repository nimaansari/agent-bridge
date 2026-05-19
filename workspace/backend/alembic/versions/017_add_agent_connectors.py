# -*- coding: utf-8 -*-
"""Add durable agent connector registry and runtime session mapping.

Revision ID: 017
Revises: 016
Create Date: 2026-05-19
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if not _has_table(inspector, "agent_connectors"):
        op.create_table(
            "agent_connectors",
            sa.Column("workspace_id", postgresql.UUID(as_uuid=False), nullable=False),
            sa.Column("agent_name", sa.Text(), nullable=False),
            sa.Column("runtime_type", sa.Text(), nullable=False, server_default="custom"),
            sa.Column("command_template", sa.Text(), nullable=True),
            sa.Column("supports_threads", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
            sa.Column("supports_reply_anchor", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
            sa.Column("supports_files", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
            sa.Column("supports_seen_ack", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
            sa.Column("supports_processing_ack", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
            sa.Column("supports_cancel", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
            sa.Column("supports_freeze", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
            sa.Column("status", sa.Text(), nullable=False, server_default="offline"),
            sa.Column("worker_id", sa.Text(), nullable=True),
            sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=True),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=True),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("workspace_id", "agent_name"),
        )
        op.create_index("idx_agent_connectors_workspace_runtime", "agent_connectors", ["workspace_id", "runtime_type"])
        op.create_index("idx_agent_connectors_status", "agent_connectors", ["workspace_id", "status"])

    if not _has_table(inspector, "agent_runtime_sessions"):
        op.create_table(
            "agent_runtime_sessions",
            sa.Column("workspace_id", postgresql.UUID(as_uuid=False), nullable=False),
            sa.Column("session_id", sa.Text(), nullable=False),
            sa.Column("agent_name", sa.Text(), nullable=False),
            sa.Column("runtime_session_id", sa.Text(), nullable=False),
            sa.Column("runtime_type", sa.Text(), nullable=False, server_default="custom"),
            sa.Column("cursor", sa.Text(), nullable=True),
            sa.Column("last_seq", sa.BigInteger(), nullable=True),
            sa.Column("status", sa.Text(), nullable=False, server_default="active"),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=True),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("workspace_id", "session_id", "agent_name"),
        )
        op.create_index("idx_agent_runtime_sessions_agent", "agent_runtime_sessions", ["workspace_id", "agent_name"])
        op.create_index("idx_agent_runtime_sessions_runtime", "agent_runtime_sessions", ["workspace_id", "runtime_type", "runtime_session_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _has_table(inspector, "agent_runtime_sessions"):
        op.drop_index("idx_agent_runtime_sessions_runtime", table_name="agent_runtime_sessions")
        op.drop_index("idx_agent_runtime_sessions_agent", table_name="agent_runtime_sessions")
        op.drop_table("agent_runtime_sessions")
    if _has_table(inspector, "agent_connectors"):
        op.drop_index("idx_agent_connectors_status", table_name="agent_connectors")
        op.drop_index("idx_agent_connectors_workspace_runtime", table_name="agent_connectors")
        op.drop_table("agent_connectors")
