# -*- coding: utf-8 -*-
"""
Tests for the event-native API (POST/GET /v1/events).
"""

import pytest


def _anchor_event_id(client, workspace, channel_name, content="anchor"):
    """Create a human message agents can reply to."""
    resp = client.post("/v1/events", json={
        "type": "workspace.message.posted",
        "source": "human:user1",
        "target": f"channel/{channel_name}",
        "payload": {"content": content},
        "network": workspace["id"],
    }, headers={"X-Workspace-Token": workspace["token"]})
    assert resp.status_code == 200
    return resp.json()["data"]["id"]


class TestSendEvent:
    """POST /v1/events — send events through the pipeline."""

    def test_send_message_event(self, client, workspace):
        """Send a workspace.message.posted event through the pipeline."""
        channel_name = workspace["channel"]["name"]
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user1",
            "target": f"channel/{channel_name}",
            "payload": {"content": "Hello, world!"},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["type"] == "workspace.message.posted"
        assert data["source"] == "human:user1"
        assert data["target"] == f"channel/{channel_name}"
        assert "id" in data
        assert "timestamp" in data

    def test_intermediate_status_messages_are_not_persisted(self, client, workspace):
        """Thinking/status/tool chatter should not become durable room history."""
        channel_name = workspace["channel"]["name"]
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user1",
            "target": f"channel/{channel_name}",
            "payload": {"content": "working...", "message_type": "status"},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})

        assert resp.status_code == 200
        event_id = resp.json()["data"]["id"]

        poll = client.get("/v1/events", params={
            "network": workspace["id"],
            "channel": channel_name,
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert poll.status_code == 200
        ids = [e["id"] for e in poll.json()["data"]["events"]]
        assert event_id not in ids

    def test_send_event_missing_network(self, client, workspace):
        """Events without network field are rejected."""
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user1",
            "target": "channel/test",
        })
        assert resp.status_code == 400

    def test_send_event_invalid_network(self, client, workspace):
        """Events with nonexistent network are rejected."""
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user1",
            "target": "channel/test",
            "network": "nonexistent",
        })
        assert resp.status_code == 404

    def test_send_event_wrong_token(self, client, workspace):
        """Events with wrong token are rejected by auth mod."""
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user1",
            "target": "channel/test",
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": "wrong-token"})
        assert resp.status_code == 401

    def test_send_event_stamps_network_id(self, client, workspace):
        """Auth mod stamps network ID on the event."""
        channel_name = workspace["channel"]["name"]
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user1",
            "target": f"channel/{channel_name}",
            "payload": {"content": "test"},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})

        assert resp.status_code == 200

        # Verify event was persisted
        poll = client.get("/v1/events", params={"network": workspace["id"]},
                          headers={"X-Workspace-Token": workspace["token"]})
        assert poll.status_code == 200
        events = poll.json()["data"]["events"]
        assert len(events) >= 1
        found = [e for e in events if e["type"] == "workspace.message.posted"]
        assert len(found) >= 1

    def test_send_event_with_metadata(self, client, workspace):
        """Custom metadata is preserved through the pipeline."""
        channel_name = workspace["channel"]["name"]
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user1",
            "target": f"channel/{channel_name}",
            "payload": {"content": "test"},
            "metadata": {"custom_key": "custom_value"},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["metadata"]["custom_key"] == "custom_value"

    def test_agent_chat_without_reply_to_is_auto_anchored(self, client, workspace):
        """Legacy agent chat is auto-anchored to the message that targeted it."""
        channel_name = workspace["channel"]["name"]
        anchor_id = _anchor_event_id(client, workspace, channel_name, "Please answer this")
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "openagents:agent-alpha",
            "target": f"channel/{channel_name}",
            "payload": {"content": "legacy answer", "message_type": "chat"},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["metadata"]["reply_to"] == anchor_id

    def test_agent_chat_reply_to_is_normalized(self, client, workspace):
        """Plain reply_to ids become ClawDeck-style quote objects in payload."""
        channel_name = workspace["channel"]["name"]
        anchor_id = _anchor_event_id(client, workspace, channel_name, "Question for the agent")
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "openagents:agent-alpha",
            "target": f"channel/{channel_name}",
            "payload": {"content": "Anchored answer", "reply_to": anchor_id},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["metadata"]["reply_to"] == anchor_id

        poll = client.get("/v1/events", params={"network": workspace["id"], "target": f"channel/{channel_name}"},
                          headers={"X-Workspace-Token": workspace["token"]})
        events = poll.json()["data"]["events"]
        reply = next(e for e in events if e["id"] == data["id"])
        assert reply["payload"]["reply_to"]["id"] == anchor_id
        assert reply["payload"]["reply_to"]["text"] == "Question for the agent"

    def test_agent_message_never_targets_itself(self, client, workspace):
        """Agent messages mentioning themselves should not create self-loop targets."""
        for name in ["reviewer", "agent.alpha"]:
            client.post("/v1/join", json={
                "agent_name": name,
                "token": workspace["token"],
                "network": workspace["id"],
            })

        channel_name = workspace["channel"]["name"]
        anchor_id = _anchor_event_id(client, workspace, channel_name, "reviewer and assistant discuss this")
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "openagents:agent.alpha",
            "target": f"channel/{channel_name}",
            "payload": {
                "content": "reviewer and assistant should improve this together.",
                "reply_to": anchor_id,
            },
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})

        assert resp.status_code == 200
        targets = resp.json()["data"]["metadata"]["target_agents"]
        assert "agent.alpha" not in targets
        assert "reviewer" in targets

    def test_human_message_routes_to_master(self, client, workspace):
        """Human messages are routed to the channel master agent."""
        channel_name = workspace["channel"]["name"]
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user1",
            "target": f"channel/{channel_name}",
            "payload": {"content": "Hello agent!"},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})

        assert resp.status_code == 200
        data = resp.json()["data"]
        # workspace_mod should add target_agents with the channel master
        assert "target_agents" in data["metadata"]
        assert "agent-alpha" in data["metadata"]["target_agents"]


    def test_agent_message_master_no_targeting_in_single_agent_channel(self, client, workspace):
        """Master agent messages in single-agent channels have empty target_agents.

        With the LLM router, multi-agent routing uses the router.
        In single-agent channels (or when router is disabled), the fallback
        applies: master's own messages get no targeting.

        As of the routing fix: target_agents is ALWAYS set (to an empty
        list if nobody should respond) so clients don't fall through to
        broadcast-to-all on missing field.
        """
        channel_name = workspace["channel"]["name"]
        anchor_id = _anchor_event_id(client, workspace, channel_name)
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "openagents:agent-alpha",
            "target": f"channel/{channel_name}",
            "payload": {
                "content": "@agent-beta please review the code",
                "message_type": "chat",
                "reply_to": anchor_id,
            },
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})

        assert resp.status_code == 200
        data = resp.json()["data"]
        # Master's message in a single-agent channel — no real targets
        # (sentinel list, not missing, so legacy clients don't broadcast)
        assert data["metadata"].get("target_agents") == ["__no_response__"]

    def test_master_message_without_mentions_no_target_agents(self, client, workspace):
        """Master agent messages without mentions produce empty target_agents (no self-trigger)."""
        channel_name = workspace["channel"]["name"]
        anchor_id = _anchor_event_id(client, workspace, channel_name)
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "openagents:agent-alpha",  # agent-alpha is the channel master
            "target": f"channel/{channel_name}",
            "payload": {"content": "Just a status update", "reply_to": anchor_id},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})

        assert resp.status_code == 200
        data = resp.json()["data"]
        # Master's own messages should NOT trigger itself — sentinel
        # list (not missing field, not empty) so legacy clients skip.
        assert data["metadata"].get("target_agents") == ["__no_response__"]

    def test_repeated_long_agent_chat_is_blocked(self, client, workspace):
        """Exact repeated long agent messages are blocked to prevent watcher loops."""
        channel_name = workspace["channel"]["name"]
        anchor_id = _anchor_event_id(client, workspace, channel_name, "duplicate guard anchor")
        content = "This is a long repeated agent response that should only appear once in the session, because exact repeats are almost certainly a bad adapter replay loop."
        payload = {
            "type": "workspace.message.posted",
            "source": "openagents:agent-alpha",
            "target": f"channel/{channel_name}",
            "payload": {"content": content, "reply_to": anchor_id},
            "network": workspace["id"],
        }
        first = client.post("/v1/events", json=payload, headers={"X-Workspace-Token": workspace["token"]})
        assert first.status_code == 200
        second = client.post("/v1/events", json=payload, headers={"X-Workspace-Token": workspace["token"]})
        assert second.status_code == 400
        assert "duplicate_agent_message" in second.json()["message"]

    def test_repeated_long_agent_chat_allowed_for_different_anchor(self, client, workspace):
        """Deterministic replies to different prompts should not trip loop guard."""
        channel_name = workspace["channel"]["name"]
        first_anchor = _anchor_event_id(client, workspace, channel_name, "first prompt")
        second_anchor = _anchor_event_id(client, workspace, channel_name, "second prompt")
        content = "This is a long deterministic agent response that may be repeated when two separate prompts ask for the same summary or confirmation."
        first = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "openagents:agent-alpha",
            "target": f"channel/{channel_name}",
            "payload": {"content": content, "reply_to": first_anchor},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert first.status_code == 200
        second = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "openagents:agent-alpha",
            "target": f"channel/{channel_name}",
            "payload": {"content": content, "reply_to": second_anchor},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert second.status_code == 200

    def test_runtime_failure_text_is_blocked_from_agent_chat(self, client, workspace):
        """Runtime/internal failure text should not leak as visible agent chat."""
        channel_name = workspace["channel"]["name"]
        anchor_id = _anchor_event_id(client, workspace, channel_name, "overflow anchor")
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "openagents:agent-alpha",
            "target": f"channel/{channel_name}",
            "payload": {
                "content": "Context overflow: prompt too large for the model. Try /reset (or /new) to start a fresh session, or use a larger-context model.",
                "reply_to": anchor_id,
            },
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert resp.status_code == 400
        assert "runtime_failure_message" in resp.json()["message"]

    def test_member_message_without_mentions_routes_to_master(self, client, workspace):
        """Member agent messages without mentions route back to channel master."""
        # Add a member agent
        client.post("/v1/join", json={
            "agent_name": "agent-beta",
            "token": workspace["token"],
            "network": workspace["id"],
        })

        channel_name = workspace["channel"]["name"]
        anchor_id = _anchor_event_id(client, workspace, channel_name)
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "openagents:agent-beta",  # member, not master
            "target": f"channel/{channel_name}",
            "payload": {"content": "I finished the task.", "reply_to": anchor_id},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})

        assert resp.status_code == 200
        data = resp.json()["data"]
        # Member's response should be routed back to the master
        assert data["metadata"]["target_agents"] == ["agent-alpha"]

    def test_member_message_with_mention_routes_to_mentioned_agent(self, client, workspace):
        """Agent messages with explicit @mentions route to the mentioned agent."""
        # Add member agents to workspace (not to channel — so channel stays single-participant)
        for name in ["agent-beta", "agent-gamma"]:
            client.post("/v1/join", json={
                "agent_name": name,
                "token": workspace["token"],
                "network": workspace["id"],
            })

        channel_name = workspace["channel"]["name"]
        anchor_id = _anchor_event_id(client, workspace, channel_name)
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "openagents:agent-beta",
            "target": f"channel/{channel_name}",
            "payload": {"content": "@agent-gamma can you review this?", "reply_to": anchor_id},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})

        assert resp.status_code == 200
        data = resp.json()["data"]
        # Explicit @mention routes directly to the mentioned agent
        assert data["metadata"]["target_agents"] == ["agent-gamma"]

    def test_human_direct_address_routes_to_joined_agent_name(self, client, workspace):
        """Natural addressing uses the actual joined session agent id."""
        client.post("/v1/join", json={
            "agent_name": "reviewer",
            "token": workspace["token"],
            "network": workspace["id"],
        })

        channel_name = workspace["channel"]["name"]
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user1",
            "target": f"channel/{channel_name}",
            "payload": {"content": "reviewer are you here?"},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["metadata"]["target_agents"] == ["reviewer"]

    def test_human_mention_supports_dotted_agent_ids(self, client, workspace):
        """@agent.alpha should target the joined agent.alpha identity, not truncate at the dot."""
        client.post("/v1/join", json={
            "agent_name": "agent.alpha",
            "token": workspace["token"],
            "network": workspace["id"],
        })

        channel_name = workspace["channel"]["name"]
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user1",
            "target": f"channel/{channel_name}",
            "payload": {"content": "@agent.alpha can you see this?"},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["metadata"]["target_agents"] == ["agent.alpha"]

    def test_human_multi_name_message_targets_all_named_joined_agents(self, client, workspace):
        """Naming two session agents in plain text targets both delivery identities."""
        for name in ["reviewer", "agent.alpha"]:
            client.post("/v1/join", json={
                "agent_name": name,
                "token": workspace["token"],
                "network": workspace["id"],
            })

        channel_name = workspace["channel"]["name"]
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user1",
            "target": f"channel/{channel_name}",
            "payload": {"content": "reviewer and assistant please compare notes."},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert set(data["metadata"]["target_agents"]) == {"reviewer", "agent.alpha"}

    def test_human_display_name_alias_routes_to_stable_agent_name(self, client, workspace):
        """Display labels are aliases, but delivery still uses stable agent_name."""
        client.patch(
            f"/v1/workspaces/{workspace['id']}/members/agent-alpha",
            json={"display_name": "reviewer"},
            headers={"X-Workspace-Token": workspace["token"]},
        )

        channel_name = workspace["channel"]["name"]
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user1",
            "target": f"channel/{channel_name}",
            "payload": {"content": "reviewer are you here?"},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["metadata"]["target_agents"] == ["agent-alpha"]


class TestPollEvents:
    """GET /v1/events — poll events from a network."""

    def test_poll_empty_network(self, client, workspace):
        """Polling a new network returns empty list."""
        resp = client.get("/v1/events", params={"network": workspace["id"]},
                          headers={"X-Workspace-Token": workspace["token"]})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["events"] == []
        assert data["has_more"] is False

    def test_poll_after_send(self, client, workspace):
        """Events appear after being sent."""
        channel_name = workspace["channel"]["name"]
        # Send an event
        client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user1",
            "target": f"channel/{channel_name}",
            "payload": {"content": "msg1"},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})

        # Poll
        resp = client.get("/v1/events", params={"network": workspace["id"]},
                          headers={"X-Workspace-Token": workspace["token"]})
        assert resp.status_code == 200
        events = resp.json()["data"]["events"]
        assert len(events) == 1
        assert events[0]["payload"]["content"] == "msg1"

    def test_poll_filter_by_type(self, client, workspace):
        """Filter events by type prefix."""
        channel_name = workspace["channel"]["name"]
        # Send two different event types
        for etype in ("workspace.message.posted", "workspace.session.created"):
            client.post("/v1/events", json={
                "type": etype,
                "source": "human:user1",
                "target": f"channel/{channel_name}",
                "payload": {},
                "network": workspace["id"],
            }, headers={"X-Workspace-Token": workspace["token"]})

        # Filter by workspace.session
        resp = client.get("/v1/events", params={
            "network": workspace["id"],
            "type": "workspace.session",
        }, headers={"X-Workspace-Token": workspace["token"]})
        events = resp.json()["data"]["events"]
        assert len(events) == 1
        assert events[0]["type"] == "workspace.session.created"

    def test_poll_filter_by_target(self, client, workspace):
        """Filter events by target address."""
        channel_name = workspace["channel"]["name"]
        client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user1",
            "target": f"channel/{channel_name}",
            "payload": {},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})

        # Filter by exact target
        resp = client.get("/v1/events", params={
            "network": workspace["id"],
            "target": f"channel/{channel_name}",
        }, headers={"X-Workspace-Token": workspace["token"]})
        events = resp.json()["data"]["events"]
        assert len(events) == 1

        # Different target returns empty
        resp2 = client.get("/v1/events", params={
            "network": workspace["id"],
            "target": "channel/nonexistent",
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert resp2.json()["data"]["events"] == []

    def test_poll_cursor_pagination(self, client, workspace):
        """Cursor-based pagination with after parameter."""
        channel_name = workspace["channel"]["name"]
        # Send 3 events
        event_ids = []
        for i in range(3):
            resp = client.post("/v1/events", json={
                "type": "workspace.message.posted",
                "source": "human:user1",
                "target": f"channel/{channel_name}",
                "payload": {"content": f"msg{i}"},
                "network": workspace["id"],
            }, headers={"X-Workspace-Token": workspace["token"]})
            event_ids.append(resp.json()["data"]["id"])

        # Get first page (limit 2)
        resp = client.get("/v1/events", params={
            "network": workspace["id"],
            "limit": 2,
        }, headers={"X-Workspace-Token": workspace["token"]})
        data = resp.json()["data"]
        assert len(data["events"]) == 2
        assert data["has_more"] is True

        # Get second page using cursor
        resp2 = client.get("/v1/events", params={
            "network": workspace["id"],
            "after": data["events"][1]["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        data2 = resp2.json()["data"]
        assert len(data2["events"]) == 1
        assert data2["has_more"] is False

    def test_poll_invalid_network(self, client):
        """Polling nonexistent network returns 404."""
        resp = client.get("/v1/events", params={"network": "nonexistent"})
        assert resp.status_code == 404


class TestHandoffAttempts:
    """Durable handoff attempt endpoint."""

    def test_ack_creates_durable_handoff_attempt(self, client, workspace):
        """Ack writes should also materialize durable attempt rows."""
        channel_name = workspace["channel"]["name"]
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user",
            "target": f"channel/{channel_name}",
            "payload": {"content": "agent-alpha please respond"},
            "metadata": {"target_agents": ["agent-alpha"]},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert resp.status_code == 200
        event_id = resp.json()["data"]["id"]

        ack = client.post(f"/v1/events/{event_id}/ack", json={
            "network": workspace["id"],
            "agent_name": "agent-alpha",
            "status": "replied",
            "attempt_id": "attempt-1",
            "reply_message_id": "reply-123",
            "detail": "reply_event_id=reply-123; runtime=test",
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert ack.status_code == 200

        handoffs = client.get(
            f"/v1/events/{event_id}/handoffs",
            params={"network": workspace["id"]},
            headers={"X-Workspace-Token": workspace["token"]},
        )
        assert handoffs.status_code == 200
        data = handoffs.json()["data"]
        assert data["handoff_state"] == "complete"
        assert data["required_responses"] == ["agent-alpha"]
        assert len(data["attempts"]) == 1
        attempt = data["attempts"][0]
        assert attempt["message_id"] == event_id
        assert attempt["agent_name"] == "agent-alpha"
        assert attempt["attempt_id"] == "attempt-1"
        assert attempt["status"] == "replied"
        assert attempt["reply_message_id"] == "reply-123"

    def test_retryable_failed_attempt_does_not_complete_obligation_and_can_requeue(self, client, workspace):
        """A retryable failed attempt is terminal only for that attempt, then requeue creates a new obligation attempt."""
        channel_name = workspace["channel"]["name"]
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user",
            "target": f"channel/{channel_name}",
            "payload": {"content": "agent-alpha please retry if needed"},
            "metadata": {"target_agents": ["agent-alpha"]},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert resp.status_code == 200
        event_id = resp.json()["data"]["id"]

        failed = client.post(f"/v1/events/{event_id}/ack", json={
            "network": workspace["id"],
            "agent_name": "agent-alpha",
            "status": "failed",
            "attempt_id": "attempt-1",
            "retryable": True,
            "runtime": "openclaw",
            "worker_id": "worker-a",
            "error_code": "timeout",
            "error_detail": "adapter timed out",
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert failed.status_code == 200

        handoffs = client.get(
            f"/v1/events/{event_id}/handoffs",
            params={"network": workspace["id"]},
            headers={"X-Workspace-Token": workspace["token"]},
        )
        assert handoffs.status_code == 200
        data = handoffs.json()["data"]
        assert data["handoff_state"] != "complete"
        assert data["session_id"] == channel_name
        attempt = data["attempts"][0]
        assert attempt["target_agent"] == "agent-alpha"
        assert attempt["retryable"] is True
        assert attempt["runtime"] == "openclaw"
        assert attempt["worker_id"] == "worker-a"
        assert attempt["error_code"] == "timeout"
        assert attempt["terminal_at"] is not None

        requeued = client.post(f"/v1/events/{event_id}/handoffs/requeue", json={
            "network": workspace["id"],
            "agent_name": "agent-alpha",
            "from_attempt_id": "attempt-1",
            "attempt_id": "attempt-2",
            "detail": "manual retry",
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert requeued.status_code == 200
        assert requeued.json()["data"]["attempt_id"] == "attempt-2"

        handoffs = client.get(
            f"/v1/events/{event_id}/handoffs",
            params={"network": workspace["id"]},
            headers={"X-Workspace-Token": workspace["token"]},
        )
        attempts = handoffs.json()["data"]["attempts"]
        assert [row["attempt_id"] for row in attempts] == ["attempt-1", "attempt-2"]
        assert attempts[0]["superseded_by_attempt_id"] == "attempt-2"
        assert attempts[1]["status"] == "queued"

    def test_processing_attempt_with_expired_lease_becomes_stalled(self, client, workspace):
        """Processing without lease renewal should surface as stalled."""
        channel_name = workspace["channel"]["name"]
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user",
            "target": f"channel/{channel_name}",
            "payload": {"content": "agent-alpha please process"},
            "metadata": {"target_agents": ["agent-alpha"]},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert resp.status_code == 200
        event_id = resp.json()["data"]["id"]

        ack = client.post(f"/v1/events/{event_id}/ack", json={
            "network": workspace["id"],
            "agent_name": "agent-alpha",
            "status": "processing",
            "attempt_id": "attempt-1",
            "lease_expires_at": "2000-01-01T00:00:00Z",
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert ack.status_code == 200

        handoffs = client.get(
            f"/v1/events/{event_id}/handoffs",
            params={"network": workspace["id"]},
            headers={"X-Workspace-Token": workspace["token"]},
        )
        assert handoffs.status_code == 200
        data = handoffs.json()["data"]
        assert data["attempts"][0]["status"] == "stalled"
