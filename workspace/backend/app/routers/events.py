# -*- coding: utf-8 -*-
"""
ONM Event endpoints — the core of the event-native API.

POST /v1/events    Send any event into the mod pipeline
GET  /v1/events    Poll events (filter by after, target, channel, type)
"""

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query
from pydantic import BaseModel
from sqlalchemy import and_, case, cast, func, or_, select, Text
from sqlalchemy.orm import Session

from app import cache
from app.database import get_db
from app.models import Channel, ChannelMember, EventRecord, HandoffAttempt, Workspace
from app.pipeline_factory import pipeline
from app.response import ResponseCode, json_response, success_response
from app.routers.network import _verify_workspace_access, _workspace_filter
from openagents.core.onm_events import Event
from openagents.core.onm_mods import EventRejected, PipelineContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["Events"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class SendEventRequest(BaseModel):
    type: str
    source: str
    target: str
    payload: Optional[dict] = None
    metadata: Optional[dict] = None
    visibility: Optional[str] = "channel"
    network: Optional[str] = None   # workspace ID or slug


class AckEventRequest(BaseModel):
    network: str
    agent_name: str
    status: str  # queued | delivered | seen | processing | paused | stalled | replied | failed | cancelled
    detail: Optional[str] = None
    source: Optional[str] = None
    attempt_id: Optional[str] = None
    reply_message_id: Optional[str] = None
    lease_expires_at: Optional[datetime] = None
    worker_id: Optional[str] = None
    runtime: Optional[str] = None
    error_code: Optional[str] = None
    error_detail: Optional[str] = None
    retryable: Optional[bool] = None
    metadata: Optional[dict] = None


class RequeueHandoffRequest(BaseModel):
    network: str
    agent_name: str
    attempt_id: Optional[str] = None
    from_attempt_id: Optional[str] = None
    detail: Optional[str] = None
    worker_id: Optional[str] = None
    runtime: Optional[str] = None
    metadata: Optional[dict] = None


def _attempt_id(value: Optional[str]) -> str:
    value = (value or "default").strip()
    return re.sub(r"[^A-Za-z0-9_.:-]+", "-", value)[:120] or "default"


def _session_id_from_target(target: Optional[str]) -> str:
    target = (target or "").strip()
    if target.startswith("channel/"):
        return target.split("/", 1)[1] or target
    return target or "default"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_aware(value: Optional[datetime]) -> Optional[datetime]:
    if not value:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _reply_message_id_from_detail(detail: Optional[str]) -> Optional[str]:
    if not detail:
        return None
    match = re.search(r"reply_event_id=([^;\s]+)", detail)
    return match.group(1) if match else None


def _event_dict(row: EventRecord) -> dict:
    return {
        "id": row.id,
        "type": row.type,
        "source": row.source,
        "target": row.target,
        "payload": row.payload,
        "metadata": row.metadata_,
        "timestamp": row.timestamp,
        "visibility": row.visibility,
    }


def _source_agent(source: str) -> Optional[str]:
    return source.split(":", 1)[1] if source.startswith("openagents:") else None


def _terminal_for_agent(row: EventRecord, agent_name: str) -> bool:
    responses = (row.metadata_ or {}).get("handoff_responses") or {}
    status = (responses.get(agent_name) or {}).get("status")
    return status in {"replied", "failed"}


def _is_actionable_for_agent(row: EventRecord, agent_name: str) -> bool:
    """Server-side actionable inbox predicate for runtime adapters.

    Transcript events are intentionally noisy: chat, acks, statuses, files,
    replies, human/admin notes. Runtime adapters should consume this inbox
    predicate instead of inferring obligations from the raw room feed.
    """
    if row.type != "workspace.message.posted":
        return False
    if _source_agent(row.source or "") == agent_name:
        return False
    payload = row.payload or {}
    if payload.get("message_type") in {"thinking", "status", "tool", "tool_call", "tool_result", "todos"}:
        return False
    metadata = row.metadata_ or {}
    required = metadata.get("required_responses") or []
    if agent_name in required:
        return not _terminal_for_agent(row, agent_name)
    targets = metadata.get("target_agents") or []
    if agent_name in targets:
        return not _terminal_for_agent(row, agent_name)
    return False


def _mark_expired_processing_attempts_stalled(db: Session, workspace_id: str, message_id: str) -> None:
    now = _utcnow()
    attempts = db.execute(
        select(HandoffAttempt).where(
            HandoffAttempt.workspace_id == workspace_id,
            HandoffAttempt.message_id == message_id,
            HandoffAttempt.status == "processing",
            HandoffAttempt.lease_expires_at.isnot(None),
        )
    ).scalars().all()
    for attempt in attempts:
        lease_expires_at = _coerce_aware(attempt.lease_expires_at)
        if lease_expires_at and lease_expires_at <= now:
            attempt.status = "stalled"
            attempt.updated_at = func.now()


def _upsert_handoff_attempt(
    db: Session,
    workspace_id: str,
    session_id: str,
    message_id: str,
    target_agent: str,
    status: str,
    detail: Optional[str] = None,
    attempt_id: Optional[str] = None,
    reply_message_id: Optional[str] = None,
    lease_expires_at: Optional[datetime] = None,
    worker_id: Optional[str] = None,
    runtime: Optional[str] = None,
    error_code: Optional[str] = None,
    error_detail: Optional[str] = None,
    retryable: Optional[bool] = None,
    metadata: Optional[dict] = None,
) -> HandoffAttempt:
    normalized_attempt_id = _attempt_id(attempt_id)
    _mark_expired_processing_attempts_stalled(db, workspace_id, message_id)
    attempt = db.execute(
        select(HandoffAttempt).where(
            HandoffAttempt.workspace_id == workspace_id,
            HandoffAttempt.session_id == session_id,
            HandoffAttempt.message_id == message_id,
            HandoffAttempt.target_agent == target_agent,
            HandoffAttempt.attempt_id == normalized_attempt_id,
        )
    ).scalar_one_or_none()
    if not attempt:
        attempt = HandoffAttempt(
            workspace_id=workspace_id,
            session_id=session_id,
            message_id=message_id,
            target_agent=target_agent,
            attempt_id=normalized_attempt_id,
        )
        db.add(attempt)

    terminal_statuses = {"replied", "failed", "cancelled"}
    if attempt.status in terminal_statuses:
        # A stale adapter/watchdog can reconnect and replay older lifecycle
        # acks (delivered/seen/processing/failed) for the same attempt after a
        # successful reply was already recorded. Treat terminal attempts as
        # immutable unless the caller creates a distinct attempt_id (normally
        # via /handoffs/requeue). This keeps per-agent inbox delivery durable:
        # a completed required response must not regress back into the queue.
        return attempt

    attempt.status = status
    attempt.detail = detail
    attempt.reply_message_id = reply_message_id or _reply_message_id_from_detail(detail)
    attempt.lease_expires_at = lease_expires_at
    attempt.worker_id = worker_id
    attempt.runtime = runtime
    attempt.error_code = error_code
    attempt.error_detail = error_detail
    if retryable is not None:
        attempt.retryable = retryable
    if metadata is not None:
        attempt.attempt_metadata = metadata
    if status in {"replied", "failed", "cancelled"}:
        attempt.terminal_at = func.now()
    else:
        attempt.terminal_at = None
    attempt.updated_at = func.now()
    return attempt


def _attempt_is_obligation_terminal(attempt: HandoffAttempt) -> bool:
    if attempt.status in {"replied", "cancelled"}:
        return True
    if attempt.status == "failed" and not attempt.retryable:
        return True
    return False


def _latest_attempts_by_agent(attempts: list[HandoffAttempt]) -> dict[str, HandoffAttempt]:
    latest: dict[str, HandoffAttempt] = {}
    for attempt in attempts:
        current = latest.get(attempt.target_agent)
        if current is None or (attempt.created_at or datetime.min.replace(tzinfo=timezone.utc)) >= (current.created_at or datetime.min.replace(tzinfo=timezone.utc)):
            latest[attempt.target_agent] = attempt
    return latest


# ---------------------------------------------------------------------------
# POST /v1/events — send an event through the pipeline
# ---------------------------------------------------------------------------

def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    """Extract bearer token from Authorization header."""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


@router.post("/events")
async def send_event(
    body: SendEventRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    x_workspace_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """
    Send an event into the network pipeline.

    The event flows through mod/auth → mod/workspace → mod/persistence
    before delivery to the target.
    """
    if not body.network:
        return json_response(ResponseCode.BAD_REQUEST, "Missing required field: network")

    # Resolve workspace
    workspace = db.execute(
        select(Workspace).where(_workspace_filter(body.network))
    ).scalar_one_or_none()

    if not workspace:
        return json_response(ResponseCode.NOT_FOUND, "Network not found")

    # Build ONM Event
    event = Event(
        type=body.type,
        source=body.source,
        target=body.target,
        payload=body.payload,
        metadata=body.metadata or {},
        visibility=body.visibility or "channel",
        network=str(workspace.id),
    )

    # Build pipeline context — extra kwargs become context.extra dict
    context = PipelineContext(
        network_id=str(workspace.id),
        agent_address=body.source,
        db=db,
        workspace=workspace,
        token=x_workspace_token,
        bearer_token=_extract_bearer(authorization),
    )

    # Run through pipeline
    try:
        result = await pipeline.process(event, context)
    except EventRejected as exc:
        # Surface the reason so clients can roll back optimistic UI on
        # specific failures (e.g. routine_channel_locked,
        # channel_join_forbidden). 403 distinguishes "you can't do this"
        # from generic auth failures.
        reason = exc.reason or "rejected"
        code = ResponseCode.BAD_REQUEST if (
            reason.startswith("reply_")
            or reason.startswith("duplicate_agent_message")
            or reason.startswith("runtime_failure_message")
        ) else (
            ResponseCode.FORBIDDEN if (
                "forbidden" in reason or "locked" in reason or "frozen" in reason
            ) else ResponseCode.UNAUTHORIZED
        )
        return json_response(code, reason)

    # Session revocation: another client has since joined as this agent.
    # Return a clear error so the stale client can stop its adapter.
    if result.metadata.get("session_error") == "session_revoked":
        db.rollback()
        return json_response(
            ResponseCode.UNAUTHORIZED,
            "session_revoked: another client is now running as this agent",
        )

    db.commit()

    # Fan out push notifications for relevant events. Runs after the
    # response is sent (FastAPI BackgroundTasks); never blocks event
    # creation; failures are logged but never raised. The service opens
    # its own short-lived DB session because `db` here is request-scoped.
    from app.services.push import fanout_for_event
    background_tasks.add_task(
        fanout_for_event,
        str(workspace.id),
        {
            "id": result.id,
            "type": result.type,
            "source": result.source,
            "target": result.target,
            "payload": result.payload,
            "metadata": result.metadata,
            "visibility": result.visibility,
            "timestamp": result.timestamp,
        },
    )

    return success_response({
        "id": result.id,
        "type": result.type,
        "source": result.source,
        "target": result.target,
        "payload": result.payload,
        "timestamp": result.timestamp,
        "metadata": result.metadata,
        "visibility": result.visibility,
    })


@router.post("/events/{event_id}/ack")
async def ack_event(
    event_id: str,
    body: AckEventRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    x_workspace_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Record delivery/read/processing state for a session message.

    This makes agent communication observable: clients can distinguish
    "message is stored" from "target agent saw it" and "target agent is
    processing it". Ack events are ordinary persisted events, but they are
    marked no-response so they never wake agents into loops.
    """
    workspace = db.execute(
        select(Workspace).where(_workspace_filter(body.network))
    ).scalar_one_or_none()
    if not workspace:
        return json_response(ResponseCode.NOT_FOUND, "Network not found")
    if not _verify_workspace_access(workspace, x_workspace_token, authorization):
        return json_response(ResponseCode.UNAUTHORIZED, "Invalid workspace credentials")

    original = db.execute(
        select(EventRecord).where(
            EventRecord.network_id == workspace.id,
            EventRecord.id == event_id,
        )
    ).scalar_one_or_none()
    if not original:
        return json_response(ResponseCode.NOT_FOUND, "Event not found")

    allowed = {"queued", "delivered", "seen", "processing", "paused", "stalled", "replied", "failed", "cancelled"}
    if body.status not in allowed:
        return json_response(ResponseCode.BAD_REQUEST, f"Invalid ack status: {body.status}")

    session_id = _session_id_from_target(original.target)
    attempt = _upsert_handoff_attempt(
        db,
        str(workspace.id),
        session_id,
        event_id,
        body.agent_name,
        body.status,
        body.detail,
        body.attempt_id,
        body.reply_message_id,
        body.lease_expires_at,
        body.worker_id,
        body.runtime,
        body.error_code,
        body.error_detail,
        body.retryable,
        body.metadata,
    )
    db.flush()

    attempts = db.execute(
        select(HandoffAttempt).where(
            HandoffAttempt.workspace_id == str(workspace.id),
            HandoffAttempt.session_id == session_id,
            HandoffAttempt.message_id == event_id,
        )
    ).scalars().all()
    latest_attempts = _latest_attempts_by_agent(attempts)

    original_metadata = dict(original.metadata_ or {})
    responses = dict(original_metadata.get("handoff_responses") or {})
    response_payload = {
        "status": attempt.status,
        "attempt_id": attempt.attempt_id,
        "retryable": bool(attempt.retryable),
        **({"detail": attempt.detail} if attempt.detail else {}),
        **({"reply_message_id": attempt.reply_message_id} if attempt.reply_message_id else {}),
        **({"error_code": attempt.error_code} if attempt.error_code else {}),
    }
    responses[body.agent_name] = response_payload
    original_metadata["handoff_responses"] = responses

    required = original_metadata.get("required_responses") or []
    if original_metadata.get("response_required") and required:
        required_latest = [latest_attempts.get(agent) for agent in required]
        if all(attempt and _attempt_is_obligation_terminal(attempt) for attempt in required_latest):
            original_metadata["handoff_state"] = "complete"
        elif any(attempt and attempt.status == "processing" for attempt in required_latest):
            original_metadata["handoff_state"] = "processing"
        elif any(attempt and attempt.status == "stalled" for attempt in required_latest):
            original_metadata["handoff_state"] = "stalled"
        elif any(attempt and attempt.status == "paused" for attempt in required_latest):
            original_metadata["handoff_state"] = "paused"
        else:
            original_metadata.setdefault("handoff_state", "pending")
    original.metadata_ = original_metadata

    source = body.source or f"openagents:{body.agent_name}"
    event = Event(
        type="workspace.message.ack",
        source=source,
        target=original.target,
        payload={
            "message_id": event_id,
            "agent_name": body.agent_name,
            "status": body.status,
            **({"detail": body.detail} if body.detail else {}),
        },
        metadata={"reply_to": event_id, "target_agents": ["__no_response__"]},
        visibility="channel",
        network=str(workspace.id),
    )
    context = PipelineContext(
        network_id=str(workspace.id),
        agent_address=source,
        db=db,
        workspace=workspace,
        token=x_workspace_token,
        bearer_token=_extract_bearer(authorization),
    )
    try:
        result = await pipeline.process(event, context)
    except EventRejected as exc:
        return json_response(ResponseCode.FORBIDDEN, exc.reason or "rejected")
    db.commit()

    from app.services.push import fanout_for_event
    background_tasks.add_task(
        fanout_for_event,
        str(workspace.id),
        {
            "id": result.id,
            "type": result.type,
            "source": result.source,
            "target": result.target,
            "payload": result.payload,
            "timestamp": result.timestamp,
        },
    )
    return success_response({
        "id": result.id,
        "type": result.type,
        "source": result.source,
        "target": result.target,
        "timestamp": result.timestamp,
        "metadata": result.metadata,
    })


@router.post("/events/{event_id}/handoffs/requeue")
async def requeue_event_handoff(
    event_id: str,
    body: RequeueHandoffRequest,
    db: Session = Depends(get_db),
    x_workspace_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Create a new queued attempt for a retryable obligation.

    The previous attempt remains terminal/history; it is linked forward via
    superseded_by_attempt_id so clients can show retry lineage without treating
    `failed` as permanently processed.
    """
    workspace = db.execute(
        select(Workspace).where(_workspace_filter(body.network))
    ).scalar_one_or_none()
    if not workspace:
        return json_response(ResponseCode.NOT_FOUND, "Network not found")
    if not _verify_workspace_access(workspace, x_workspace_token, authorization):
        return json_response(ResponseCode.UNAUTHORIZED, "Invalid workspace credentials")

    original = db.execute(
        select(EventRecord).where(
            EventRecord.network_id == workspace.id,
            EventRecord.id == event_id,
        )
    ).scalar_one_or_none()
    if not original:
        return json_response(ResponseCode.NOT_FOUND, "Event not found")

    session_id = _session_id_from_target(original.target)
    existing = db.execute(
        select(HandoffAttempt).where(
            HandoffAttempt.workspace_id == str(workspace.id),
            HandoffAttempt.session_id == session_id,
            HandoffAttempt.message_id == event_id,
            HandoffAttempt.target_agent == body.agent_name,
        ).order_by(HandoffAttempt.created_at.desc())
    ).scalars().all()
    previous = None
    if body.from_attempt_id:
        previous = next((row for row in existing if row.attempt_id == _attempt_id(body.from_attempt_id)), None)
        if not previous:
            return json_response(ResponseCode.NOT_FOUND, "Source attempt not found")
    elif existing:
        previous = existing[0]

    next_attempt_id = _attempt_id(body.attempt_id) if body.attempt_id else f"attempt-{len(existing) + 1}"
    if any(row.attempt_id == next_attempt_id for row in existing):
        return json_response(ResponseCode.BAD_REQUEST, "Attempt already exists")

    attempt = HandoffAttempt(
        workspace_id=str(workspace.id),
        session_id=session_id,
        message_id=event_id,
        target_agent=body.agent_name,
        attempt_id=next_attempt_id,
        status="queued",
        retryable=False,
        detail=body.detail,
        worker_id=body.worker_id,
        runtime=body.runtime,
        attempt_metadata=body.metadata or {},
    )
    db.add(attempt)
    if previous:
        previous.superseded_by_attempt_id = next_attempt_id
        previous.updated_at = func.now()

    original_metadata = dict(original.metadata_ or {})
    responses = dict(original_metadata.get("handoff_responses") or {})
    responses[body.agent_name] = {"status": "queued", "attempt_id": next_attempt_id}
    original_metadata["handoff_responses"] = responses
    original_metadata["handoff_state"] = "pending"
    original.metadata_ = original_metadata

    db.commit()
    return success_response({
        "workspace_id": str(workspace.id),
        "session_id": session_id,
        "message_id": event_id,
        "target_agent": body.agent_name,
        "attempt_id": next_attempt_id,
        "status": "queued",
        "supersedes_attempt_id": previous.attempt_id if previous else None,
    })


@router.get("/events/{event_id}/handoffs")
async def get_event_handoffs(
    event_id: str,
    network: str = Query(..., description="Network (workspace) ID or slug"),
    db: Session = Depends(get_db),
    x_workspace_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Return durable handoff attempts for one message.

    This is the first production path off metadata-only handoff state. The
    original event metadata remains for backward-compatible transcript badges,
    while this endpoint exposes attempt rows for retries, leases, and future
    connector dashboards.
    """
    workspace = db.execute(
        select(Workspace).where(_workspace_filter(network))
    ).scalar_one_or_none()
    if not workspace:
        return json_response(ResponseCode.NOT_FOUND, "Network not found")
    if not _verify_workspace_access(workspace, x_workspace_token, authorization):
        return json_response(ResponseCode.UNAUTHORIZED, "Invalid workspace credentials")

    original = db.execute(
        select(EventRecord).where(
            EventRecord.network_id == workspace.id,
            EventRecord.id == event_id,
        )
    ).scalar_one_or_none()
    if not original:
        return json_response(ResponseCode.NOT_FOUND, "Event not found")

    session_id = _session_id_from_target(original.target)
    _mark_expired_processing_attempts_stalled(db, str(workspace.id), event_id)
    db.flush()
    attempts = db.execute(
        select(HandoffAttempt)
        .where(
            HandoffAttempt.workspace_id == workspace.id,
            HandoffAttempt.session_id == session_id,
            HandoffAttempt.message_id == event_id,
        )
        .order_by(HandoffAttempt.target_agent.asc(), HandoffAttempt.created_at.asc())
    ).scalars().all()
    return success_response({
        "workspace_id": str(workspace.id),
        "session_id": session_id,
        "message_id": event_id,
        "handoff_state": (original.metadata_ or {}).get("handoff_state"),
        "required_responses": (original.metadata_ or {}).get("required_responses") or [],
        "attempts": [
            {
                "workspace_id": str(row.workspace_id),
                "session_id": row.session_id,
                "message_id": row.message_id,
                "target_agent": row.target_agent,
                "agent_name": row.target_agent,  # backward-compatible alias
                "attempt_id": row.attempt_id,
                "status": row.status,
                "retryable": row.retryable,
                "detail": row.detail,
                "reply_message_id": row.reply_message_id,
                "lease_expires_at": row.lease_expires_at.isoformat() if row.lease_expires_at else None,
                "worker_id": row.worker_id,
                "runtime": row.runtime,
                "error_code": row.error_code,
                "error_detail": row.error_detail,
                "superseded_by_attempt_id": row.superseded_by_attempt_id,
                "metadata": row.attempt_metadata or {},
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "terminal_at": row.terminal_at.isoformat() if row.terminal_at else None,
            }
            for row in attempts
        ],
    })


# ---------------------------------------------------------------------------
# GET /v1/events — poll events
# ---------------------------------------------------------------------------

@router.get("/events")
async def poll_events(
    network: str = Query(..., description="Network (workspace) ID or slug"),
    after: Optional[str] = Query(None, description="Return events after this event ID"),
    before: Optional[str] = Query(None, description="Return events before this event ID"),
    target: Optional[str] = Query(None, description="Filter by target address"),
    channel: Optional[str] = Query(None, description="Filter by channel name"),
    type: Optional[str] = Query(None, description="Filter by event type prefix"),
    conversation: Optional[str] = Query(None, description="Filter to DM conversation between two agents (comma-separated addresses)"),
    search: Optional[str] = Query(None, description="Search message content (case-insensitive)"),
    member: Optional[str] = Query(None, description="Filter to channels where this agent is a member"),
    sort: Optional[str] = Query(None, description="Sort order: 'asc' (default) or 'desc'"),
    limit: int = Query(50, ge=1, le=200, description="Max events to return"),
    db: Session = Depends(get_db),
    x_workspace_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """
    Poll events from the network.

    Supports filtering by target, channel, type, and cursor-based pagination
    using the `after` parameter (event ID — events are sorted by timestamp).
    """
    # Resolve workspace
    workspace = db.execute(
        select(Workspace).where(_workspace_filter(network))
    ).scalar_one_or_none()

    if not workspace:
        return json_response(ResponseCode.NOT_FOUND, "Network not found")

    if not _verify_workspace_access(workspace, x_workspace_token, authorization):
        return json_response(ResponseCode.UNAUTHORIZED, "Invalid workspace credentials")

    # Two-level read-through cache for poll traffic.
    #
    # Level 1: FULL key (includes `after`/`before` cursor). Dedupes identical
    # polls from the same agent within the TTL window. Correct for any
    # parameters.
    #
    # Level 2: HEAD-CURSOR tracking. When a non-empty poll returns events,
    # we remember the newest event id for these filters. When a subsequent
    # poll comes in with `after = cached_head_id` (i.e. the client is
    # already caught up to the most recent event we've seen), its "give me
    # anything newer" query is equivalent to a no-cursor "give me the empty
    # set". Many agents sharing the head cursor all hash to the same
    # Level-2 key and share a single DB hit.
    #
    # This is a strict correctness guarantee: we only route to Level 2 when
    # the caller's cursor is EQUAL to the tracked head. Agents that are
    # behind (historical backfill) fall through to Level 1 / DB.
    cache_key = None
    at_head_key = None
    head_tracker_key = None
    incoming_after = after or ""
    if not search and not member:
        key_parts = [
            str(workspace.id), target or "", channel or "",
            type or "", conversation or "",
            after or "", before or "",
            sort or "asc", str(limit),
        ]
        cache_key = "v1events:full:" + hashlib.sha1(
            "|".join(key_parts).encode("utf-8")
        ).hexdigest()

        # Per-filter head cursor marker (what the newest event id was for
        # this filter the last time we saw any events). Cursor-free.
        filter_parts = [
            str(workspace.id), target or "", channel or "",
            type or "", conversation or "",
            sort or "asc", str(limit),
        ]
        filter_hash = hashlib.sha1("|".join(filter_parts).encode("utf-8")).hexdigest()
        head_tracker_key = "v1events:head:" + filter_hash

        import json as _json

        # Level 1: exact-match cache
        cached = cache.get_bytes(cache_key)
        if cached is not None:
            try:
                return _json.loads(cached)
            except Exception:
                pass

        # Level 2: if client is at head (after == last-known head), route
        # to a shared cached-empty response. Only fires when we already know
        # the head AND client's cursor matches it — so agents behind head
        # cannot receive this cached empty by mistake.
        if before is None:
            head_id = cache.get_bytes(head_tracker_key)
            if head_id is not None:
                head_id_str = head_id.decode("utf-8") if isinstance(head_id, bytes) else str(head_id)
                if head_id_str and head_id_str == incoming_after:
                    at_head_key = "v1events:athead:" + filter_hash
                    cached_empty = cache.get_bytes(at_head_key)
                    if cached_empty is not None:
                        try:
                            return _json.loads(cached_empty)
                        except Exception:
                            pass

    query = select(EventRecord).where(EventRecord.network_id == workspace.id)

    # Filter events to only channels where the agent is a member
    if member:
        member_channel_names = db.execute(
            select(Channel.name).where(
                Channel.workspace_id == workspace.id,
                Channel.id.in_(
                    select(ChannelMember.channel_id).where(ChannelMember.agent_name == member)
                ),
            )
        ).scalars().all()
        channel_targets = [f"channel/{name}" for name in member_channel_names]
        if channel_targets:
            query = query.where(EventRecord.target.in_(channel_targets))
        else:
            # Agent is not a member of any channel — return empty
            return success_response({"events": [], "has_more": False})

    if conversation:
        parts = [p.strip() for p in conversation.split(",", 1)]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return json_response(ResponseCode.BAD_REQUEST, "conversation must be two comma-separated addresses")
        a, b = parts
        query = query.where(
            EventRecord.visibility == "direct",
            ~EventRecord.target.startswith("channel/"),
            or_(
                and_(EventRecord.source == a, EventRecord.target == b),
                and_(EventRecord.source == b, EventRecord.target == a),
            ),
        )

    if after:
        cursor_row = db.execute(
            select(EventRecord.timestamp, EventRecord.id).where(EventRecord.id == after)
        ).one_or_none()
        if cursor_row is not None:
            # Use (timestamp, id) tuple to avoid skipping/duplicating events with the same timestamp
            query = query.where(
                or_(
                    EventRecord.timestamp > cursor_row.timestamp,
                    and_(EventRecord.timestamp == cursor_row.timestamp, EventRecord.id > cursor_row.id),
                )
            )

    if before:
        cursor_row = db.execute(
            select(EventRecord.timestamp, EventRecord.id).where(EventRecord.id == before)
        ).one_or_none()
        if cursor_row is not None:
            query = query.where(
                or_(
                    EventRecord.timestamp < cursor_row.timestamp,
                    and_(EventRecord.timestamp == cursor_row.timestamp, EventRecord.id < cursor_row.id),
                )
            )

    if target:
        query = query.where(EventRecord.target == target)

    if channel:
        query = query.where(EventRecord.target == f"channel/{channel}")

    if type:
        query = query.where(EventRecord.type.startswith(type))

    if search:
        # Search within payload JSON for content field (works with both JSONB and JSON)
        query = query.where(
            cast(EventRecord.payload, Text).ilike(f"%{search}%")
        )

    if sort == "desc":
        query = query.order_by(EventRecord.timestamp.desc(), EventRecord.id.desc()).limit(limit + 1)
    else:
        query = query.order_by(EventRecord.timestamp.asc(), EventRecord.id.asc()).limit(limit + 1)
    rows = db.execute(query).scalars().all()

    has_more = len(rows) > limit
    events = rows[:limit]

    response = success_response({
        "events": [
            _event_dict(e)
            for e in events
        ],
        "has_more": has_more,
        "oldest_id": (events[-1].id if sort == "desc" else events[0].id) if events else None,
        "newest_id": (events[0].id if sort == "desc" else events[-1].id) if events else None,
    })

    # Populate cache for subsequent polls within the TTL window.
    # success_response returns a dict; Redis stores the serialized JSON.
    if cache_key is not None and isinstance(response, dict):
        try:
            import json as _json
            serialized = _json.dumps(
                response, default=str, separators=(",", ":")
            ).encode("utf-8")
            # Level 1: exact-match cache (includes cursor). Slightly
            # longer TTL helps dedup adjacent polls from the same agent.
            cache.set_bytes(cache_key, serialized, ttl_seconds=1.5)

            # Level 2 maintenance — track the head cursor for these
            # filters, and cache the "empty" response when the client was
            # already at head.
            if head_tracker_key is not None:
                newest_id = response.get("data", {}).get("newest_id")
                if events and newest_id:
                    # Update head tracker — newest_id is the tip we just saw.
                    # Longer TTL because head updates are cheap and we want
                    # subsequent at-head checks to find it.
                    cache.set_bytes(
                        head_tracker_key,
                        str(newest_id).encode("utf-8"),
                        ttl_seconds=30.0,
                    )
                elif not events and incoming_after:
                    # DB returned empty AND the client had a cursor. This
                    # confirms "after = head" for this filter. Populate
                    # both the head tracker (so other clients can match)
                    # and the shared at-head empty response.
                    filter_hash = head_tracker_key.split(":")[-1]
                    cache.set_bytes(
                        head_tracker_key,
                        incoming_after.encode("utf-8"),
                        ttl_seconds=30.0,
                    )
                    cache.set_bytes(
                        "v1events:athead:" + filter_hash,
                        serialized,
                        ttl_seconds=1.5,
                    )
        except Exception:
            pass

    return response


@router.get("/agents/{agent_name}/inbox")
async def poll_agent_inbox(
    agent_name: str,
    network: str = Query(..., description="Network (workspace) ID or slug"),
    channel: Optional[str] = Query(None, description="Restrict to one session/channel name"),
    after: Optional[str] = Query(None, description="Return actionable events after this event ID"),
    limit: int = Query(50, ge=1, le=100, description="Max actionable events to return"),
    db: Session = Depends(get_db),
    x_workspace_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Return only actionable messages for one agent.

    This endpoint is the production transport surface for runtime adapters.
    It separates the human-visible transcript from the per-agent work queue so
    agents do not mix up chat, acks, status noise, self-messages, or messages
    intended for other agents.
    """
    workspace = db.execute(
        select(Workspace).where(_workspace_filter(network))
    ).scalar_one_or_none()
    if not workspace:
        return json_response(ResponseCode.NOT_FOUND, "Network not found")
    if not _verify_workspace_access(workspace, x_workspace_token, authorization):
        return json_response(ResponseCode.UNAUTHORIZED, "Invalid workspace credentials")

    query = select(EventRecord).where(
        EventRecord.network_id == workspace.id,
        EventRecord.type == "workspace.message.posted",
    )
    if channel:
        query = query.where(EventRecord.target == f"channel/{channel}")

    if after:
        cursor_row = db.execute(
            select(EventRecord.timestamp, EventRecord.id).where(
                EventRecord.network_id == workspace.id,
                EventRecord.id == after,
            )
        ).one_or_none()
        if cursor_row is not None:
            query = query.where(
                or_(
                    EventRecord.timestamp > cursor_row.timestamp,
                    and_(EventRecord.timestamp == cursor_row.timestamp, EventRecord.id > cursor_row.id),
                )
            )

    # Pull a bounded candidate window and filter in Python for SQLite/Postgres
    # portability across JSON metadata shapes. The candidate window is larger
    # than the returned limit so the inbox remains useful even when the room is
    # busy with unrelated chat.
    candidate_limit = max(limit * 10, 200)
    if after:
        candidates = db.execute(
            query.order_by(EventRecord.timestamp.asc(), EventRecord.id.asc()).limit(candidate_limit)
        ).scalars().all()
    else:
        # First attach should inspect the current tail of the session, not the
        # oldest historical messages. Otherwise long-lived mixed rooms can hide
        # fresh actionable messages behind years of irrelevant transcript.
        candidates = list(reversed(db.execute(
            query.order_by(EventRecord.timestamp.desc(), EventRecord.id.desc()).limit(candidate_limit)
        ).scalars().all()))
    all_actionable = [row for row in candidates if _is_actionable_for_agent(row, agent_name)]
    actionable = (all_actionable[:limit] if after else all_actionable[-limit:])
    newest_id = (actionable[-1].id if after and len(all_actionable) > limit else (candidates[-1].id if candidates else after))
    return success_response({
        "workspace_id": str(workspace.id),
        "channel": channel,
        "agent_name": agent_name,
        "events": [_event_dict(row) for row in actionable],
        "has_more": bool(after and len(all_actionable) > limit),
        # Cursor advances over the inspected candidate window, not just
        # returned actionable events. That prevents adapters from rescanning
        # the same unrelated room chatter forever.
        "newest_id": newest_id,
    })


# ---------------------------------------------------------------------------
# GET /v1/events/conversations — discover agent-to-agent DM conversations
# ---------------------------------------------------------------------------

@router.get("/events/conversations")
async def list_conversations(
    network: str = Query(..., description="Network (workspace) ID or slug"),
    agent: Optional[str] = Query(None, description="Filter to conversations involving this agent"),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    x_workspace_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """
    List active agent-to-agent DM conversations.

    Returns distinct conversation pairs with their latest message,
    ordered by most recent activity.
    """
    workspace = db.execute(
        select(Workspace).where(_workspace_filter(network))
    ).scalar_one_or_none()

    if not workspace:
        return json_response(ResponseCode.NOT_FOUND, "Network not found")

    if not _verify_workspace_access(workspace, x_workspace_token, authorization):
        return json_response(ResponseCode.UNAUTHORIZED, "Invalid workspace credentials")

    # Build a subquery to find the latest event per conversation pair.
    # Normalize pairs so (A→B) and (B→A) are the same conversation.
    # Use case() instead of func.least/greatest for SQLite compatibility.
    lesser = case(
        (EventRecord.source <= EventRecord.target, EventRecord.source),
        else_=EventRecord.target,
    )
    greater = case(
        (EventRecord.source > EventRecord.target, EventRecord.source),
        else_=EventRecord.target,
    )

    base = (
        select(
            lesser.label("agent_a"),
            greater.label("agent_b"),
            func.max(EventRecord.timestamp).label("last_ts"),
            func.count().label("msg_count"),
        )
        .where(
            EventRecord.network_id == workspace.id,
            EventRecord.visibility == "direct",
            # Exclude channel targets — those are not DMs
            ~EventRecord.target.startswith("channel/"),
        )
    )

    if agent:
        base = base.where(
            or_(EventRecord.source == agent, EventRecord.target == agent)
        )

    base = base.group_by("agent_a", "agent_b").order_by(func.max(EventRecord.timestamp).desc()).limit(limit)

    pairs = db.execute(base).all()

    # For each pair, fetch the actual latest event
    conversations = []
    for row in pairs:
        latest_event = db.execute(
            select(EventRecord)
            .where(
                EventRecord.network_id == workspace.id,
                EventRecord.timestamp == row.last_ts,
                or_(
                    and_(EventRecord.source == row.agent_a, EventRecord.target == row.agent_b),
                    and_(EventRecord.source == row.agent_b, EventRecord.target == row.agent_a),
                ),
            )
            .limit(1)
        ).scalar_one_or_none()

        if latest_event:
            payload = latest_event.payload or {}
            conversations.append({
                "agents": [row.agent_a, row.agent_b],
                "last_message": {
                    "content": payload.get("content", ""),
                    "sender": latest_event.source,
                    "timestamp": latest_event.timestamp,
                },
                "message_count": row.msg_count,
            })

    return success_response({"conversations": conversations})


# ---------------------------------------------------------------------------
# GET /v1/events/latest-per-channel — bulk thread preview endpoint
# ---------------------------------------------------------------------------

@router.get("/events/latest-per-channel")
async def latest_per_channel(
    network: str = Query(..., description="Network (workspace) ID or slug"),
    type: Optional[str] = Query("workspace.message", description="Event type prefix to filter"),
    db: Session = Depends(get_db),
    x_workspace_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """
    Return the most recent event per channel in a single query.

    Replaces N separate pollEvents calls for thread list previews.
    Uses a SQL window function to efficiently pick the latest event per target.
    """
    workspace = db.execute(
        select(Workspace).where(_workspace_filter(network))
    ).scalar_one_or_none()

    if not workspace:
        return json_response(ResponseCode.NOT_FOUND, "Network not found")

    if not _verify_workspace_access(workspace, x_workspace_token, authorization):
        return json_response(ResponseCode.UNAUTHORIZED, "Invalid workspace credentials")

    # Window function: ROW_NUMBER() OVER (PARTITION BY target ORDER BY timestamp DESC)
    row_num = func.row_number().over(
        partition_by=EventRecord.target,
        order_by=EventRecord.timestamp.desc(),
    ).label("rn")

    inner = (
        select(EventRecord, row_num)
        .where(
            EventRecord.network_id == workspace.id,
            EventRecord.target.startswith("channel/"),
        )
    )

    if type:
        inner = inner.where(EventRecord.type.startswith(type))

    inner = inner.subquery()

    # Select only the first row per partition
    query = select(inner).where(inner.c.rn == 1)
    rows = db.execute(query).all()

    channels = {}
    for row in rows:
        channel_name = row.target.replace("channel/", "", 1)
        channels[channel_name] = {
            "id": row.id,
            "type": row.type,
            "source": row.source,
            "target": row.target,
            "payload": row.payload,
            "metadata": row.metadata,
            "timestamp": row.timestamp,
            "visibility": row.visibility,
        }

    return success_response({"channels": channels})
