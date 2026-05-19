#!/usr/bin/env python3
"""Agent Bridge ↔ OpenClaw session adapter.

This is intentionally a transport adapter, not a responder. Inbound Agent Bridge
messages are handed to OpenClaw as a real session turn using a stable session id;
OpenClaw's generated reply is then posted back to Agent Bridge with reply anchors
and terminal acks.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

TERMINAL_ACKS = {"replied", "failed"}
DEFAULT_STATE = Path.home() / ".openclaw" / "workspace" / "agent-bridge" / ".tmp" / "openclaw_agent_bridge_adapter_state.json"


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
    qs = urllib.parse.urlencode({
        "network": network,
        "channel": channel,
        "type": "workspace",
        "sort": "desc",
        "limit": str(limit),
    })
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
    if source.startswith("openagents:"):
        return source.split(":", 1)[1]
    return None


def should_handle(event: dict[str, Any], agent_name: str) -> bool:
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
        responses = metadata.get("handoff_responses") or {}
        status = (responses.get(agent_name) or {}).get("status")
        return status not in TERMINAL_ACKS
    if isinstance(targets, list):
        return agent_name in targets
    # Human/channel messages are valid session input; agent-to-agent messages
    # without explicit targeting are not, to avoid accidental loops.
    return not str(event.get("source") or "").startswith("openagents:")


def clean_reply(raw: str) -> str:
    raw = raw.strip()
    try:
        obj = json.loads(raw)
        for key in ("reply", "text", "message", "content", "output"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        # Some OpenClaw JSON uses nested result fields.
        result = obj.get("result") or obj.get("data")
        if isinstance(result, dict):
            for key in ("reply", "text", "message", "content"):
                val = result.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    except Exception:
        pass
    # Strip obvious JSON/log wrappers only as a fallback.
    return raw[-6000:] if len(raw) > 6000 else raw


def run_openclaw_turn(session_id: str, prompt: str, timeout: int) -> str:
    cmd = ["openclaw", "agent", "--session-id", session_id, "--message", prompt, "--json", "--timeout", str(timeout)]
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout + 30)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"openclaw exited {proc.returncode}").strip())
    reply = clean_reply(proc.stdout)
    if not reply:
        raise RuntimeError("openclaw returned an empty reply")
    return reply


def build_prompt(event: dict[str, Any], agent_name: str) -> str:
    payload = event.get("payload") or {}
    metadata = event.get("metadata") or {}
    sender = payload.get("sender_name") or source_agent(event.get("source") or "") or event.get("source") or "unknown"
    content = payload.get("content") or ""
    required = agent_name in (metadata.get("required_responses") or [])
    return (
        "You are mr.robot participating in an Agent Bridge session. "
        "This is a real OpenClaw session turn for the Agent Bridge transport. "
        "Reply with useful work or a concrete blocker. Do not repeat protocol summaries. "
        "If addressed by another agent, coordinate with implementation-level detail.\n\n"
        f"Agent Bridge event id: {event.get('id')}\n"
        f"Sender: {sender}\n"
        f"Response required: {required}\n"
        f"Message:\n{content}\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("AGENT_BRIDGE_BASE", "http://100.97.69.95:3010"))
    ap.add_argument("--network", required=True)
    ap.add_argument("--channel", required=True)
    ap.add_argument("--token", default=os.environ.get("AGENT_BRIDGE_TOKEN"))
    ap.add_argument("--agent-name", default="mr.robot")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--poll-seconds", type=float, default=5.0)
    ap.add_argument("--run-once", action="store_true")
    ap.add_argument("--baseline-only", action="store_true")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()
    if not args.token:
        print("AGENT_BRIDGE_TOKEN/--token is required", file=sys.stderr)
        return 2

    state = load_state(args.state)
    processed = set(state.get("processed_event_ids") or [])
    session_id = state.get("openclaw_session_id") or f"agent-bridge:{args.network}:{args.channel}:{args.agent_name}"
    state["openclaw_session_id"] = session_id

    while True:
        events = poll_events(args.base, args.network, args.channel, args.token)
        if args.baseline_only:
            processed.update(e["id"] for e in events)
            state["processed_event_ids"] = sorted(processed)
            save_state(args.state, state)
            print(f"baselined {len(events)} events")
            return 0

        for event in events:
            event_id = event["id"]
            if event_id in processed or not should_handle(event, args.agent_name):
                continue
            processed.add(event_id)
            state["processed_event_ids"] = sorted(processed)[-1000:]
            save_state(args.state, state)
            try:
                post_ack(args.base, args.network, args.token, event_id, args.agent_name, "delivered")
                post_ack(args.base, args.network, args.token, event_id, args.agent_name, "seen")
                post_ack(args.base, args.network, args.token, event_id, args.agent_name, "processing")
                reply = run_openclaw_turn(session_id, build_prompt(event, args.agent_name), args.timeout)
                reply_event = post_reply(args.base, args.network, args.channel, args.token, args.agent_name, reply, event_id)
                post_ack(args.base, args.network, args.token, event_id, args.agent_name, "replied", f"reply_event_id={reply_event['id']}")
                print(json.dumps({"handled": event_id, "reply_event_id": reply_event["id"]}))
            except Exception as exc:
                post_ack(args.base, args.network, args.token, event_id, args.agent_name, "failed", str(exc))
                print(json.dumps({"failed": event_id, "error": str(exc)}), file=sys.stderr)
        if args.run_once:
            return 0
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
