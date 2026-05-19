# -*- coding: utf-8 -*-
"""Durable connector enrollment and runtime session mapping endpoints."""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AgentConnector, AgentRuntimeSession, Workspace, WorkspaceMember
from app.response import ResponseCode, json_response, success_response
from app.routers.network import _verify_workspace_access, _workspace_filter

router = APIRouter(prefix="/v1", tags=["Connectors"])


class ConnectorEnrollmentRequest(BaseModel):
    network: str
    runtime_type: str = Field(default="custom", pattern=r"^(openclaw|hermes|custom)$")
    command_template: Optional[str] = None
    supports_threads: bool = False
    supports_reply_anchor: bool = False
    supports_files: bool = False
    supports_seen_ack: bool = False
    supports_processing_ack: bool = False
    supports_cancel: bool = False
    supports_freeze: bool = False
    enabled: bool = True
    worker_id: Optional[str] = None
    metadata: Optional[dict] = None


class ConnectorHeartbeatRequest(BaseModel):
    network: str
    worker_id: Optional[str] = None
    status: str = Field(default="online", pattern=r"^(online|offline|degraded)$")
    metadata: Optional[dict] = None


class RuntimeSessionMapRequest(BaseModel):
    network: str
    runtime_session_id: str
    runtime_type: str = Field(default="custom", pattern=r"^(openclaw|hermes|custom)$")
    cursor: Optional[str] = None
    last_seq: Optional[int] = None
    status: str = Field(default="active", pattern=r"^(active|paused|archived)$")
    metadata: Optional[dict] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_workspace(db: Session, network: str) -> Optional[Workspace]:
    return db.execute(select(Workspace).where(_workspace_filter(network))).scalar_one_or_none()


def _connector_payload(row: AgentConnector) -> dict:
    return {
        "workspace_id": str(row.workspace_id),
        "agent_name": row.agent_name,
        "runtime_type": row.runtime_type,
        "command_template": row.command_template,
        "capabilities": {
            "supports_threads": bool(row.supports_threads),
            "supports_reply_anchor": bool(row.supports_reply_anchor),
            "supports_files": bool(row.supports_files),
            "supports_seen_ack": bool(row.supports_seen_ack),
            "supports_processing_ack": bool(row.supports_processing_ack),
            "supports_cancel": bool(row.supports_cancel),
            "supports_freeze": bool(row.supports_freeze),
        },
        "enabled": bool(row.enabled),
        "status": row.status,
        "worker_id": row.worker_id,
        "last_heartbeat": row.last_heartbeat.isoformat() if row.last_heartbeat else None,
        "metadata": row.metadata_ or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _runtime_session_payload(row: AgentRuntimeSession) -> dict:
    return {
        "workspace_id": str(row.workspace_id),
        "session_id": row.session_id,
        "agent_name": row.agent_name,
        "runtime_session_id": row.runtime_session_id,
        "runtime_type": row.runtime_type,
        "cursor": row.cursor,
        "last_seq": row.last_seq,
        "status": row.status,
        "metadata": row.metadata_ or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("/connectors")
async def list_connectors(
    network: str = Query(...),
    runtime_type: Optional[str] = Query(None),
    enabled: Optional[bool] = Query(None),
    db: Session = Depends(get_db),
    x_workspace_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    workspace = _resolve_workspace(db, network)
    if not workspace:
        return json_response(ResponseCode.NOT_FOUND, "Network not found")
    if not _verify_workspace_access(workspace, x_workspace_token, authorization):
        return json_response(ResponseCode.UNAUTHORIZED, "Invalid workspace credentials")

    query = select(AgentConnector).where(AgentConnector.workspace_id == workspace.id)
    if runtime_type:
        query = query.where(AgentConnector.runtime_type == runtime_type)
    if enabled is not None:
        query = query.where(AgentConnector.enabled == enabled)
    rows = db.execute(query.order_by(AgentConnector.agent_name.asc())).scalars().all()
    return success_response({"connectors": [_connector_payload(row) for row in rows]})


@router.put("/connectors/{agent_name}")
async def enroll_connector(
    agent_name: str,
    body: ConnectorEnrollmentRequest,
    db: Session = Depends(get_db),
    x_workspace_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    workspace = _resolve_workspace(db, body.network)
    if not workspace:
        return json_response(ResponseCode.NOT_FOUND, "Network not found")
    if not _verify_workspace_access(workspace, x_workspace_token, authorization):
        return json_response(ResponseCode.UNAUTHORIZED, "Invalid workspace credentials")

    member = db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.agent_name == agent_name,
        )
    ).scalar_one_or_none()
    if not member:
        member = WorkspaceMember(
            workspace_id=workspace.id,
            agent_name=agent_name,
            role="member",
            agent_type=body.runtime_type,
            status="offline",
        )
        db.add(member)
    else:
        member.agent_type = body.runtime_type

    connector = db.execute(
        select(AgentConnector).where(
            AgentConnector.workspace_id == workspace.id,
            AgentConnector.agent_name == agent_name,
        )
    ).scalar_one_or_none()
    if not connector:
        connector = AgentConnector(workspace_id=workspace.id, agent_name=agent_name)
        db.add(connector)

    connector.runtime_type = body.runtime_type
    connector.command_template = body.command_template
    connector.supports_threads = body.supports_threads
    connector.supports_reply_anchor = body.supports_reply_anchor
    connector.supports_files = body.supports_files
    connector.supports_seen_ack = body.supports_seen_ack
    connector.supports_processing_ack = body.supports_processing_ack
    connector.supports_cancel = body.supports_cancel
    connector.supports_freeze = body.supports_freeze
    connector.enabled = body.enabled
    connector.worker_id = body.worker_id
    connector.metadata_ = body.metadata or {}
    connector.updated_at = func.now()

    db.commit()
    db.refresh(connector)
    return success_response({"connector": _connector_payload(connector)})


@router.post("/connectors/{agent_name}/heartbeat")
async def connector_heartbeat(
    agent_name: str,
    body: ConnectorHeartbeatRequest,
    db: Session = Depends(get_db),
    x_workspace_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    workspace = _resolve_workspace(db, body.network)
    if not workspace:
        return json_response(ResponseCode.NOT_FOUND, "Network not found")
    if not _verify_workspace_access(workspace, x_workspace_token, authorization):
        return json_response(ResponseCode.UNAUTHORIZED, "Invalid workspace credentials")

    connector = db.execute(
        select(AgentConnector).where(
            AgentConnector.workspace_id == workspace.id,
            AgentConnector.agent_name == agent_name,
        )
    ).scalar_one_or_none()
    if not connector:
        return json_response(ResponseCode.NOT_FOUND, "Connector not enrolled")

    now = _now()
    connector.status = body.status
    connector.worker_id = body.worker_id or connector.worker_id
    connector.last_heartbeat = now
    connector.updated_at = func.now()
    if body.metadata is not None:
        connector.metadata_ = body.metadata

    member = db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.agent_name == agent_name,
        )
    ).scalar_one_or_none()
    if member:
        member.status = body.status
        member.last_heartbeat = now

    db.commit()
    db.refresh(connector)
    return success_response({"connector": _connector_payload(connector)})


@router.put("/sessions/{session_id}/agents/{agent_name}/runtime-session")
async def upsert_runtime_session_mapping(
    session_id: str,
    agent_name: str,
    body: RuntimeSessionMapRequest,
    db: Session = Depends(get_db),
    x_workspace_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    workspace = _resolve_workspace(db, body.network)
    if not workspace:
        return json_response(ResponseCode.NOT_FOUND, "Network not found")
    if not _verify_workspace_access(workspace, x_workspace_token, authorization):
        return json_response(ResponseCode.UNAUTHORIZED, "Invalid workspace credentials")

    mapping = db.execute(
        select(AgentRuntimeSession).where(
            AgentRuntimeSession.workspace_id == workspace.id,
            AgentRuntimeSession.session_id == session_id,
            AgentRuntimeSession.agent_name == agent_name,
        )
    ).scalar_one_or_none()
    if not mapping:
        mapping = AgentRuntimeSession(
            workspace_id=workspace.id,
            session_id=session_id,
            agent_name=agent_name,
        )
        db.add(mapping)

    mapping.runtime_session_id = body.runtime_session_id
    mapping.runtime_type = body.runtime_type
    mapping.cursor = body.cursor
    mapping.last_seq = body.last_seq
    mapping.status = body.status
    mapping.metadata_ = body.metadata or {}
    mapping.updated_at = func.now()

    db.commit()
    db.refresh(mapping)
    return success_response({"runtime_session": _runtime_session_payload(mapping)})


@router.get("/sessions/{session_id}/runtime-sessions")
async def list_runtime_session_mappings(
    session_id: str,
    network: str = Query(...),
    agent_name: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    x_workspace_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    workspace = _resolve_workspace(db, network)
    if not workspace:
        return json_response(ResponseCode.NOT_FOUND, "Network not found")
    if not _verify_workspace_access(workspace, x_workspace_token, authorization):
        return json_response(ResponseCode.UNAUTHORIZED, "Invalid workspace credentials")

    query = select(AgentRuntimeSession).where(
        AgentRuntimeSession.workspace_id == workspace.id,
        AgentRuntimeSession.session_id == session_id,
    )
    if agent_name:
        query = query.where(AgentRuntimeSession.agent_name == agent_name)
    rows = db.execute(query.order_by(AgentRuntimeSession.agent_name.asc())).scalars().all()
    return success_response({"runtime_sessions": [_runtime_session_payload(row) for row in rows]})
