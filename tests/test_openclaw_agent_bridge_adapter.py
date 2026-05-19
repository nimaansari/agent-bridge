import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from openclaw_agent_bridge_adapter import (
    AgentBinding,
    build_prompt,
    clamp_text,
    clean_reply,
    expand_command_template,
    is_session_channel,
    load_bindings,
    migrate_legacy_state,
    record_transient_failure,
    maybe_rotate_before_turn,
    rotate_session_id,
    run_runtime_turn_with_recovery,
    session_id_for,
    should_attach_at_head,
    should_handle,
    should_retry_now,
    slug,
    terminal_for_agent,
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


def test_session_channels_do_not_attach_at_head_by_default():
    assert is_session_channel("session-alpha") is True
    assert should_attach_at_head("session-alpha", replay_existing=False) is False


def test_non_session_channels_attach_at_head_unless_replay_requested():
    assert is_session_channel("channel-alpha") is False
    assert should_attach_at_head("channel-alpha", replay_existing=False) is True
    assert should_attach_at_head("channel-alpha", replay_existing=True) is False


def test_failed_is_terminal_for_required_response_until_attempt_table_lands():
    event = {
        "id": "evt-1",
        "type": "workspace.message.posted",
        "source": "human:user",
        "payload": {"content": "please answer"},
        "metadata": {
            "required_responses": ["mr.robot"],
            "handoff_responses": {"mr.robot": {"status": "failed"}},
        },
    }
    assert terminal_for_agent(event, "mr.robot") is True
    assert should_handle(event, AgentBinding("mr.robot")) is False


def test_transient_failure_records_retry_backoff_without_processed_marker():
    state = {}
    event = {"id": "evt-1", "type": "workspace.message.posted", "source": "human:user", "payload": {"content": "retry me"}}
    attempts = record_transient_failure(state, "mr.robot", event, "rate limited", 30.0)
    assert attempts == 1
    assert "evt-1" not in state.get("processed_event_ids_by_agent", {}).get("mr.robot", [])
    assert should_retry_now(state, "mr.robot", "evt-1", now=0) is False


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


def test_runtime_session_ids_are_isolated_per_room():
    state = {}
    binding = AgentBinding("mr.robot", runtime="openclaw")
    first = session_id_for(state, "workspace", "room-a", binding)
    second = session_id_for(state, "workspace", "room-b", binding)
    assert first != second
    assert len(state["runtime_sessions"]["openclaw"]) == 2


def test_runtime_session_rotates_after_configured_turn_limit():
    state = {}
    binding = AgentBinding("mr.robot", runtime="openclaw", max_session_turns=1)
    first = maybe_rotate_before_turn(state, "workspace", "room", binding)
    state.setdefault("runtime_session_turns", {}).setdefault("openclaw", {})[
        "workspace-room-mr.robot"
    ] = 1
    second = maybe_rotate_before_turn(state, "workspace", "room", binding)
    assert second != first


def test_prompt_content_is_clamped_for_large_agent_messages():
    long = "A" * 5000 + "B" * 5000
    clamped = clamp_text(long, 2000)
    assert len(clamped) < len(long)
    assert "middle truncated" in clamped
    event = {
        "id": "evt-long",
        "type": "workspace.message.posted",
        "source": "openagents:Amin",
        "payload": {"content": long, "sender_name": "Amin"},
        "metadata": {},
    }
    prompt = build_prompt(event, AgentBinding("mr.robot", max_prompt_chars=2000))
    assert "middle truncated" in prompt
    assert len(prompt) < 3500


def test_rotate_session_id_preserves_runtime_namespace():
    state = {}
    binding = AgentBinding("mr.robot", runtime="openclaw")
    first = session_id_for(state, "workspace", "channel", binding)
    rotated = rotate_session_id(state, "workspace", "channel", binding)
    assert rotated != first
    assert rotated in state["runtime_sessions"]["openclaw"].values()


def test_legacy_agent_name_session_key_is_not_reused_for_new_room():
    state = {"runtime_sessions": {"openclaw": {"mr.robot": "old-shared-session"}}}
    binding = AgentBinding("mr.robot", runtime="openclaw")
    migrate_legacy_state(state, [binding])
    session_id = session_id_for(state, "workspace", "new-room", binding)
    assert session_id != "old-shared-session"
    assert "mr.robot" not in state["runtime_sessions"]["openclaw"]
    assert state["legacy_runtime_sessions_by_agent"]["openclaw"]["mr.robot"] == "old-shared-session"


def test_command_template_supports_non_openclaw_runtimes():
    binding = AgentBinding(
        "Hermes",
        runtime="hermes",
        command=["hermes", "chat", "--session", "{session_id}", "--message", "{message}", "--timeout", "{timeout}"],
    )
    cmd = expand_command_template(binding.command, binding, "sess-1", "hello", 120)
    assert cmd == ["hermes", "chat", "--session", "sess-1", "--message", "hello", "--timeout", "120"]


def test_bridge_prompt_preserves_runtime_agnostic_session_semantics():
    event = {
        "id": "evt-1",
        "type": "workspace.message.posted",
        "source": "human:user",
        "payload": {"content": "fix bridge handling", "sender_name": "Nima"},
        "metadata": {"required_responses": ["Hermes"]},
    }
    prompt = build_prompt(event, AgentBinding("Hermes", runtime="hermes"))
    assert "real hermes runtime session turn" in prompt
    assert "runtime-agnostic" in prompt
    assert "OpenClaw, Hermes, and future joined agents" in prompt
    assert "Response required: True" in prompt


def test_bridge_prompt_keeps_openclaw_session_language_for_openclaw_runtime():
    event = {
        "id": "evt-2",
        "type": "workspace.message.posted",
        "source": "human:user",
        "payload": {"content": "live test"},
        "metadata": {},
    }
    prompt = build_prompt(event, AgentBinding("mr.robot", runtime="openclaw"))
    assert "real OpenClaw session turn" in prompt
    assert "runtime-agnostic" in prompt


def test_context_overflow_is_not_returned_as_visible_reply(monkeypatch):
    import openclaw_agent_bridge_adapter as adapter

    def fake_turn(binding, session_id, prompt, timeout):
        return "Context overflow: prompt too large for the model. Try /reset (or /new) to start a fresh session, or use a larger-context model."

    monkeypatch.setattr(adapter, "run_configured_turn", fake_turn)
    args = argparse.Namespace(network="net", channel="chan", timeout=30)
    try:
        run_runtime_turn_with_recovery({}, args, AgentBinding("mr.robot", runtime="openclaw"), "hello")
    except RuntimeError as exc:
        assert "context_overflow" in str(exc)
    else:
        raise AssertionError("context overflow must raise instead of becoming chat")


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
