#!/usr/bin/env python3
"""Agent Bridge ↔ OpenClaw session adapter.

Transport adapter, not a chatbot script. Each configured Agent Bridge identity is
mapped to its own OpenClaw session id. Inbound events targeted to that identity
are passed to OpenClaw; the OpenClaw reply is posted back with reply anchors and
terminal acks.

This is generic across local OpenClaw-backed agents: add more --agent entries or
a JSON --config. Agent Bridge itself stays agent-agnostic; every agent connects
through the same session-adapter contract.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TERMINAL_ACKS = {"replied", "failed"}
DEFAULT_STATE = Path.home() / ".openclaw" / "workspace" / "agent-bridge" / ".tmp" / "openclaw_agent_bridge_adapter_state.json"
DEFAULT_CONFIG = Path.home() / ".openclaw" / "workspace" / "agent-bridge" / ".tmp" / "openclaw_agent_bridge_agents.json"


@dataclass(frozen=True)
class AgentBinding:
    agent_name: str
    openclaw_agent: str | None = None
    model: str | None = None
    thinking: str | None = None


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


def http_json(method: str, url: str, token: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps(data).encode() if data is not None else None
    headers = {"X-Workspace-Token": token, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def poll_events(base: str, network: str, channel: str, token: str, limit: int = 100) -> list[dict[str, Any]]:
    qs = urllib.parse.urlencode({"network": network, "channel": channel, "type": "workspace", "sort": "desc", "limit": str(limit)})
    data = http_json("GET", f"{base}/v1/events?{qs}", token)
    return list(reversed(data["data"]["events"]))


def post_ack(base: str, network: str, token: str, event_id: str, agent_name: str, status: str, detail: str | None = None) -> None:
    payload: dict[str, Any] = {"network": network, "agent_name": agent_name, "status": status}
    if detail:
        payload["detail"] = detail[:500]
    http_json("POST", f"{base}/v1/events/{event_id}/ack", token, payload)


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


def source_agent(source: str) -> str | None:
    return source.split(":", 1)[1] if source.startswith("openagents:") else None


def terminal_for_agent(event: dict[str, Any], agent_name: str) -> bool:
    responses = (event.get("metadata") or {}).get("handoff_responses") or {}
    status = (responses.get(agent_name) or {}).get("status")
    return status in TERMINAL_ACKS


def should_handle(event: dict[str, Any], binding: AgentBinding) -> bool:
    agent_name = binding.agent_name
    if event.get("type") != "workspace.message.posted":
        return False
    payload = event.get("payload") or {}
    if payload.get("message_type") == "status":
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
        result = obj.get("result") or obj.get("data")
        if isinstance(result, dict):
            for key in ("reply", "text", "message", "content"):
                val = result.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    except Exception:
        pass
    return raw[-6000:] if len(raw) > 6000 else raw


def session_id_for(state: dict[str, Any], network: str, channel: str, agent_name: str) -> str:
    sessions = state.setdefault("openclaw_sessions", {})
    if agent_name not in sessions:
        sessions[agent_name] = slug(f"agent-bridge-{network}-{channel}-{agent_name}")
    return sessions[agent_name]


def run_openclaw_turn(binding: AgentBinding, session_id: str, prompt: str, timeout: int) -> str:
    openclaw_bin = os.environ.get("OPENCLAW_BIN", "/home/nimapro1381/.npm-global/bin/openclaw")
    cmd = [openclaw_bin, "agent", "--session-id", session_id, "--message", prompt, "--json", "--timeout", str(timeout)]
    if binding.openclaw_agent:
        cmd.extend(["--agent", binding.openclaw_agent])
    if binding.model:
        cmd.extend(["--model", binding.model])
    if binding.thinking:
        cmd.extend(["--thinking", binding.thinking])
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout + 30)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"openclaw exited {proc.returncode}").strip())
    reply = clean_reply(proc.stdout)
    if not reply:
        raise RuntimeError("openclaw returned an empty reply")
    return reply


def build_prompt(event: dict[str, Any], binding: AgentBinding) -> str:
    payload = event.get("payload") or {}
    metadata = event.get("metadata") or {}
    sender = payload.get("sender_name") or source_agent(event.get("source") or "") or event.get("source") or "unknown"
    content = payload.get("content") or ""
    required = binding.agent_name in (metadata.get("required_responses") or [])
    return (
        f"You are {binding.agent_name}, a joined agent in an Agent Bridge session. "
        "This is a real OpenClaw session turn delivered by the Agent Bridge transport adapter. "
        "Respond as an agent teammate with concrete work, code-level feedback, or a clear blocker. "
        "Do not produce generic agreement or repeated protocol summaries.\n\n"
        f"Agent Bridge event id: {event.get('id')}\n"
        f"Sender: {sender}\n"
        f"Response required: {required}\n"
        f"Message:\n{content}\n"
    )


def load_bindings(args: argparse.Namespace) -> list[AgentBinding]:
    raw: list[dict[str, Any]] = []
    config = args.config
    if config and config.exists():
        data = json.loads(config.read_text())
        raw.extend(data.get("agents", []))
    for name in args.agent_name or []:
        raw.append({"agent_name": name})
    if not raw:
        raw.append({"agent_name": "mr.robot"})
    seen: set[str] = set()
    bindings: list[AgentBinding] = []
    for item in raw:
        name = item.get("agent_name") or item.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        bindings.append(AgentBinding(agent_name=name, openclaw_agent=item.get("openclaw_agent"), model=item.get("model"), thinking=item.get("thinking")))
    return bindings


def handle_event(args: argparse.Namespace, state: dict[str, Any], binding: AgentBinding, event: dict[str, Any]) -> None:
    event_id = event["id"]
    session_id = session_id_for(state, args.network, args.channel, binding.agent_name)
    post_ack(args.base, args.network, args.token, event_id, binding.agent_name, "delivered")
    post_ack(args.base, args.network, args.token, event_id, binding.agent_name, "seen")
    post_ack(args.base, args.network, args.token, event_id, binding.agent_name, "processing")
    reply = run_openclaw_turn(binding, session_id, build_prompt(event, binding), args.timeout)
    reply_event = post_reply(args.base, args.network, args.channel, args.token, binding.agent_name, reply, event_id)
    post_ack(args.base, args.network, args.token, event_id, binding.agent_name, "replied", f"reply_event_id={reply_event['id']}")
    print(json.dumps({"agent": binding.agent_name, "handled": event_id, "reply_event_id": reply_event["id"]}), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("AGENT_BRIDGE_BASE", "http://100.97.69.95:3010"))
    ap.add_argument("--network", required=True)
    ap.add_argument("--channel", required=True)
    ap.add_argument("--token", default=os.environ.get("AGENT_BRIDGE_TOKEN"))
    ap.add_argument("--agent-name", action="append", help="Agent Bridge identity to bind. Repeatable.")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="JSON config: {agents:[{agent_name, openclaw_agent?, model?, thinking?}]}")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--poll-seconds", type=float, default=5.0)
    ap.add_argument("--run-once", action="store_true")
    ap.add_argument("--baseline-only", action="store_true")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()
    if not args.token:
        print("AGENT_BRIDGE_TOKEN/--token is required", file=sys.stderr)
        return 2
    bindings = load_bindings(args)
    state = load_state(args.state)
    processed_by_agent: dict[str, set[str]] = {
        b.agent_name: set((state.get("processed_event_ids_by_agent") or {}).get(b.agent_name, [])) for b in bindings
    }

    while True:
        events = poll_events(args.base, args.network, args.channel, args.token)
        if args.baseline_only:
            by_agent = state.setdefault("processed_event_ids_by_agent", {})
            for binding in bindings:
                ids = sorted(set(by_agent.get(binding.agent_name, [])) | {e["id"] for e in events})
                by_agent[binding.agent_name] = ids[-1000:]
            save_state(args.state, state)
            print(f"baselined {len(events)} events for {', '.join(b.agent_name for b in bindings)}")
            return 0

        for event in events:
            for binding in bindings:
                event_id = event["id"]
                processed = processed_by_agent.setdefault(binding.agent_name, set())
                if event_id in processed or not should_handle(event, binding):
                    continue
                processed.add(event_id)
                state.setdefault("processed_event_ids_by_agent", {})[binding.agent_name] = sorted(processed)[-1000:]
                save_state(args.state, state)
                try:
                    handle_event(args, state, binding, event)
                    save_state(args.state, state)
                except Exception as exc:
                    try:
                        post_ack(args.base, args.network, args.token, event_id, binding.agent_name, "failed", str(exc))
                    finally:
                        print(json.dumps({"agent": binding.agent_name, "failed": event_id, "error": str(exc)}), file=sys.stderr, flush=True)
        if args.run_once:
            return 0
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
