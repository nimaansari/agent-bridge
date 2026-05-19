# -*- coding: utf-8 -*-
"""Reconcile handoff attempts table shape.

Revision ID: 018
Revises: 017
Create Date: 2026-05-19
"""

from alembic import op
import sqlalchemy as sa


revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _columns(inspector, table_name: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table_name)}


def _indexes(inspector, table_name: str) -> set[str]:
    return {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _has_table(inspector, "handoff_attempts"):
        return

    cols = _columns(inspector, "handoff_attempts")
    if "session_id" not in cols:
        op.add_column("handoff_attempts", sa.Column("session_id", sa.Text(), nullable=True))
        op.execute("UPDATE handoff_attempts SET session_id = 'default' WHERE session_id IS NULL")
        op.alter_column("handoff_attempts", "session_id", nullable=False)
    if "target_agent" not in cols:
        op.add_column("handoff_attempts", sa.Column("target_agent", sa.Text(), nullable=True))
        if "agent_name" in cols:
            op.execute("UPDATE handoff_attempts SET target_agent = agent_name WHERE target_agent IS NULL")
        op.execute("UPDATE handoff_attempts SET target_agent = 'unknown' WHERE target_agent IS NULL")
        op.alter_column("handoff_attempts", "target_agent", nullable=False)
    if "retryable" not in cols:
        op.add_column("handoff_attempts", sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")))
    for name in ["worker_id", "runtime", "error_code", "error_detail", "superseded_by_attempt_id"]:
        if name not in cols:
            op.add_column("handoff_attempts", sa.Column(name, sa.Text(), nullable=True))
    if "terminal_at" not in cols:
        op.add_column("handoff_attempts", sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True))

    # Older live deployments created primary key (message_id, agent_name,
    # attempt_id). The production model scopes attempts by workspace, session,
    # message, target agent, and attempt id. Keep legacy agent_name as a nullable
    # compatibility column but stop requiring it on new inserts.
    pk = inspector.get_pk_constraint("handoff_attempts").get("name")
    if pk:
        op.drop_constraint(pk, "handoff_attempts", type_="primary")
    if "agent_name" in _columns(sa.inspect(bind), "handoff_attempts"):
        op.alter_column("handoff_attempts", "agent_name", nullable=True)

    indexes = _indexes(sa.inspect(bind), "handoff_attempts")
    for idx in [
        "idx_handoff_attempts_workspace_message",
        "idx_handoff_attempts_agent_status",
        "idx_handoff_attempts_workspace_session_message",
        "idx_handoff_attempts_lease",
    ]:
        if idx in indexes:
            op.drop_index(idx, table_name="handoff_attempts")

    op.create_primary_key(
        "handoff_attempts_pkey",
        "handoff_attempts",
        ["workspace_id", "session_id", "message_id", "target_agent", "attempt_id"],
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
    pass
