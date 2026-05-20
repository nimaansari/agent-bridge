#!/usr/bin/env python3
"""Agent Bridge session adapter for local agent runtimes.

Transport adapter, not a chatbot script. Each configured Agent Bridge identity is
mapped to its own runtime session id. Inbound events targeted to that identity
are passed to the configured runtime (OpenClaw, Hermes, or an explicit command);
the runtime reply is posted back with reply anchors and terminal acks.

Agent Bridge itself stays agent-agnostic; every runtime connects through the
same session-adapter contract.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TERMINAL_ACKS = {"replied", "failed"}
DEFAULT_STATE = Path.home() / ".openclaw" / "workspace" / "agent-bridge" / ".tmp" / "openclaw_agent_bridge_adapter_state.json"
DEFAULT_CONFIG = Path.home() / ".openclaw" / "workspace" / "agent-bridge" / ".tmp" / "openclaw_agent_bridge_agents.json"
TOOL_REQUIRED_RE = re.compile(
    r"\b(ssh|ubuntu|server|deploy|docker|compose|systemctl|journalctl|git\s+(?:status|pull|push|commit|diff)|repo|logs?|terminal|shell|host|100\.97\.69\.95)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AgentBinding:
    agent_name: str
    runtime: str = "openclaw_model"
    openclaw_agent: str | None = None
    model: str | None = None
    thinking: str | None = None
    command: list[str] | None = None
    env: dict[str, str] | None = None
    session_prefix: str | None = None
    enabled: bool = True
    max_session_turns: int = 0
    max_prompt_chars: int = 6000
    allow_session_rotation: bool = False


def slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "agent"


def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def http_json(method: str, url: str, token: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps(data).encode() if data is not None else None
    headers = {"X-Workspace-Token": token, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1000]
        raise RuntimeError(f"HTTP {exc.code} {method} {url}: {detail}") from exc


def poll_events(base: str, network: str, channel: str, token: str, after: str | None = None, limit: int = 100) -> dict[str, Any]:
    params = {"network": network, "channel": channel, "type": "workspace", "sort": "asc", "limit": str(limit)}
    if after:
        params["after"] = after
    qs = urllib.parse.urlencode(params)
    data = http_json("GET", f"{base}/v1/events?{qs}", token)
    return data["data"]


def poll_agent_inbox(base: str, network: str, channel: str, token: str, agent_name: str, after: str | None = None, limit: int = 50) -> dict[str, Any]:
    params = {"network": network, "channel": channel, "limit": str(limit)}
    if after:
        params["after"] = after
    qs = urllib.parse.urlencode(params)
    data = http_json("GET", f"{base}/v1/agents/{urllib.parse.quote(agent_name)}/inbox?{qs}", token)
    return data["data"]


def discover_channel_agents(base: str, network: str, channel: str, token: str) -> list[str]:
    qs = urllib.parse.urlencode({"network": network})
    data = http_json("GET", f"{base}/v1/discover?{qs}", token)
    for row in data.get("data", {}).get("channels", []):
        if row.get("address") == f"channel/{channel}":
            return [name for name in row.get("participants", []) if name]
    return []


def post_heartbeat(base: str, network: str, token: str, binding: AgentBinding, session_id: str) -> None:
    payload = {"network": network, "agent_name": binding.agent_name}
    if os.environ.get("AGENT_BRIDGE_SEND_RUNTIME_SESSION_ID") == "1":
        # Agent Bridge's member session_id is an enrollment/session-token
        # concept, not the OpenClaw runtime conversation id. Keep it omitted
        # by default so adapter heartbeats are accepted for existing joined
        # agents without revoking themselves.
        payload["session_id"] = session_id
    http_json("POST", f"{base}/v1/heartbeat", token, payload)


def post_ack(base: str, network: str, token: str, event_id: str, agent_name: str, status: str, detail: str | None = None) -> None:
    payload: dict[str, Any] = {"network": network, "agent_name": agent_name, "status": status}
    if detail:
        payload["detail"] = detail[:500]
    http_json("POST", f"{base}/v1/events/{event_id}/ack", token, payload)


def safe_post_ack(base: str, network: str, token: str, event_id: str, agent_name: str, status: str, detail: str | None = None) -> None:
    try:
        post_ack(base, network, token, event_id, agent_name, status, detail)
    except Exception as exc:
        # Ack write failures must not crash the adapter. Crashing here leaves
        # the source message stuck at the previous state (usually processing)
        # and can create an infinite systemd restart loop.
        print(json.dumps({"agent": agent_name, "ack_failed": event_id, "status": status, "error": str(exc)}), file=sys.stderr, flush=True)


def task_summary_from_event(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    content = str(payload.get("content") or "").strip().replace("\n", " ")
    content = re.sub(r"\s+", " ", content)
    return content[:150] + ("…" if len(content) > 150 else "") if content else f"Handle event {event.get('id')}"


def update_agent_task(base: str, network: str, token: str, agent_name: str, current_task: str, task_status: str) -> None:
    payload = {"current_task": current_task[:180], "task_status": task_status}
    http_json(
        "PATCH",
        f"{base}/v1/workspaces/{urllib.parse.quote(network)}/members/{urllib.parse.quote(agent_name)}",
        token,
        payload,
    )


def safe_update_agent_task(base: str, network: str, token: str, agent_name: str, current_task: str, task_status: str) -> None:
    try:
        update_agent_task(base, network, token, agent_name, current_task, task_status)
    except Exception as exc:
        print(json.dumps({"agent": agent_name, "task_update_failed": str(exc)}), file=sys.stderr, flush=True)


def post_reply(base: str, network: str, channel: str, token: str, agent_name: str, content: str, reply_to: str) -> dict[str, Any]:
    payload = {
        "network": network,
        "type": "workspace.message.posted",
        "source": f"openagents:{agent_name}",
        "target": f"channel/{channel}",
        "payload": {
            "content": content,
            "sender_type": "agent",
            "sender_name": agent_name,
            "message_type": "chat",
            "reply_to": reply_to,
        },
        "metadata": {"reply_to": reply_to},
        "visibility": "channel",
    }
    return http_json("POST", f"{base}/v1/events", token, payload)["data"]


def is_tool_required_event(event: dict[str, Any]) -> bool:
    payload = event.get("payload") or {}
    metadata = event.get("metadata") or {}
    if metadata.get("operator_required") or metadata.get("tool_required"):
        return True
    content = str(payload.get("content") or "")
    return bool(TOOL_REQUIRED_RE.search(content))


def operator_queue_path(args: argparse.Namespace) -> Path:
    configured = os.environ.get("AGENT_BRIDGE_OPERATOR_QUEUE")
    if configured:
        return Path(configured).expanduser()
    return args.state.parent / "agent_bridge_operator_requests.jsonl"


def enqueue_operator_request(args: argparse.Namespace, binding: AgentBinding, event: dict[str, Any]) -> None:
    append_jsonl(operator_queue_path(args), {
        "queued_at": int(time.time() * 1000),
        "network": args.network,
        "channel": args.channel,
        "agent_name": binding.agent_name,
        "event_id": event.get("id"),
        "source": event.get("source"),
        "payload": event.get("payload") or {},
        "metadata": event.get("metadata") or {},
    })


def source_agent(source: str) -> str | None:
    return source.split(":", 1)[1] if source.startswith("openagents:") else None


def terminal_for_agent(event: dict[str, Any], agent_name: str) -> bool:
    responses = (event.get("metadata") or {}).get("handoff_responses") or {}
    status = (responses.get(agent_name) or {}).get("status")
    return status in TERMINAL_ACKS


def nonterminal_handoff_for_agent(event: dict[str, Any], agent_name: str) -> bool:
    responses = (event.get("metadata") or {}).get("handoff_responses") or {}
    status = (responses.get(agent_name) or {}).get("status")
    return bool(status and status not in TERMINAL_ACKS)


def should_handle(event: dict[str, Any], binding: AgentBinding) -> bool:
    if not binding.enabled:
        return False
    agent_name = binding.agent_name
    if event.get("type") != "workspace.message.posted":
        return False
    payload = event.get("payload") or {}
    if payload.get("message_type") in {"thinking", "status", "tool", "tool_call", "tool_result", "todos"}:
        return False
    if source_agent(event.get("source") or "") == agent_name:
        return False
    metadata = event.get("metadata") or {}
    targets = metadata.get("target_agents")
    required = metadata.get("required_responses") or []
    if agent_name in required:
        return not terminal_for_agent(event, agent_name)
    if isinstance(targets, list):
        return agent_name in targets
    # Human messages can enter every configured local agent session. Agent
    # messages must be explicitly targeted/required to prevent loops.
    return not str(event.get("source") or "").startswith("openagents:")


def clean_reply(raw: str) -> str:
    raw = raw.strip()
    try:
        obj = json.loads(raw)
        for key in ("reply", "text", "message", "content", "output"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        outputs = obj.get("outputs")
        if isinstance(outputs, list):
            texts = []
            for item in outputs:
                if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip():
                    texts.append(item["text"].strip())
                elif isinstance(item, str) and item.strip():
                    texts.append(item.strip())
            if texts:
                return "\n\n".join(texts).strip()
        result = obj.get("result") or obj.get("data")
        if isinstance(result, dict):
            payloads = result.get("payloads")
            if isinstance(payloads, list):
                texts = []
                for p in payloads:
                    if not isinstance(p, dict):
                        continue
                    ptype = str(p.get("type") or p.get("kind") or "text").lower()
                    if ptype in {"thinking", "status", "tool", "tool_call", "tool_result"}:
                        continue
                    text = p.get("text") or p.get("content")
                    if isinstance(text, str) and text.strip():
                        texts.append(text.strip())
                if texts:
                    return "\n\n".join(texts).strip()
            for key in ("reply", "text", "message", "content"):
                val = result.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    except Exception:
        pass
    return raw[-6000:] if len(raw) > 6000 else raw


def room_session_key(network: str, channel: str, binding: AgentBinding) -> str:
    return slug(f"{network}-{channel}-{binding.agent_name}")


def session_id_for(state: dict[str, Any], network: str, channel: str, binding: AgentBinding) -> str:
    runtime = binding.runtime or "openclaw"
    if is_sessionless_runtime(runtime):
        return "one-shot"
    sessions = state.setdefault("runtime_sessions", {})
    runtime_sessions = sessions.setdefault(runtime, {})
    key = room_session_key(network, channel, binding)
    if key not in runtime_sessions:
        # Preserve the original OpenClaw session id only for the same derived
        # room key. Older adapter state was keyed by agent_name alone, which
        # could leak one runtime conversation across different rooms.
        legacy_sessions = state.get("openclaw_sessions") or {}
        legacy = legacy_sessions.get(key)
        prefix = binding.session_prefix or f"agent-bridge-{runtime}"
        runtime_sessions[key] = legacy or slug(f"{prefix}-{network}-{channel}-{binding.agent_name}")
    return runtime_sessions[key]


def rotate_session_id(state: dict[str, Any], network: str, channel: str, binding: AgentBinding) -> str:
    runtime = binding.runtime or "openclaw"
    if is_sessionless_runtime(runtime):
        return "one-shot"
    key = room_session_key(network, channel, binding)
    prefix = binding.session_prefix or f"agent-bridge-{runtime}"
    new_id = slug(f"{prefix}-{network}-{channel}-{binding.agent_name}-{int(time.time())}")
    state.setdefault("runtime_sessions", {}).setdefault(runtime, {})[key] = new_id
    if runtime == "openclaw":
        state.setdefault("openclaw_sessions", {})[key] = new_id
    state.setdefault("runtime_session_turns", {}).setdefault(runtime, {})[key] = 0
    return new_id


def session_turns(state: dict[str, Any], binding: AgentBinding, network: str, channel: str) -> int:
    runtime = binding.runtime or "openclaw"
    key = room_session_key(network, channel, binding)
    return int(((state.get("runtime_session_turns") or {}).get(runtime) or {}).get(key) or 0)


def increment_session_turns(state: dict[str, Any], binding: AgentBinding, network: str, channel: str) -> None:
    runtime = binding.runtime or "openclaw"
    key = room_session_key(network, channel, binding)
    turns = state.setdefault("runtime_session_turns", {}).setdefault(runtime, {})
    turns[key] = int(turns.get(key) or 0) + 1


def maybe_rotate_before_turn(state: dict[str, Any], network: str, channel: str, binding: AgentBinding) -> str:
    # OpenClaw explicit sessions accumulate transcript. In Agent Bridge the
    # backend is the durable source of truth, so rotate local runtime sessions
    # periodically to prevent one long room from becoming unusable due to model
    # context overflow. Other runtimes can opt into the same behavior via config.
    if not binding.allow_session_rotation:
        return session_id_for(state, network, channel, binding)
    max_turns = max(1, int(binding.max_session_turns or 6))
    if session_turns(state, binding, network, channel) >= max_turns:
        return rotate_session_id(state, network, channel, binding)
    return session_id_for(state, network, channel, binding)


def clamp_text(value: str, max_chars: int) -> str:
    value = value or ""
    if len(value) <= max_chars:
        return value
    head = max_chars // 2
    tail = max_chars - head
    return value[:head].rstrip() + "\n\n[...middle truncated by Agent Bridge adapter... ]\n\n" + value[-tail:].lstrip()


def cursor_key(network: str, channel: str) -> str:
    return f"{network}:{channel}"


def inbox_cursor_key(network: str, channel: str, agent_name: str) -> str:
    return f"{network}:{channel}:{agent_name}:inbox"


def is_session_channel(channel: str) -> bool:
    """Return true for Agent Bridge session channels.

    `session-*` channels are the durable user-facing session threads. When an
    adapter first attaches to one, it must read the existing targeted messages
    in that session instead of skipping to the current head; otherwise a human
    can create/invite an agent and the first preexisting prompt is silently
    missed. Non-session channels keep the historical attach-at-head default to
    avoid replaying old operational traffic.
    """
    return bool(channel and channel.startswith("session-"))


def should_attach_at_head(channel: str, replay_existing: bool) -> bool:
    return not replay_existing and not is_session_channel(channel)


def run_command(cmd: list[str], timeout: int, extra_env: dict[str, str] | None = None) -> str:
    env = os.environ.copy()
    if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items()})
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout + 30, env=env)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"command exited {proc.returncode}").strip())
    reply = clean_reply(proc.stdout)
    if not reply:
        raise RuntimeError("runtime returned an empty reply")
    return reply


def expand_command_template(parts: list[str], binding: AgentBinding, session_id: str, prompt: str, timeout: int) -> list[str]:
    values = {
        "agent_name": binding.agent_name,
        "runtime": binding.runtime,
        "session_id": session_id,
        "message": prompt,
        "prompt": prompt,
        "timeout": str(timeout),
        "model": binding.model or "",
        "thinking": binding.thinking or "",
    }
    return [str(part).format(**values) for part in parts]


def run_openclaw_turn(binding: AgentBinding, session_id: str, prompt: str, timeout: int) -> str:
    openclaw_bin = os.environ.get("OPENCLAW_BIN") or shutil.which("openclaw")
    if not openclaw_bin:
        raise RuntimeError("openclaw binary not found; set OPENCLAW_BIN or put openclaw on PATH")
    cmd = [openclaw_bin, "agent", "--session-id", session_id, "--message", prompt, "--json", "--timeout", str(timeout)]
    if binding.openclaw_agent:
        cmd.extend(["--agent", binding.openclaw_agent])
    if binding.model:
        cmd.extend(["--model", binding.model])
    if binding.thinking:
        cmd.extend(["--thinking", binding.thinking])
    return run_command(cmd, timeout, binding.env)


def is_sessionless_runtime(runtime: str | None) -> bool:
    return (runtime or "").lower() in {"openclaw_model", "openclaw-model", "model", "infer", "capability_model"}


def run_openclaw_model_turn(binding: AgentBinding, prompt: str, timeout: int) -> str:
    """Run a one-shot OpenClaw model capability turn without creating a session.

    `openclaw agent --session-id ...` is useful for normal chat channels, but
    Agent Bridge should not pollute the user's visible OpenClaw/ClawDeck session
    list with adapter implementation details. The model capability transport is
    intentionally stateless and leaves durable conversation history in Agent
    Bridge, where it belongs.
    """
    openclaw_bin = os.environ.get("OPENCLAW_BIN") or shutil.which("openclaw")
    if not openclaw_bin:
        raise RuntimeError("openclaw binary not found; set OPENCLAW_BIN or put openclaw on PATH")
    cmd = [openclaw_bin, "capability", "model", "run", "--gateway", "--prompt", prompt, "--json"]
    if binding.model:
        cmd.extend(["--model", binding.model])
    return run_command(cmd, timeout, binding.env)


def run_configured_turn(binding: AgentBinding, session_id: str, prompt: str, timeout: int) -> str:
    runtime = (binding.runtime or "openclaw").lower()
    if is_sessionless_runtime(runtime):
        return run_openclaw_model_turn(binding, prompt, timeout)
    if runtime == "openclaw":
        return run_openclaw_turn(binding, session_id, prompt, timeout)
    if binding.command:
        return run_command(expand_command_template(binding.command, binding, session_id, prompt, timeout), timeout, binding.env)
    raise RuntimeError(
        f"runtime '{binding.runtime}' for agent '{binding.agent_name}' has no command configured; "
        "set agents[].command to the runtime CLI template"
    )


def looks_like_context_overflow(reply: str) -> bool:
    text = (reply or "").lower()
    return "context overflow" in text or "prompt too large" in text or "context length" in text


def run_runtime_turn_with_recovery(state: dict[str, Any], args: argparse.Namespace, binding: AgentBinding, prompt: str) -> tuple[str, str, bool]:
    session_id = maybe_rotate_before_turn(state, args.network, args.channel, binding)
    reply = run_configured_turn(binding, session_id, prompt, args.timeout)
    if (binding.runtime or "openclaw").lower() == "openclaw" and binding.allow_session_rotation and looks_like_context_overflow(reply):
        session_id = rotate_session_id(state, args.network, args.channel, binding)
        retry_prompt = prompt + "\n\nNote: this is a fresh runtime session after the previous runtime session exceeded context. Answer the current Agent Bridge message only.\n"
        reply = run_configured_turn(binding, session_id, retry_prompt, args.timeout)
        if looks_like_context_overflow(reply):
            raise RuntimeError(
                "openclaw_context_overflow: runtime returned context overflow after fresh-session retry"
            )
        increment_session_turns(state, binding, args.network, args.channel)
        return reply, session_id, True
    if looks_like_context_overflow(reply):
        raise RuntimeError("runtime_context_overflow: runtime returned context overflow")
    increment_session_turns(state, binding, args.network, args.channel)
    return reply, session_id, False


def runtime_session_description(binding: AgentBinding) -> str:
    """Human-facing description of the bound local runtime session.

    The adapter started as an OpenClaw adapter, but the bridge contract is
    runtime-agnostic: OpenClaw, Hermes, or any future CLI-backed agent runtime
    should receive the same semantics. Keep the prompt explicit that this is a
    real session turn, without implying Agent Bridge only supports OpenClaw.
    """
    runtime = (binding.runtime or "openclaw").lower()
    if is_sessionless_runtime(runtime):
        return "a one-shot OpenClaw model turn with no visible OpenClaw session"
    if runtime == "openclaw":
        return "a real OpenClaw session turn"
    return f"a real {binding.runtime} runtime session turn"


def build_prompt(event: dict[str, Any], binding: AgentBinding) -> str:
    payload = event.get("payload") or {}
    metadata = event.get("metadata") or {}
    sender = payload.get("sender_name") or source_agent(event.get("source") or "") or event.get("source") or "unknown"
    max_chars = max(1000, int(binding.max_prompt_chars or 6000))
    content = clamp_text(payload.get("content") or "", max_chars)
    required = binding.agent_name in (metadata.get("required_responses") or [])
    required_banner = (
        f"{binding.agent_name}: response needed — this handoff explicitly requires your reply.\n"
        if required else ""
    )
    return (
        f"You are {binding.agent_name}, a joined agent in an Agent Bridge session. "
        f"This is {runtime_session_description(binding)} delivered by the Agent Bridge transport adapter. "
        "Agent Bridge is runtime-agnostic; preserve session semantics for OpenClaw, Hermes, and future joined agents. "
        "Respond as an agent teammate with concrete work, code-level feedback, or a clear blocker. "
        "Do not produce generic agreement or repeated protocol summaries.\n\n"
        f"Agent Bridge event id: {event.get('id')}\n"
        f"Sender: {sender}\n"
        f"Response required: {required}\n"
        f"Message:\n{required_banner}{content}\n"
    )


def load_bindings(args: argparse.Namespace) -> list[AgentBinding]:
    raw: list[dict[str, Any]] = []
    config = args.config
    config_defaults: dict[str, Any] = {}
    auto_discover = args.discover_channel_agents
    if config and config.exists():
        data = json.loads(config.read_text())
        config_defaults = data.get("defaults") or {}
        auto_discover = bool(data.get("auto_discover", auto_discover))
        raw.extend(data.get("agents", []))
    for name in args.agent_name or []:
        raw.append({"agent_name": name})
    if auto_discover:
        try:
            for name in discover_channel_agents(args.base, args.network, args.channel, args.token):
                raw.append({**config_defaults, "agent_name": name})
        except Exception as exc:
            print(json.dumps({"warning": "agent_discovery_failed", "error": str(exc)}), file=sys.stderr, flush=True)
    if not raw:
        raw.append({"agent_name": "assistant"})
    seen: set[str] = set()
    bindings: list[AgentBinding] = []
    for item in raw:
        name = item.get("agent_name") or item.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        merged = {**config_defaults, **item}
        command = merged.get("command")
        if isinstance(command, str):
            command = [command]
        bindings.append(AgentBinding(
            agent_name=name,
            runtime=merged.get("runtime") or merged.get("driver") or "openclaw_model",
            openclaw_agent=merged.get("openclaw_agent"),
            model=merged.get("model"),
            thinking=merged.get("thinking"),
            command=command if isinstance(command, list) else None,
            env=merged.get("env") if isinstance(merged.get("env"), dict) else None,
            session_prefix=merged.get("session_prefix"),
            enabled=bool(merged.get("enabled", True)),
            max_session_turns=int(merged.get("max_session_turns", 0) or 0),
            max_prompt_chars=int(merged.get("max_prompt_chars", 6000) or 6000),
            allow_session_rotation=bool(merged.get("allow_session_rotation", False)),
        ))
    return bindings


def handle_event(args: argparse.Namespace, state: dict[str, Any], binding: AgentBinding, event: dict[str, Any]) -> None:
    event_id = event["id"]
    session_id = session_id_for(state, args.network, args.channel, binding)
    task_summary = task_summary_from_event(event)
    safe_update_agent_task(args.base, args.network, args.token, binding.agent_name, f"Responding: {task_summary}", "working")
    safe_post_ack(args.base, args.network, args.token, event_id, binding.agent_name, "delivered")
    safe_post_ack(args.base, args.network, args.token, event_id, binding.agent_name, "seen")
    safe_post_ack(args.base, args.network, args.token, event_id, binding.agent_name, "processing")

    if is_sessionless_runtime(binding.runtime) and is_tool_required_event(event):
        enqueue_operator_request(args, binding, event)
        reply = (
            "Tool-required request received. I am not refusing this: the sessionless Agent Bridge model path has no shell/SSH tools, "
            "so I queued this for the tool-capable mr.robot operator runtime instead of pretending I cannot access the server. "
            "The operator path has the Ubuntu/SSH access and should handle the repo/server work."
        )
        reply_event = post_reply(args.base, args.network, args.channel, args.token, binding.agent_name, reply, event_id)
        detail = f"reply_event_id={reply_event['id']}; runtime={binding.runtime}; operator_queue={operator_queue_path(args)}"
        safe_post_ack(args.base, args.network, args.token, event_id, binding.agent_name, "replied", detail)
        safe_update_agent_task(args.base, args.network, args.token, binding.agent_name, f"Queued for operator: {task_summary}", "done")
        print(json.dumps({"agent": binding.agent_name, "operator_queued": event_id, "reply_event_id": reply_event["id"], "queue": str(operator_queue_path(args))}), flush=True)
        return

    reply, session_id, recovered = run_runtime_turn_with_recovery(state, args, binding, build_prompt(event, binding))
    reply_event = post_reply(args.base, args.network, args.channel, args.token, binding.agent_name, reply, event_id)
    detail = f"reply_event_id={reply_event['id']}; runtime={binding.runtime}; session_id={session_id}"
    if recovered:
        detail += "; recovered=context_overflow_reset"
    safe_post_ack(args.base, args.network, args.token, event_id, binding.agent_name, "replied", detail)
    safe_update_agent_task(args.base, args.network, args.token, binding.agent_name, f"Answered: {task_summary}", "done")
    print(json.dumps({"agent": binding.agent_name, "runtime": binding.runtime, "handled": event_id, "reply_event_id": reply_event["id"], "session_id": session_id, "recovered": recovered}), flush=True)


def mark_processed(state: dict[str, Any], agent_name: str, event_id: str) -> None:
    by_agent = state.setdefault("processed_event_ids_by_agent", {})
    ids = set(by_agent.get(agent_name, []))
    ids.add(event_id)
    by_agent[agent_name] = sorted(ids)[-2000:]
    clear_retry(state, agent_name, event_id)


def processed_set(state: dict[str, Any], agent_name: str) -> set[str]:
    return set((state.get("processed_event_ids_by_agent") or {}).get(agent_name, []))


def retry_entry(state: dict[str, Any], agent_name: str, event_id: str) -> dict[str, Any] | None:
    return ((state.get("pending_retries_by_agent") or {}).get(agent_name) or {}).get(event_id)


def should_retry_now(state: dict[str, Any], agent_name: str, event_id: str, now: float | None = None) -> bool:
    entry = retry_entry(state, agent_name, event_id)
    if not entry:
        return True
    return float(entry.get("next_retry_at") or 0) <= (time.time() if now is None else now)


def record_transient_failure(state: dict[str, Any], agent_name: str, event: dict[str, Any], error: str, backoff_seconds: float) -> int:
    event_id = event["id"]
    by_agent = state.setdefault("pending_retries_by_agent", {}).setdefault(agent_name, {})
    entry = by_agent.get(event_id) or {"attempts": 0}
    attempts = int(entry.get("attempts") or 0) + 1
    entry.update({
        "attempts": attempts,
        "event": event,
        "last_error": error[:1000],
        "next_retry_at": time.time() + max(1.0, backoff_seconds) * attempts,
    })
    by_agent[event_id] = entry
    return attempts


def clear_retry(state: dict[str, Any], agent_name: str, event_id: str) -> None:
    by_agent = (state.get("pending_retries_by_agent") or {}).get(agent_name)
    if isinstance(by_agent, dict):
        by_agent.pop(event_id, None)


def due_retry_events(state: dict[str, Any], bindings: list[AgentBinding]) -> list[dict[str, Any]]:
    now = time.time()
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    pending = state.get("pending_retries_by_agent") or {}
    for binding in bindings:
        for event_id, entry in (pending.get(binding.agent_name) or {}).items():
            event = entry.get("event")
            if event_id in seen or not isinstance(event, dict):
                continue
            if float(entry.get("next_retry_at") or 0) <= now:
                events.append(event)
                seen.add(event_id)
    return events


def migrate_legacy_state(state: dict[str, Any], bindings: list[AgentBinding]) -> None:
    legacy = state.get("processed_event_ids")
    by_agent = state.setdefault("processed_event_ids_by_agent", {})
    if isinstance(legacy, list) and legacy:
        for binding in bindings:
            if binding.agent_name not in by_agent:
                by_agent[binding.agent_name] = sorted(set(str(x) for x in legacy))[-2000:]

    # Runtime sessions used to be keyed only by agent_name. New adapters key by
    # network/channel/agent so adding the same agent to another room creates a
    # fresh local runtime conversation instead of inheriting stale context.
    for runtime, runtime_sessions in list((state.get("runtime_sessions") or {}).items()):
        if not isinstance(runtime_sessions, dict):
            continue
        for binding in bindings:
            runtime_name = binding.runtime or "openclaw"
            if runtime != runtime_name:
                continue
            value = runtime_sessions.get(binding.agent_name)
            if isinstance(value, str):
                runtime_sessions.pop(binding.agent_name, None)
                state.setdefault("legacy_runtime_sessions_by_agent", {}).setdefault(runtime, {})[binding.agent_name] = value


def maybe_heartbeat(args: argparse.Namespace, state: dict[str, Any], bindings: list[AgentBinding]) -> None:
    now = time.time()
    last = float(state.get("last_heartbeat_at") or 0)
    if now - last < args.heartbeat_seconds:
        return
    for binding in bindings:
        if not binding.enabled:
            continue
        session_id = session_id_for(state, args.network, args.channel, binding)
        try:
            post_heartbeat(args.base, args.network, args.token, binding, session_id)
        except Exception as exc:
            print(json.dumps({"agent": binding.agent_name, "heartbeat_failed": str(exc)}), file=sys.stderr, flush=True)
    state["last_heartbeat_at"] = now


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("AGENT_BRIDGE_BASE", "http://127.0.0.1:3010"))
    ap.add_argument("--network", required=True)
    ap.add_argument("--channel", required=True)
    ap.add_argument("--token", default=os.environ.get("AGENT_BRIDGE_TOKEN"))
    ap.add_argument("--agent-name", action="append", help="Agent Bridge identity to bind. Repeatable.")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="JSON config: {agents:[{agent_name, openclaw_agent?, model?, thinking?}]}")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--poll-seconds", type=float, default=5.0)
    ap.add_argument("--heartbeat-seconds", type=float, default=25.0)
    ap.add_argument("--discover-channel-agents", action="store_true", help="Bind all current participants in the channel using config defaults.")
    ap.add_argument("--replay-existing", action="store_true", help="On first start without a cursor, process existing recent events instead of starting at the channel head.")
    ap.add_argument("--run-once", action="store_true")
    ap.add_argument("--baseline-only", action="store_true")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--retry-backoff-seconds", type=float, default=30.0, help="Base backoff for retryable handoff failures.")
    ap.add_argument("--max-transient-attempts", type=int, default=3, help="Stop retrying and mark processed after this many failed attempts.")
    ap.add_argument("--raw-room-poll", action="store_true", help="Legacy mode: poll the raw room transcript instead of the per-agent inbox.")
    ap.add_argument("--reconcile-limit", type=int, default=20, help="Also scan the current inbox tail for non-terminal handoffs that may have been skipped by cursor advancement.")
    args = ap.parse_args()
    if not args.token:
        print("AGENT_BRIDGE_TOKEN/--token is required", file=sys.stderr)
        return 2
    bindings = load_bindings(args)
    state = load_state(args.state)
    migrate_legacy_state(state, bindings)
    cursor = state.get("cursors", {}).get(cursor_key(args.network, args.channel))

    while True:
        bindings = load_bindings(args)
        migrate_legacy_state(state, bindings)
        maybe_heartbeat(args, state, bindings)
        retry_events = due_retry_events(state, bindings)
        try:
            if args.raw_room_poll:
                data = poll_events(args.base, args.network, args.channel, args.token, cursor)
                polled_events = data.get("events", [])
            else:
                data = {"events": [], "newest_id": None}
                polled_events = []
                for binding in bindings:
                    inbox_key = inbox_cursor_key(args.network, args.channel, binding.agent_name)
                    inbox_cursor = state.get("cursors", {}).get(inbox_key)
                    inbox = poll_agent_inbox(args.base, args.network, args.channel, args.token, binding.agent_name, inbox_cursor)
                    state.setdefault("cursors", {})[inbox_key] = inbox.get("newest_id") or inbox_cursor
                    for event in inbox.get("events", []):
                        event = dict(event)
                        event["__inbox_agent"] = binding.agent_name
                        polled_events.append(event)
                    if args.reconcile_limit > 0:
                        # Cursor advancement must not strand an already-acked
                        # non-terminal handoff forever. This tail scan is a repair
                        # lane: it does not move cursors, and it only reconsiders
                        # events whose durable handoff state is still non-terminal
                        # for this agent (for example `processing`).
                        reconcile = poll_agent_inbox(
                            args.base,
                            args.network,
                            args.channel,
                            args.token,
                            binding.agent_name,
                            None,
                            args.reconcile_limit,
                        )
                        for event in reconcile.get("events", []):
                            if not nonterminal_handoff_for_agent(event, binding.agent_name):
                                continue
                            event = dict(event)
                            event["__inbox_agent"] = binding.agent_name
                            event["__reconcile"] = True
                            polled_events.append(event)
                save_state(args.state, state)
        except Exception as exc:
            print(json.dumps({"poll_failed": str(exc)}), file=sys.stderr, flush=True)
            save_state(args.state, state)
            if args.run_once:
                return 1
            time.sleep(args.poll_seconds)
            continue
        events = retry_events + [e for e in polled_events if e.get("id") not in {r.get("id") for r in retry_events}]
        if args.baseline_only:
            by_agent = state.setdefault("processed_event_ids_by_agent", {})
            for binding in bindings:
                ids = sorted(set(by_agent.get(binding.agent_name, [])) | {e["id"] for e in events})
                by_agent[binding.agent_name] = ids[-1000:]
            newest = data.get("newest_id")
            if newest:
                state.setdefault("cursors", {})[cursor_key(args.network, args.channel)] = newest
            save_state(args.state, state)
            print(f"baselined {len(events)} events for {', '.join(b.agent_name for b in bindings)}")
            return 0

        if args.raw_room_poll and not cursor and polled_events and should_attach_at_head(args.channel, args.replay_existing):
            # First production start should attach at the current session head
            # rather than replaying old room history. Operators can opt into
            # replay with --replay-existing for repair/backfill jobs.
            # Exception: session-* channels are real user-facing session
            # threads, so first attach must process preexisting targeted human
            # prompts in that session instead of skipping them.
            cursor = data.get("newest_id")
            if cursor:
                state.setdefault("cursors", {})[cursor_key(args.network, args.channel)] = cursor
                save_state(args.state, state)
            if args.run_once:
                return 0
            time.sleep(args.poll_seconds)
            continue

        for event in events:
            is_retried_event = any(event.get("id") == r.get("id") for r in retry_events)
            if event.get("id") and not is_retried_event and not event.get("__reconcile"):
                cursor = event["id"]
                state.setdefault("cursors", {})[cursor_key(args.network, args.channel)] = cursor
            for binding in bindings:
                event_id = event["id"]
                inbox_agent = event.get("__inbox_agent")
                if inbox_agent and inbox_agent != binding.agent_name:
                    continue
                already_processed = event_id in processed_set(state, binding.agent_name)
                if (already_processed and not nonterminal_handoff_for_agent(event, binding.agent_name)) or not should_handle(event, binding):
                    continue
                if not should_retry_now(state, binding.agent_name, event_id):
                    continue
                try:
                    handle_event(args, state, binding, event)
                    mark_processed(state, binding.agent_name, event_id)
                    save_state(args.state, state)
                except Exception as exc:
                    safe_update_agent_task(args.base, args.network, args.token, binding.agent_name, f"Blocked: {task_summary_from_event(event)}", "blocked")
                    attempts = record_transient_failure(state, binding.agent_name, event, str(exc), args.retry_backoff_seconds)
                    terminal = attempts >= args.max_transient_attempts
                    # Backend currently accepts the public statuses
                    # delivered/seen/processing/replied/failed. Keep richer
                    # retry semantics in adapter state/detail until the
                    # durable attempt table lands.
                    status = "failed"
                    detail = f"attempts={attempts}; terminal={terminal}; retryable={not terminal}; error={str(exc)}"
                    try:
                        safe_post_ack(args.base, args.network, args.token, event_id, binding.agent_name, status, detail)
                    finally:
                        if terminal:
                            mark_processed(state, binding.agent_name, event_id)
                        save_state(args.state, state)
                        print(json.dumps({"agent": binding.agent_name, status: event_id, "attempts": attempts, "error": str(exc)}), file=sys.stderr, flush=True)
        save_state(args.state, state)
        if args.run_once:
            return 0
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
