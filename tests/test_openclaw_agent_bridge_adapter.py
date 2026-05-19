import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from openclaw_agent_bridge_adapter import AgentBinding, clean_reply, should_handle, slug


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
