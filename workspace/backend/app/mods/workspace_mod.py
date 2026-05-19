# -*- coding: utf-8 -*-
"""
mod/workspace — session routing, presence tracking, delegation.

Transform mod (priority 50). Handles workspace-specific event processing:
- Agent join/leave/ping → update WorkspaceMember
- Channel create/join/leave → manage Channel + ChannelMember rows
- Message posted by human → route to channel master
- Message posted by agent → LLM router decides next speaker or stop

Expects context.extra to contain:
  - db: SQLAlchemy Session
  - workspace: Workspace ORM object
"""

import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy import select

from openagents.core.onm_events import Event, WorkspaceEventTypes
from openagents.core.onm_mods import EventRejected, PipelineContext, TransformMod
from app.models import EventRecord

logger = logging.getLogger(__name__)

# Lazy-initialized LLM client for the router
_llm_client = None
_llm_provider = None


class WorkspaceMod(TransformMod):
    """Workspace-specific event processing."""
    name = "workspace"
    intercepts: List[str] = []  # Match all events — we dispatch internally
    priority = 50

    async def process(self, event: Event, context: PipelineContext) -> Optional[Event]:
        handler = _HANDLERS.get(event.type)
        if handler:
            return await handler(event, context)
        # Pass through unhandled event types unchanged
        return event


# ---------------------------------------------------------------------------
# Per-type handlers
# ---------------------------------------------------------------------------

async def _handle_agent_join(event: Event, ctx: PipelineContext) -> Optional[Event]:
    """network.agent.join → upsert WorkspaceMember, set online, rotate session.

    Agent Bridge rooms are intentionally simple chat rooms: when a new
    agent joins the room, it should be able to see and talk in the active
    room chats without the human manually adding it to each channel.
    """
    import uuid as _uuid
    from app.models import Channel, ChannelMember, WorkspaceMember

    db = ctx.extra["db"]
    workspace = ctx.extra["workspace"]
    agent_name = event.payload.get("agent_name") if event.payload else None
    if not agent_name:
        logger.warning("workspace_mod: agent.join missing agent_name in payload")
        return None

    existing = db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.agent_name == agent_name,
        )
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)

    agent_type = event.payload.get("agent_type") if event.payload else None
    server_host = event.payload.get("server_host") if event.payload else None
    working_dir = event.payload.get("working_dir") if event.payload else None

    # Rotate session on every join. Any prior client holding the old
    # session_id (ghost adapter, duplicate daemon) gets rejected when it
    # next heartbeats or posts, which tells it to stop.
    new_session_id = _uuid.uuid4().hex

    if existing:
        prior_session = existing.session_id
        existing.status = "online"
        existing.last_heartbeat = now
        existing.session_id = new_session_id
        existing.session_started_at = now
        if agent_type and not existing.agent_type:
            existing.agent_type = agent_type
        if server_host:
            existing.server_host = server_host
        if working_dir:
            existing.working_dir = working_dir
        if prior_session and prior_session != new_session_id:
            logger.info(
                "workspace_mod: rotated session for %s in %s (prior session revoked)",
                agent_name, workspace.id,
            )
    else:
        role = event.payload.get("role", "member")
        member = WorkspaceMember(
            workspace_id=workspace.id,
            agent_name=agent_name,
            role=role,
            agent_type=agent_type,
            server_host=server_host,
            working_dir=working_dir,
            status="online",
            last_heartbeat=now,
            session_id=new_session_id,
            session_started_at=now,
        )
        db.add(member)

    workspace.last_activity_at = now
    db.flush()

    # Simple-room behavior: every joined agent can participate in every
    # active non-routine channel. This keeps the room mental model simple:
    # join the room → read/send in the room chatbox.
    active_channels = db.execute(
        select(Channel).where(
            Channel.workspace_id == workspace.id,
            Channel.status == "active",
        )
    ).scalars().all()
    for channel in active_channels:
        if channel.name.startswith("routines:"):
            continue
        already_in_channel = db.execute(
            select(ChannelMember).where(
                ChannelMember.channel_id == channel.id,
                ChannelMember.agent_name == agent_name,
            )
        ).scalar_one_or_none()
        if not already_in_channel:
            db.add(ChannelMember(channel_id=channel.id, agent_name=agent_name))
    db.flush()

    # Enrich event metadata with resolved info + session_id so the
    # router returns it to the joining client.
    event.metadata["role"] = existing.role if existing else event.payload.get("role", "member")
    event.metadata["network_id"] = str(workspace.id)
    event.metadata["session_id"] = new_session_id
    return event


def _validate_session(db, workspace_id, agent_name: str, claimed_session: Optional[str]) -> Optional[str]:
    """Check that claimed_session matches the current session for this agent.

    Returns None if valid or legacy (nothing to enforce), else an error code
    string ("session_revoked" | "session_missing") that callers can surface.

    Semantics:
      - stored=None      → legacy member, accept anything (transition)
      - stored=X, claim=None → legacy client, accept (transition)
      - stored=X, claim=X → valid
      - stored=X, claim=Y → revoked: another client joined as this agent
    """
    from app.models import WorkspaceMember

    member = db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.agent_name == agent_name,
        )
    ).scalar_one_or_none()
    if not member or not member.session_id:
        return None  # legacy or not-yet-joined
    if not claimed_session:
        return None  # legacy client that hasn't learned session_id yet
    if claimed_session != member.session_id:
        return "session_revoked"
    return None


async def _handle_agent_leave(event: Event, ctx: PipelineContext) -> Optional[Event]:
    """network.agent.leave → set member offline."""
    from app.models import WorkspaceMember

    db = ctx.extra["db"]
    workspace = ctx.extra["workspace"]
    agent_name = event.payload.get("agent_name") if event.payload else None
    if not agent_name:
        return None

    member = db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.agent_name == agent_name,
        )
    ).scalar_one_or_none()

    if not member:
        return None

    member.status = "offline"
    db.flush()
    return event


async def _handle_agent_remove(event: Event, ctx: PipelineContext) -> Optional[Event]:
    """network.agent.remove → delete WorkspaceMember, reassign master if needed."""
    from app.models import Channel, WorkspaceMember

    db = ctx.extra["db"]
    workspace = ctx.extra["workspace"]
    agent_name = event.payload.get("agent_name") if event.payload else None
    if not agent_name:
        logger.warning("workspace_mod: agent.remove missing agent_name in payload")
        return None

    member = db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.agent_name == agent_name,
        )
    ).scalar_one_or_none()

    if not member:
        return None

    was_master = member.role == "master"
    db.delete(member)
    db.flush()

    new_master_name = None

    # If removed agent was master, promote the next available agent
    if was_master:
        next_master = db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace.id,
            ).order_by(WorkspaceMember.joined_at.asc())
        ).scalar_one_or_none()

        if next_master:
            next_master.role = "master"
            new_master_name = next_master.agent_name
            db.flush()

    # Reassign channel masters: any channel where removed agent was master
    channels = db.execute(
        select(Channel).where(
            Channel.workspace_id == workspace.id,
            Channel.master_agent == agent_name,
        )
    ).scalars().all()

    for ch in channels:
        ch.master_agent = new_master_name
    db.flush()

    event.metadata["removed_agent"] = agent_name
    if new_master_name:
        event.metadata["new_master"] = new_master_name
    return event


async def _handle_ping(event: Event, ctx: PipelineContext) -> Optional[Event]:
    """network.ping → update heartbeat timestamp.

    Validates session_id if the client sent one. A mismatch means a newer
    client has joined as this agent; we drop this heartbeat and mark the
    event metadata so the caller can surface session_revoked to the
    stale client, which will then stop.
    """
    from app.models import WorkspaceMember

    db = ctx.extra["db"]
    workspace = ctx.extra["workspace"]
    agent_name = event.payload.get("agent_name") if event.payload else None
    if not agent_name:
        return None

    claimed_session = (event.payload or {}).get("session_id")
    err = _validate_session(db, workspace.id, agent_name, claimed_session)
    if err == "session_revoked":
        event.metadata["session_error"] = err
        logger.info(
            "workspace_mod: rejected heartbeat for %s in %s (stale session_id)",
            agent_name, workspace.id,
        )
        return event

    member = db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.agent_name == agent_name,
        )
    ).scalar_one_or_none()

    if not member:
        return None

    now = datetime.now(timezone.utc)
    member.status = "online"
    member.last_heartbeat = now
    db.flush()
    return event


async def _handle_channel_create(event: Event, ctx: PipelineContext) -> Optional[Event]:
    """network.channel.create → create Channel + initial ChannelMember rows."""
    from app.models import Channel, ChannelMember, WorkspaceMember

    db = ctx.extra["db"]
    workspace = ctx.extra["workspace"]
    payload = event.payload or {}

    channel_name = payload.get("name", f"channel-{event.id[:8]}")
    channel = Channel(
        workspace_id=workspace.id,
        name=channel_name,
        title=payload.get("title"),
        created_by=event.source,
        master_agent=payload.get("master"),
        resume_from=payload.get("resume_from"),
        status="active",
    )
    db.add(channel)
    db.flush()  # get channel.id

    # Add initial participants. In simple Agent Bridge rooms, new chat
    # channels include all current room agents so every side can see/read
    # and send in the same chatbox. Routine channels stay isolated.
    participants = set(payload.get("participants", []))
    if not channel_name.startswith("routines:"):
        room_agents = db.execute(
            select(WorkspaceMember.agent_name).where(
                WorkspaceMember.workspace_id == workspace.id,
            )
        ).scalars().all()
        participants.update(room_agents)

    for agent_name in participants:
        db.add(ChannelMember(channel_id=channel.id, agent_name=agent_name))

    db.flush()

    # Enrich event with created channel info
    event.metadata["channel_id"] = str(channel.id)
    event.metadata["channel_name"] = channel.name
    event.target = f"channel/{channel.name}"
    return event


def _is_channel_admin(event_source: str, channel, agent_name: str) -> bool:
    """Who is allowed to manage channel membership.

    Three sources are accepted:
      • a human user (`human:<name>`) — workspace clients act on behalf
        of the logged-in human
      • the channel's master agent (`openagents:<master>`) — owner can
        manage their own thread
      • the agent being added/removed itself (`openagents:<agent_name>`) —
        agents can join channels they've been invited to and leave on
        their own initiative
    """
    src = event_source or ""
    if src.startswith("human:"):
        return True
    if channel.master_agent and src == f"openagents:{channel.master_agent}":
        return True
    if src == f"openagents:{agent_name}":
        return True
    return False


async def _handle_channel_join(event: Event, ctx: PipelineContext) -> Optional[Event]:
    """network.channel.join → add ChannelMember.

    Routine channels (`routines:<agent>`) are locked single-agent queues —
    we raise EventRejected so clients can roll back any optimistic UI.
    The owner is added in-line when the channel is first created (see
    app/routers/routines.py).
    """
    from app.models import Channel, ChannelMember

    db = ctx.extra["db"]
    workspace = ctx.extra["workspace"]
    payload = event.payload or {}
    channel_name = payload.get("channel")
    agent_name = payload.get("agent_name")
    if not channel_name or not agent_name:
        return None

    if channel_name.startswith("routines:"):
        raise EventRejected(
            "workspace_mod",
            "routine_channel_locked: membership of routines:* is managed by the system",
        )

    channel = db.execute(
        select(Channel).where(
            Channel.workspace_id == workspace.id,
            Channel.name == channel_name,
        )
    ).scalar_one_or_none()
    if not channel:
        raise EventRejected("workspace_mod", "channel_not_found")

    if not _is_channel_admin(event.source or "", channel, agent_name):
        raise EventRejected(
            "workspace_mod",
            "channel_join_forbidden: only humans, the channel master, or "
            "the agent being added may invite",
        )

    # Check if already a member
    existing = db.execute(
        select(ChannelMember).where(
            ChannelMember.channel_id == channel.id,
            ChannelMember.agent_name == agent_name,
        )
    ).scalar_one_or_none()

    if not existing:
        db.add(ChannelMember(channel_id=channel.id, agent_name=agent_name))
        # Auto-promote first agent to channel master if none set
        if not channel.master_agent:
            channel.master_agent = agent_name
        db.flush()

    return event


async def _handle_channel_leave(event: Event, ctx: PipelineContext) -> Optional[Event]:
    """network.channel.leave → remove ChannelMember.

    Routine channels are locked — removing the owner is rejected so the
    queue keeps its single-agent invariant. Returns EventRejected so
    clients can roll back optimistic UI.
    """
    from app.models import Channel, ChannelMember

    db = ctx.extra["db"]
    workspace = ctx.extra["workspace"]
    payload = event.payload or {}
    channel_name = payload.get("channel")
    agent_name = payload.get("agent_name")
    if not channel_name or not agent_name:
        return None

    if channel_name.startswith("routines:"):
        raise EventRejected(
            "workspace_mod",
            "routine_channel_locked: membership of routines:* is managed by the system",
        )

    channel = db.execute(
        select(Channel).where(
            Channel.workspace_id == workspace.id,
            Channel.name == channel_name,
        )
    ).scalar_one_or_none()
    if not channel:
        raise EventRejected("workspace_mod", "channel_not_found")

    if not _is_channel_admin(event.source or "", channel, agent_name):
        raise EventRejected(
            "workspace_mod",
            "channel_leave_forbidden: only humans, the channel master, or "
            "the agent being removed may leave",
        )

    member = db.execute(
        select(ChannelMember).where(
            ChannelMember.channel_id == channel.id,
            ChannelMember.agent_name == agent_name,
        )
    ).scalar_one_or_none()

    if member:
        db.delete(member)
        db.flush()

    return event


def _agent_alias_map(members) -> Dict[str, str]:
    """Return lowercase aliases/display-names → canonical agent_name."""
    aliases: Dict[str, str] = {}
    for member in members or []:
        agent_name = getattr(member, "agent_name", None)
        if not agent_name:
            continue
        aliases[agent_name.lower()] = agent_name
        display_name = getattr(member, "display_name", None)
        if display_name:
            aliases[display_name.lower()] = agent_name
    return aliases


def _extract_mentions(content: str, agent_aliases: Dict[str, str]) -> List[str]:
    """Parse @agent-name mentions from message text, validated against joined agents.

    Agent ids may include dots (e.g. ``mr.robot``), so the mention parser
    deliberately accepts ``.`` as well as word chars and hyphens.
    """
    if not content or not agent_aliases:
        return []
    raw_mentions = re.findall(r"@([\w.-]+)", content)
    targets: List[str] = []
    for mention in raw_mentions:
        target = agent_aliases.get(mention.lower())
        if target and target not in targets:
            targets.append(target)
    return targets


def _extract_leading_mention(content: str, known_agents: List[str]) -> Optional[str]:
    """Return the agent name if the message starts with @agent-name, else None."""
    if not content or not known_agents:
        return None
    m = re.match(r"^\s*@([\w-]+)", content)
    if m and m.group(1) in set(known_agents):
        return m.group(1)
    return None


def _extract_direct_address(content: str, agent_aliases: Dict[str, str]) -> Optional[str]:
    """Return agent if message begins by addressing their name.

    Chat sessions should not require @mentions for obvious turn-taking, so
    support forms like `Amin, ...`, `Amin: ...`, and `Amin are you here?`.
    """
    if not content or not agent_aliases:
        return None
    # Prefer longer aliases first so `mr.robot` wins before `mr` if both exist.
    for alias, agent_name in sorted(agent_aliases.items(), key=lambda item: len(item[0]), reverse=True):
        if re.match(rf"^\s*{re.escape(alias)}(?:\s*[:,\-—]|\s+)", content, re.I):
            return agent_name
    return None


def _extract_named_agent_references(content: str, agent_aliases: Dict[str, str]) -> List[str]:
    """Return agents whose names/display names appear as standalone text.

    This is used only as a multi-name signal. For example,
    `Amin and mr.robot, talk here` should target both joined agents even
    without @mentions.
    """
    if not content or not agent_aliases:
        return []
    targets: List[str] = []
    for alias, agent_name in sorted(agent_aliases.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"(?<![\w.-]){re.escape(alias)}(?![\w.-])", content, re.I):
            if agent_name not in targets:
                targets.append(agent_name)
    return targets


def _fallback_targets(event, channel, mentions: List[str]) -> List[str]:
    """Determine target agents when LLM router is unavailable.

    Priority: explicit @mentions → master (for human/member msgs) → all participants.
    """
    if mentions:
        return mentions
    participants = [p.agent_name for p in (channel.participants or [])]
    if channel.master_agent and channel.master_agent in participants:
        if event.source.startswith("openagents:"):
            sender = event.source[len("openagents:"):]
            # Master's own messages: no self-trigger
            if sender == channel.master_agent:
                return []
        return [channel.master_agent]
    # No valid master — target the first actual joined participant. This avoids
    # stale defaults (for example old `openclaw-main`) that are no longer in the
    # session membership.
    return [participants[0]] if participants else []


def _looks_like_group_chat_request(content: str) -> bool:
    """Detect simple-room requests addressed to multiple agents.

    Agent Bridge rooms should feel like one shared chatbox. If the human
    says “guys”, “both”, “all”, “talk/chat”, etc., route the turn to every
    participant instead of only the master.
    """
    text = (content or "").lower()
    phrases = (
        "guys",
        "both",
        "all of you",
        "everyone",
        "you two",
        "talk",
        "chat",
        "speak",
        "each other",
        "start talking",
        "start chatting",
    )
    return any(phrase in text for phrase in phrases)


def _all_channel_agents(channel) -> List[str]:
    """Return all agent participants for simple-room broadcast turns."""
    return [p.agent_name for p in (channel.participants or []) if p.agent_name]


_ROUTER_PROMPT = """\
You are a conversation router for a multi-agent workspace. Decide which \
agent should respond next to the LATEST message. Use judgment — read the \
message carefully and think about who is actually being addressed.

Channel participants:
{participants}
Master agent: {master}

Recent conversation (oldest → newest):
{history}

LATEST message from {sender}:
{content}

HOW TO DECIDE:

A. Identify who (if anyone) is being directly addressed.
   Treat @agent-name as ADDRESSING that agent only when the agent is the \
subject being asked to do/say something. If the agent is merely referenced \
("check @Alice's note, Bob" — Alice is referred to, Bob is addressed), \
pick the addressed agent, not the mentioned one.

B. If the LATEST message is from a HUMAN:
   - Always pick exactly one agent. Humans expect a reply — never output \
"stop" for a human message.
   - Prefer whoever is directly addressed.
   - If nobody is directly addressed, check CONVERSATIONAL CONTINUITY: \
if the user was just conversing with a specific agent (the last agent \
reply was from agent X, or X asked the user a question that this message \
appears to answer), continue with that agent X.
   - Otherwise pick the agent whose role/description best fits the topic; \
fall back to the master agent.

C. If the LATEST message is from an AGENT:
   - If it delegates or hands off to another agent ("@Alice please do X", \
"Alice, could you check X"), route to that agent.
   - If it reports back to the master or asks the master to decide, route to the master.
   - If it is a FINAL answer to the previous human question or an \
acknowledgement ("done", "saved", "sounds good"), output "stop".
   - Never route back to the same agent that just spoke (no self-loops).
   - When unsure, prefer "stop" to avoid infinite agent-to-agent loops.

EXAMPLES:
  Human: "@alice what's the status?"                → next:alice
  Human: "check @alice's notes, @bob"                → next:bob       (bob is addressed)
  Human: "how about julia?"  (julia is not an agent) → next:<master>  (who owns that topic)
  Agent alice: "@bob can you verify?"                → next:bob
  Agent alice: "Done — results attached."            → stop
  Agent bob (master): "Here's the final answer ..."  → stop

  Conversational continuity examples:
    alice: "I'm here. What do you need?"
    Human: "do you know about X?"                    → next:alice     (continuing with alice)

    alice: "I pulled these results: [...]."
    Human: "thanks, can you also check Y?"           → next:alice     (follow-up to alice)

Output EXACTLY one line, lowercase, no punctuation or explanation:
  next:<agent_name>
  stop"""


def _get_router_api_key() -> str:
    """Resolve the API key: ROUTER_LLM_API_KEY takes priority, then ANTHROPIC_API_KEY."""
    from app.config import config
    return config.ROUTER_LLM_API_KEY or config.ANTHROPIC_API_KEY


def _get_router_model() -> str:
    """Resolve the model: explicit config or provider default."""
    from app.config import config
    if config.ROUTER_LLM_MODEL:
        return config.ROUTER_LLM_MODEL
    if config.ROUTER_LLM_PROVIDER == "openai":
        return "gpt-4o-mini"
    return "claude-haiku-4-5-20251001"


def _get_llm_client():
    """Lazy-init the LLM client based on provider config."""
    global _llm_client, _llm_provider
    from app.config import config

    provider = config.ROUTER_LLM_PROVIDER
    if _llm_client is not None and _llm_provider == provider:
        return _llm_client, provider

    api_key = _get_router_api_key()

    if provider == "openai":
        from openai import OpenAI
        kwargs = {"api_key": api_key}
        if config.ROUTER_LLM_BASE_URL:
            kwargs["base_url"] = config.ROUTER_LLM_BASE_URL
        _llm_client = OpenAI(**kwargs)
    else:
        import anthropic
        _llm_client = anthropic.Anthropic(api_key=api_key)

    _llm_provider = provider
    return _llm_client, provider


async def _route_with_llm(channel, new_event: Event, db, workspace) -> List[str]:
    """Use a small LLM to decide which agent(s) should respond next.

    Returns a list of agent names to target, or an empty list (stop).
    Falls back to empty list on any error.
    """
    from app.config import config
    from app.models import EventRecord

    if not _get_router_api_key():
        logger.warning("LLM router: no API key set (ROUTER_LLM_API_KEY or ANTHROPIC_API_KEY), defaulting to stop")
        return []

    # Fetch last 5 chat messages from this channel
    channel_target = f"channel/{channel.name}"
    recent = db.execute(
        select(EventRecord)
        .where(
            EventRecord.network_id == workspace.id,
            EventRecord.target == channel_target,
            EventRecord.type == "workspace.message.posted",
        )
        .order_by(EventRecord.timestamp.desc())
        .limit(5)
    ).scalars().all()

    # Build conversation history (oldest first)
    recent.reverse()
    history_lines = []
    for evt in recent:
        payload = evt.payload or {}
        msg_type = payload.get("message_type", "chat")
        if msg_type in ("thinking", "status"):
            continue
        source = evt.source
        if source.startswith("human:"):
            label = "human"
        elif source.startswith("openagents:"):
            label = source[len("openagents:"):]
        else:
            label = source
        text = (payload.get("content") or "")[:500]  # Truncate long messages
        history_lines.append(f"[{label}] {text}")

    history = "\n".join(history_lines) if history_lines else "(no prior messages)"

    # Participant list with role/description for better routing
    from app.models import WorkspaceMember
    participant_names = [p.agent_name for p in (channel.participants or [])]
    members = {
        m.agent_name: m for m in db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace.id,
                WorkspaceMember.agent_name.in_(participant_names),
            )
        ).scalars().all()
    }
    participant_lines = []
    for name in participant_names:
        m = members.get(name)
        role = m.role if m else "member"
        desc = m.description if m and m.description else ""
        line = f"  - {name} (role: {role})"
        if desc:
            line += f" — {desc}"
        participant_lines.append(line)
    participants_str = "\n".join(participant_lines) if participant_lines else "  (none)"

    master = channel.master_agent or "(none)"
    sender = new_event.source
    if sender.startswith("openagents:"):
        sender = sender[len("openagents:"):]

    content = (new_event.payload or {}).get("content", "")[:500]

    prompt = _ROUTER_PROMPT.format(
        participants=participants_str,
        master=master,
        history=history,
        sender=sender,
        content=content,
    )

    try:
        client, provider = _get_llm_client()
        model = _get_router_model()

        # Synchronous LLM call — fast (~500ms) router decision
        if provider == "openai":
            response = client.chat.completions.create(
                model=model,
                max_tokens=30,
                messages=[{"role": "user", "content": prompt}],
            )
            raw_result = response.choices[0].message.content.strip()
        else:
            response = client.messages.create(
                model=model,
                max_tokens=30,
                messages=[{"role": "user", "content": prompt}],
            )
            raw_result = response.content[0].text.strip()

        # Case-insensitive keyword detection but preserve original case
        # of the agent name so we can match it against participants
        # (agent names are case-sensitive in the workspace).
        result = raw_result.lower()

        logger.info("LLM router decision: %s (channel=%s, sender=%s, provider=%s)", raw_result, channel.name, sender, provider)

        if result.startswith("next:"):
            # Preserve the original case from the model output so we can
            # match against participant names, which ARE case-sensitive.
            agent_name = raw_result[len("next:"):].strip().split(",")[0].strip()
            # Case-insensitive participant lookup, then canonicalize to
            # the stored case.
            participants_by_lower = {
                p.agent_name.lower(): p.agent_name
                for p in (channel.participants or [])
            }
            canonical = participants_by_lower.get(agent_name.lower())
            if canonical is None:
                logger.warning(
                    "LLM router returned unknown agent: %r (valid: %s)",
                    agent_name, list(participants_by_lower.values()),
                )
                # For human senders, fall through to the safety net below
                # so the user always gets a reply.
                if not (new_event.source or "").startswith("human:"):
                    return []
                agent_name = None
            else:
                agent_name = canonical
                # Reject self-loops — router sometimes picks the agent
                # who just spoke. Sender's adapter skips own messages but
                # legacy clients would still see the target and retry.
                if (new_event.source or "").startswith("openagents:"):
                    sender = new_event.source[len("openagents:"):]
                    if agent_name == sender:
                        logger.info("LLM router self-loop rejected: %s", sender)
                        return []
                return [agent_name]
        else:
            agent_name = None  # "stop" or unrecognized

        # Safety net: humans ALWAYS get a response. If the router said
        # "stop" (or returned an invalid agent) for a human message,
        # fall back to the master/fallback target. Without this, the
        # router can silently drop a legitimate follow-up question like
        # "how about Julia?" after a previous "final answer" message.
        if (new_event.source or "").startswith("human:"):
            fallback = _fallback_targets(new_event, channel, [])
            if fallback:
                logger.info(
                    "LLM router returned stop/invalid for human message — "
                    "routing to fallback %s instead", fallback,
                )
                return fallback
        return []

    except Exception as e:
        logger.error("LLM router failed, defaulting to fallback: %s", e)
        # Same safety net on exception: humans still get a reply.
        if (new_event.source or "").startswith("human:"):
            try:
                fallback = _fallback_targets(new_event, channel, [])
                if fallback:
                    return fallback
            except Exception:
                pass
        return []


_DEFAULT_TITLES = {"New Thread", "Session 1", None, ""}


def _auto_title_channel(channel, content: str, db) -> None:
    """Set channel title from message content if still using a default title."""
    if channel.title not in _DEFAULT_TITLES:
        return
    if not content or not content.strip():
        return
    # Use first line, truncated to 60 chars
    first_line = content.strip().split("\n")[0]
    title = first_line[:60].rstrip()
    if len(first_line) > 60:
        title += "..."
    channel.title = title
    db.flush()


def _reply_to_id(value) -> Optional[str]:
    """Accept ClawDeck-style reply objects or a plain event id."""
    if not value:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("id", "event_id", "msgId", "requestId"):
            candidate = value.get(key)
            if candidate:
                return str(candidate).strip() or None
    return None


def _infer_agent_reply_to(event: Event, db, workspace, channel) -> Optional[str]:
    """Find the latest session event this agent is plausibly answering.

    New adapters should send reply_to explicitly. For legacy/current adapters
    that do not yet know about Agent Bridge's anchored-reply rule, infer the
    anchor from the latest channel event that targeted this agent (or, as a
    final fallback, the latest non-self event). This keeps the guarantee that
    persisted agent chat messages are replies without breaking live agents.
    """
    from app.models import EventRecord

    sender = event.source[len("openagents:"):] if event.source.startswith("openagents:") else None
    target = f"channel/{channel.name}"
    rows = db.execute(
        select(EventRecord)
        .where(EventRecord.network_id == workspace.id)
        .where(EventRecord.target == target)
        .where(EventRecord.id != getattr(event, "id", None))
        .order_by(EventRecord.timestamp.desc(), EventRecord.id.desc())
        .limit(50)
    ).scalars().all()

    if sender:
        for row in rows:
            metadata = row.metadata_ or {}
            targets = metadata.get("target_agents") or []
            if sender in targets and row.source != event.source:
                return row.id

    for row in rows:
        if row.source != event.source and row.type.startswith("workspace."):
            return row.id
    return None


def _normalize_for_duplicate_check(content: str) -> str:
    return re.sub(r"\s+", " ", (content or "").strip()).lower()


def _reject_repeated_agent_chat(event: Event, db, workspace) -> None:
    """Block exact repeated long agent chat messages in the same session.

    This is a safety rail for buggy adapters/watchers. A real agent may make
    the same point twice, but posting the exact same long response repeatedly
    is almost always a loop/replay bug and makes the shared session unusable.
    """
    if not event.source.startswith("openagents:"):
        return
    payload = event.payload or {}
    if payload.get("message_type", "chat") != "chat":
        return
    content = str(payload.get("content") or "")
    normalized = _normalize_for_duplicate_check(content)
    if len(normalized) < 80:
        return

    recent = db.execute(
        select(EventRecord)
        .where(
            EventRecord.network_id == workspace.id,
            EventRecord.type == "workspace.message.posted",
            EventRecord.source == event.source,
            EventRecord.target == event.target,
        )
        .order_by(EventRecord.timestamp.desc())
        .limit(50)
    ).scalars().all()

    for previous in recent:
        previous_payload = previous.payload or {}
        if previous_payload.get("message_type", "chat") != "chat":
            continue
        previous_reply_to = _reply_to_id(previous_payload.get("reply_to")) \
            or _reply_to_id((previous.metadata_ or {}).get("reply_to"))
        current_reply_to = _reply_to_id(payload.get("reply_to")) \
            or _reply_to_id((event.metadata or {}).get("reply_to"))
        # Repeating the exact same long answer to the exact same source
        # message is a replay/loop bug. The same text anchored to a different
        # source message is allowed: short smoke prompts and deterministic
        # agent reviews can legitimately produce identical final text.
        if previous_reply_to != current_reply_to:
            continue
        if _normalize_for_duplicate_check(str(previous_payload.get("content") or "")) == normalized:
            raise EventRejected("workspace_mod", "duplicate_agent_message: repeated agent chat blocked")


def _normalize_reply_to(event: Event, db, workspace, channel) -> dict:
    """Validate and normalize reply metadata for session messages.

    Agent Bridge follows the ClawDeck shape: `payload.reply_to` is a small
    quote object (`id`, `type`, `sender`, `text`) and `metadata.reply_to`
    keeps the canonical event id. Agents must supply this so every agent
    message is anchored to the session message it answers. Humans may supply
    it, but are allowed to send top-level messages.
    """
    payload = event.payload or {}
    reply_id = _reply_to_id(payload.get("reply_to")) or _reply_to_id(payload.get("replyTo")) \
        or _reply_to_id((event.metadata or {}).get("reply_to")) or _reply_to_id((event.metadata or {}).get("replyTo"))
    if not reply_id:
        reply_id = _infer_agent_reply_to(event, db, workspace, channel)
    if not reply_id:
        raise EventRejected("workspace_mod", "reply_required: agent messages must include reply_to")

    from app.models import EventRecord

    original = db.execute(
        select(EventRecord).where(
            EventRecord.network_id == workspace.id,
            EventRecord.id == reply_id,
        )
    ).scalar_one_or_none()
    if not original:
        raise EventRejected("workspace_mod", "reply_not_found: reply_to message was not found")
    if original.target != event.target or original.target != f"channel/{channel.name}":
        raise EventRejected("workspace_mod", "reply_wrong_channel: reply_to must be in this session channel")

    original_payload = original.payload or {}
    text = str(
        original_payload.get("content")
        or original_payload.get("filename")
        or original_payload.get("message")
        or ""
    )
    quote = {
        "id": original.id,
        "type": original.type,
        "sender": original_payload.get("sender_name") or original.source,
        "text": text[:500],
    }
    event.payload = {**payload, "reply_to": quote}
    event.metadata = {**(event.metadata or {}), "reply_to": original.id}
    return quote


async def _handle_message_posted(event: Event, ctx: PipelineContext) -> Optional[Event]:
    """
    workspace.message.posted → route messages to the right agents.

    Routing rules (human messages):
    - Starts with @agent-name → route to that agent only
    - No leading @mention → channel master (or all participants if no master)

    Routing rules (agent messages in multi-agent threads):
    - LLM router (Haiku) evaluates the last few messages and decides:
      - "next:agent-name" → route to that agent
      - "stop" → no targeting, conversation rests until human speaks
    - Fallback (single-agent threads or router disabled): no routing needed.
    """
    from app.models import Channel, WorkspaceMember

    db = ctx.extra["db"]
    workspace = ctx.extra["workspace"]
    payload = event.payload or {}
    content = payload.get("content", "")
    message_type = payload.get("message_type", "chat")

    # Room-level freeze is a hard stop. When enabled, nobody — human or
    # agent — can add chat/thinking/status messages to the room until the
    # operator unfreezes it. This is intentionally enforced server-side so
    # every connected agent stops, not just the current browser UI.
    if (workspace.settings or {}).get("frozen"):
        raise EventRejected("workspace_mod", "room_frozen: chat is frozen")

    # Reject posts from stale agent sessions. If the sender is an agent
    # and its claimed session_id does not match the current one in
    # WorkspaceMember, drop the event and flag it so the router can
    # return session_revoked to the client.
    if event.source and event.source.startswith("openagents:"):
        sender = event.source[len("openagents:"):]
        claimed_session = event.metadata.get("session_id") if event.metadata else None
        err = _validate_session(db, workspace.id, sender, claimed_session)
        if err == "session_revoked":
            event.metadata["session_error"] = err
            logger.info(
                "workspace_mod: rejected message from %s in %s (stale session_id)",
                sender, workspace.id,
            )
            # Return the event with the error flag but no content changes;
            # the router checks session_error and returns an error response.
            return event

        # Message activity is presence activity. Some adapters can miss or
        # slow down heartbeat polls, but if an agent is actively talking in
        # the room it should not be shown as offline.
        member = db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace.id,
                WorkspaceMember.agent_name == sender,
            )
        ).scalar_one_or_none()
        if member:
            member.status = "online"
            member.last_heartbeat = datetime.now(timezone.utc)
            db.flush()

    # "thinking", "status", and "todos" messages are intermediate agent output
    # — they should NOT trigger other agents.
    if message_type in ("thinking", "status", "todos"):
        return event

    # Resolve channel (needed for both agent and human message routing)
    channel = None
    if event.target.startswith("channel/"):
        channel_name = event.target[len("channel/"):]
        channel = db.execute(
            select(Channel).where(
                Channel.workspace_id == workspace.id,
                Channel.name == channel_name,
            )
        ).scalar_one_or_none()

    # Auto-name channel from first human message if title is default/empty
    if event.source.startswith("human:") and channel:
        _auto_title_channel(channel, content, db)

    # Skip non-human, non-agent sources
    if not event.source.startswith("human:") and not event.source.startswith("openagents:"):
        return event

    if not channel:
        return event

    # Humans can send normal top-level messages. Agents must reply to a
    # concrete message/file event in the same session channel, so their
    # output is always anchored and auditable instead of floating in the room.
    if event.source.startswith("openagents:"):
        _normalize_reply_to(event, db, workspace, channel)
        _reject_repeated_agent_chat(event, db, workspace)

    # Parse direct agent addressing against the actual joined participants in
    # this channel, not stale workspace defaults. This is the key room/session
    # behavior: if Amin joined as `Amin`, messages like `Amin are you here?`
    # must target Amin, never an old master such as `openclaw-main`.
    participant_names = [p.agent_name for p in (channel.participants or []) if p.agent_name]
    participant_members = db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.agent_name.in_(participant_names),
        )
    ).scalars().all() if participant_names else []
    agent_aliases = _agent_alias_map(participant_members)
    mentions = _extract_mentions(content, agent_aliases)
    named_agents = _extract_named_agent_references(content, agent_aliases)
    direct_address = None if len(named_agents) >= 2 else _extract_direct_address(content, agent_aliases)
    if len(named_agents) >= 2:
        for agent_name in named_agents:
            if agent_name not in mentions:
                mentions.append(agent_name)
    elif direct_address and direct_address not in mentions:
        mentions.insert(0, direct_address)

    if event.source.startswith("human:") and mentions:
        targets = mentions
    elif event.source.startswith("human:") and _looks_like_group_chat_request(content):
        targets = _all_channel_agents(channel)
    # ── Multi-agent channel: use LLM/router unless human asked the group ──
    elif len(channel.participants or []) >= 2:
        from app.config import config
        if config.ROUTER_LLM_ENABLED and _get_router_api_key():
            targets = await _route_with_llm(channel, event, db, workspace)
        else:
            # LLM router not available — fallback to mention or master
            targets = _fallback_targets(event, channel, mentions)
    # ── Single-agent channel ────────────────────────────────────────
    else:
        targets = _fallback_targets(event, channel, mentions)

    # ALWAYS set target_agents, even when nobody should respond.
    #
    # Use a non-empty sentinel list ["__no_response__"] instead of []
    # because legacy clients (pre-0.2.106) check `!targets.length ||
    # targets.includes(agentName)` — an empty list is truthy-skipped
    # and falls through to broadcast, so every agent in the channel
    # replies at once. A non-empty list that contains no real agent
    # name causes old clients to reject (they fail the includes check)
    # and new clients to treat it as "nobody" (the sentinel is ignored).
    if event.source.startswith("openagents:"):
        sender_name = event.source[len("openagents:"):]
        targets = [agent_name for agent_name in targets if agent_name != sender_name]

    event.metadata["target_agents"] = targets if targets else ["__no_response__"]
    real_targets = [agent_name for agent_name in event.metadata["target_agents"] if agent_name != "__no_response__"]
    if event.source.startswith("openagents:") and real_targets:
        event.metadata["response_required"] = True
        event.metadata["required_responses"] = real_targets
        event.metadata.setdefault("handoff_state", "pending")

    # Auto-add targeted agents as channel participants so they can poll
    # for messages on this channel. Three guards:
    #   1. Never add the `__no_response__` sentinel — it's a routing
    #      signal, not a real agent.
    #   2. Only auto-add when the sender is a human. Agent→agent routing
    #      decisions (from the LLM router or master-fallback) used to
    #      drag bystander agents into channels they didn't belong in.
    #   3. Routine channels (`routines:<agent>`) are locked single-agent
    #      job queues — never add anyone but the owner.
    if event.source and event.source.startswith("human:") and \
            not channel.name.startswith("routines:"):
        from app.models import ChannelMember
        existing = {p.agent_name for p in (channel.participants or [])}
        for agent_name in event.metadata.get("target_agents", []):
            if agent_name == "__no_response__":
                continue
            if agent_name not in existing:
                db.add(ChannelMember(channel_id=channel.id, agent_name=agent_name))
                existing.add(agent_name)
        db.flush()

    return event


# ---------------------------------------------------------------------------
# Handler dispatch table
# ---------------------------------------------------------------------------

_HANDLERS = {
    "network.agent.join": _handle_agent_join,
    "network.agent.leave": _handle_agent_leave,
    "network.agent.remove": _handle_agent_remove,
    "network.ping": _handle_ping,
    "network.channel.create": _handle_channel_create,
    "network.channel.join": _handle_channel_join,
    "network.channel.leave": _handle_channel_leave,
    WorkspaceEventTypes.MESSAGE_POSTED: _handle_message_posted,
}
