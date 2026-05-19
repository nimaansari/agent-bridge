import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from openclaw_agent_bridge_adapter import (
    AgentBinding,
    clean_reply,
    expand_command_template,
    load_bindings,
    rotate_session_id,
    session_id_for,
    should_handle,
    slug,
)


def test_clean_reply_extracts_visible_text_payloads_only():
    raw = json.dumps({
        "result": {
            "payloads": [
                {"type": "thinking", "text": "private chain of thought"},
                {"type": "tool_call", "text": "curl something"},
                {"type": "text", "text": "Clean room reply"},
            ]
        }
    })
    assert clean_reply(raw) == "Clean room reply"


def test_should_handle_only_stable_targeted_agent_name():
    event = {
        "id": "evt-1",
        "type": "workspace.message.posted",
        "source": "human:user",
        "payload": {"content": "Amin and mr.robot please review"},
        "metadata": {"target_agents": ["mr.robot"]},
    }
    assert should_handle(event, AgentBinding("mr.robot")) is True
    assert should_handle(event, AgentBinding("Robot label")) is False


def test_should_ignore_intermediate_and_self_messages():
    binding = AgentBinding("mr.robot")
    assert should_handle({
        "type": "workspace.message.posted",
        "source": "human:user",
        "payload": {"message_type": "status", "content": "working"},
        "metadata": {"target_agents": ["mr.robot"]},
    }, binding) is False
    assert should_handle({
        "type": "workspace.message.posted",
        "source": "openagents:mr.robot",
        "payload": {"message_type": "chat", "content": "reply"},
        "metadata": {"target_agents": ["mr.robot"]},
    }, binding) is False


def test_slug_creates_openclaw_safe_session_ids():
    assert ":" not in slug("agent-bridge:workspace/channel:mr.robot")


def test_runtime_session_ids_are_namespaced_by_runtime():
    state = {}
    openclaw = AgentBinding("mr.robot", runtime="openclaw")
    hermes = AgentBinding("Hermes", runtime="hermes", session_prefix="agent-bridge-hermes")
    assert session_id_for(state, "workspace:1", "channel/x", openclaw).startswith("agent-bridge-openclaw-")
    assert session_id_for(state, "workspace:1", "channel/x", hermes).startswith("agent-bridge-hermes-")
    assert set(state["runtime_sessions"].keys()) == {"openclaw", "hermes"}


def test_rotate_session_id_preserves_runtime_namespace():
    state = {}
    binding = AgentBinding("mr.robot", runtime="openclaw")
    first = session_id_for(state, "workspace", "channel", binding)
    rotated = rotate_session_id(state, "workspace", "channel", binding)
    assert rotated != first
    assert state["runtime_sessions"]["openclaw"]["mr.robot"] == rotated


def test_command_template_supports_non_openclaw_runtimes():
    binding = AgentBinding(
        "Hermes",
        runtime="hermes",
        command=["hermes", "chat", "--session", "{session_id}", "--message", "{message}", "--timeout", "{timeout}"],
    )
    cmd = expand_command_template(binding.command, binding, "sess-1", "hello", 120)
    assert cmd == ["hermes", "chat", "--session", "sess-1", "--message", "hello", "--timeout", "120"]


def test_load_bindings_accepts_runtime_config(tmp_path):
    config = tmp_path / "agents.json"
    config.write_text(json.dumps({
        "defaults": {"model": "openrouter/auto"},
        "agents": [
            {"agent_name": "mr.robot", "runtime": "openclaw"},
            {"agent_name": "Hermes", "runtime": "hermes", "command": ["hermes", "chat", "--session", "{session_id}"]},
        ],
    }))
    args = argparse.Namespace(
        config=config,
        agent_name=[],
        discover_channel_agents=False,
        base="http://127.0.0.1:3010",
        network="net",
        channel="chan",
        token="tok",
    )
    bindings = load_bindings(args)
    assert [(b.agent_name, b.runtime, b.model) for b in bindings] == [
        ("mr.robot", "openclaw", "openrouter/auto"),
        ("Hermes", "hermes", "openrouter/auto"),
    ]
    assert bindings[1].command == ["hermes", "chat", "--session", "{session_id}"]
