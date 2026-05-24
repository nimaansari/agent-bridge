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

    def test_explicit_target_agents_are_preserved_as_privileged_routing_input(self, client, workspace):
        """Trusted explicit target_agents are preserved and not recomputed from transcript text."""
        channel_name = workspace["channel"]["name"]
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user1",
            "target": f"channel/{channel_name}",
            "payload": {"content": "hello master, but route this to reviewer"},
            "metadata": {"target_agents": ["reviewer"]},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["metadata"]["target_agents"] == ["reviewer"]

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

    def test_agent_message_without_handoff_does_not_wake_teammate(self, client, workspace):
        """Plain agent chat does not wake teammates without structured handoff metadata."""
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
        metadata = resp.json()["data"]["metadata"]
        assert metadata["target_agents"] == ["__no_response__"]
        assert metadata["loop_guard"] == "needs_reply_required"

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
        assert data["metadata"]["reply_required"] is True
        assert data["metadata"]["reply_responsible"] == ["human:user"]
        assert data["metadata"]["reply_state"] == "pending"

    def test_anchored_reply_closes_reply_accountability(self, client, workspace):
        """Every message is accountable until somebody replies, including humans."""
        channel_name = workspace["channel"]["name"]
        anchor_id = _anchor_event_id(client, workspace, channel_name)
        agent_msg = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "openagents:agent-alpha",
            "target": f"channel/{channel_name}",
            "payload": {"content": "Done. Please confirm.", "reply_to": anchor_id},
            "metadata": {"needs_reply": False},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert agent_msg.status_code == 200
        agent_id = agent_msg.json()["data"]["id"]
        assert agent_msg.json()["data"]["metadata"]["reply_responsible"] == ["human:user"]

        human_reply = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user1",
            "target": f"channel/{channel_name}",
            "payload": {"content": "Confirmed", "reply_to": agent_id},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert human_reply.status_code == 200

        poll = client.get("/v1/events", params={
            "network": workspace["id"],
            "channel": channel_name,
            "type": "workspace.message.posted",
        }, headers={"X-Workspace-Token": workspace["token"]})
        original = next(event for event in poll.json()["data"]["events"] if event["id"] == agent_id)
        assert original["metadata"]["reply_state"] == "complete"
        assert original["metadata"]["reply_answered_by"] == "human:user"

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

    def test_member_message_without_handoff_does_not_route_to_master(self, client, workspace):
        """Member agent chat does not bounce to the master without structured handoff."""
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
        assert data["metadata"]["target_agents"] == ["__no_response__"]
        assert data["metadata"]["loop_guard"] == "needs_reply_required"

    def test_member_message_with_mention_still_requires_structured_handoff(self, client, workspace):
        """An agent @mention alone is not enough; structured handoff is required."""
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
        assert data["metadata"]["target_agents"] == ["__no_response__"]
        assert data["metadata"]["loop_guard"] == "needs_reply_required"

    def test_human_bare_direct_address_falls_back_to_master(self, client, workspace):
        """Bare human naming does not retarget; without @mention it falls back to the master."""
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
        assert data["metadata"]["target_agents"] == ["agent-alpha"]

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

    def test_human_bare_names_do_not_wake_multiple_agents(self, client, workspace):
        """Bare agent names do not wake agents; human routing must use explicit @mentions."""
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
        assert data["metadata"]["target_agents"] == ["agent-alpha"]

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
            "metadata": {
                "target_agents": ["agent-alpha"],
                "response_required": True,
                "required_responses": ["agent-alpha"],
                "handoff_state": "pending",
            },
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


    def test_replied_ack_repairs_same_attempt_after_failed_ack(self, client, workspace):
        """A real reply must win over an earlier failed ack for the same adapter attempt."""
        channel_name = workspace["channel"]["name"]
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user",
            "target": f"channel/{channel_name}",
            "payload": {"content": "agent-alpha answer after transient failure"},
            "metadata": {"target_agents": ["agent-alpha"]},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert resp.status_code == 200
        event_id = resp.json()["data"]["id"]

        failed = client.post(f"/v1/events/{event_id}/ack", json={
            "network": workspace["id"],
            "agent_name": "agent-alpha",
            "status": "failed",
            "attempt_id": "default",
            "retryable": True,
            "detail": "transient gateway failure",
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert failed.status_code == 200

        replied = client.post(f"/v1/events/{event_id}/ack", json={
            "network": workspace["id"],
            "agent_name": "agent-alpha",
            "status": "replied",
            "attempt_id": "default",
            "reply_message_id": "reply-456",
            "detail": "reply_event_id=reply-456; runtime=openclaw",
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert replied.status_code == 200

        handoffs = client.get(
            f"/v1/events/{event_id}/handoffs",
            params={"network": workspace["id"]},
            headers={"X-Workspace-Token": workspace["token"]},
        )
        assert handoffs.status_code == 200
        attempt = handoffs.json()["data"]["attempts"][0]
        assert attempt["status"] == "replied"
        assert attempt["reply_message_id"] == "reply-456"

    def test_retryable_failed_attempt_does_not_complete_obligation_and_can_requeue(self, client, workspace):
        """A retryable failed attempt is terminal only for that attempt, then requeue creates a new obligation attempt."""
        channel_name = workspace["channel"]["name"]
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user",
            "target": f"channel/{channel_name}",
            "payload": {"content": "agent-alpha please retry if needed"},
            "metadata": {
                "target_agents": ["agent-alpha"],
                "response_required": True,
                "required_responses": ["agent-alpha"],
                "handoff_state": "pending",
            },
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

    def test_stale_ack_cannot_regress_terminal_attempt(self, client, workspace):
        """A replayed processing/failed ack must not reopen a completed handoff."""
        channel_name = workspace["channel"]["name"]
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user",
            "target": f"channel/{channel_name}",
            "payload": {"content": "agent-alpha please respond"},
            "metadata": {
                "target_agents": ["agent-alpha"],
                "response_required": True,
                "required_responses": ["agent-alpha"],
                "handoff_state": "pending",
            },
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert resp.status_code == 200
        event_id = resp.json()["data"]["id"]

        replied = client.post(f"/v1/events/{event_id}/ack", json={
            "network": workspace["id"],
            "agent_name": "agent-alpha",
            "status": "replied",
            "attempt_id": "attempt-1",
            "reply_message_id": "reply-123",
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert replied.status_code == 200

        stale = client.post(f"/v1/events/{event_id}/ack", json={
            "network": workspace["id"],
            "agent_name": "agent-alpha",
            "status": "processing",
            "attempt_id": "attempt-1",
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert stale.status_code == 200

        handoffs = client.get(
            f"/v1/events/{event_id}/handoffs",
            params={"network": workspace["id"]},
            headers={"X-Workspace-Token": workspace["token"]},
        )
        data = handoffs.json()["data"]
        assert data["handoff_state"] == "complete"
        assert data["attempts"][0]["status"] == "replied"
        assert data["attempts"][0]["reply_message_id"] == "reply-123"

        inbox = client.get(
            "/v1/agents/agent-alpha/inbox",
            params={"network": workspace["id"], "channel": channel_name},
            headers={"X-Workspace-Token": workspace["token"]},
        )
        assert inbox.status_code == 200
        assert inbox.json()["data"]["events"] == []


class TestAgentInbox:
    """Per-agent actionable inbox."""

    def test_agent_inbox_filters_mixed_room_transcript(self, client, workspace):
        channel_name = workspace["channel"]["name"]
        # Noise: unrelated human message for another agent, self-message, other-agent target.
        client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user",
            "target": f"channel/{channel_name}",
            "payload": {"content": "general chat"},
            "metadata": {"target_agents": ["reviewer"]},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "openagents:agent-alpha",
            "target": f"channel/{channel_name}",
            "payload": {"content": "self note"},
            "metadata": {"target_agents": ["agent-alpha"]},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user",
            "target": f"channel/{channel_name}",
            "payload": {"content": "other target"},
            "metadata": {"target_agents": ["reviewer"]},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        actionable = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user",
            "target": f"channel/{channel_name}",
            "payload": {"content": "agent-alpha do this"},
            "metadata": {"target_agents": ["agent-alpha"]},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert actionable.status_code == 200
        event_id = actionable.json()["data"]["id"]

        inbox = client.get(
            "/v1/agents/agent-alpha/inbox",
            params={"network": workspace["id"], "channel": channel_name},
            headers={"X-Workspace-Token": workspace["token"]},
        )
        assert inbox.status_code == 200
        events = inbox.json()["data"]["events"]
        assert [event["id"] for event in events] == [event_id]

    def test_agent_inbox_excludes_terminal_handoff(self, client, workspace):
        channel_name = workspace["channel"]["name"]
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user",
            "target": f"channel/{channel_name}",
            "payload": {"content": "agent-alpha do this"},
            "metadata": {"target_agents": ["agent-alpha"]},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        event_id = resp.json()["data"]["id"]
        client.post(f"/v1/events/{event_id}/ack", json={
            "network": workspace["id"],
            "agent_name": "agent-alpha",
            "status": "replied",
            "attempt_id": "attempt-1",
        }, headers={"X-Workspace-Token": workspace["token"]})

        inbox = client.get(
            "/v1/agents/agent-alpha/inbox",
            params={"network": workspace["id"], "channel": channel_name},
            headers={"X-Workspace-Token": workspace["token"]},
        )
        assert inbox.status_code == 200
        assert inbox.json()["data"]["events"] == []

class TestManagerModeRouting:
    def _enable_manager_mode(self, client, workspace, active_task="Ship release"):
        resp = client.patch(f"/v1/workspaces/{workspace["id"]}", json={
            "settings": {
                "agent_collaboration_mode": "manager",
                "session_manager_agent": "manager-bot",
                "active_task": active_task,
            },
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert resp.status_code == 200
        for name, role in [("manager-bot", "session_manager"), ("worker-bot", "member")]:
            join = client.post("/v1/join", json={
                "agent_name": name,
                "token": workspace["token"],
                "network": workspace["id"],
            })
            assert join.status_code == 200
            patch = client.patch(
                f"/v1/workspaces/{workspace["id"]}/members/{name}",
                json={"role": role},
                headers={"X-Workspace-Token": workspace["token"]},
            )
            assert patch.status_code == 200
        return workspace["channel"]["name"]

    def test_assign_to_worker_is_actionable_without_semantic_handoff_fields(self, client, workspace):
        channel_name = self._enable_manager_mode(client, workspace)
        anchor_id = _anchor_event_id(client, workspace, channel_name, "Manager thread")
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "openagents:manager-bot",
            "target": f"channel/{channel_name}",
            "payload": {"content": "@worker-bot handle the release", "reply_to": anchor_id},
            "metadata": {"needs_reply": True},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["metadata"]["target_agents"] == ["worker-bot"]
        assert data["metadata"]["response_required"] is True
        assert data["metadata"]["active_task"] == "Ship release"
        assert "required_responses" not in data["metadata"]
        assert "handoff_state" not in data["metadata"]

        inbox = client.get(
            "/v1/agents/worker-bot/inbox",
            params={"network": workspace["id"], "channel": channel_name},
            headers={"X-Workspace-Token": workspace["token"]},
        )
        assert [event["id"] for event in inbox.json()["data"]["events"]] == [data["id"]]

    def test_worker_reply_and_complete_clear_actionable_and_keep_manager_target(self, client, workspace):
        channel_name = self._enable_manager_mode(client, workspace)
        anchor_id = _anchor_event_id(client, workspace, channel_name, "Manager thread")
        assign = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "openagents:manager-bot",
            "target": f"channel/{channel_name}",
            "payload": {"content": "@worker-bot do the task", "reply_to": anchor_id},
            "metadata": {"needs_reply": True},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        event_id = assign.json()["data"]["id"]

        reply = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "openagents:worker-bot",
            "target": f"channel/{channel_name}",
            "payload": {"content": "Done, please review", "reply_to": event_id},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert reply.status_code == 200
        reply_meta = reply.json()["data"]["metadata"]
        assert reply_meta["target_agents"] == ["manager-bot"]
        assert reply_meta["response_required"] is True
        assert reply_meta["routed_to_session_manager"] is True
        assert "required_responses" not in reply_meta
        assert "handoff_state" not in reply_meta

        ack = client.post(f"/v1/events/{event_id}/ack", json={
            "network": workspace["id"],
            "agent_name": "worker-bot",
            "status": "replied",
            "attempt_id": "attempt-1",
            "reply_message_id": reply.json()["data"]["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert ack.status_code == 200

        inbox = client.get(
            "/v1/agents/worker-bot/inbox",
            params={"network": workspace["id"], "channel": channel_name},
            headers={"X-Workspace-Token": workspace["token"]},
        )
        assert inbox.json()["data"]["events"] == []

    def test_blocked_or_failed_worker_update_clears_actionable(self, client, workspace):
        channel_name = self._enable_manager_mode(client, workspace)
        anchor_id = _anchor_event_id(client, workspace, channel_name, "Manager thread")
        assign = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "openagents:manager-bot",
            "target": f"channel/{channel_name}",
            "payload": {"content": "@worker-bot do the task", "reply_to": anchor_id},
            "metadata": {"needs_reply": True},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        event_id = assign.json()["data"]["id"]

        failed = client.post(f"/v1/events/{event_id}/ack", json={
            "network": workspace["id"],
            "agent_name": "worker-bot",
            "status": "failed",
            "attempt_id": "attempt-1",
            "retryable": False,
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert failed.status_code == 200

        inbox = client.get(
            "/v1/agents/worker-bot/inbox",
            params={"network": workspace["id"], "channel": channel_name},
            headers={"X-Workspace-Token": workspace["token"]},
        )
        assert inbox.json()["data"]["events"] == []

    def test_human_mentions_and_replies_do_not_steal_owner_in_manager_mode(self, client, workspace):
        channel_name = self._enable_manager_mode(client, workspace)
        human = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user1",
            "target": f"channel/{channel_name}",
            "payload": {"content": "@worker-bot can you do this?"},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert human.status_code == 200
        assert human.json()["data"]["metadata"]["target_agents"] == ["worker-bot"]

        manager_msg = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "openagents:manager-bot",
            "target": f"channel/{channel_name}",
            "payload": {"content": "@worker-bot work it", "reply_to": human.json()["data"]["id"]},
            "metadata": {"needs_reply": True},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        assign_id = manager_msg.json()["data"]["id"]

        human_reply = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "human:user1",
            "target": f"channel/{channel_name}",
            "payload": {"content": "any update?", "reply_to": assign_id},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert human_reply.status_code == 200
        assert human_reply.json()["data"]["metadata"]["target_agents"] == ["manager-bot"]

    def test_manager_mode_off_keeps_legacy_handoff_fields(self, client, workspace):
        channel_name = workspace["channel"]["name"]
        client.post("/v1/join", json={"agent_name": "reviewer", "token": workspace["token"], "network": workspace["id"]})
        patch = client.patch(
            f"/v1/workspaces/{workspace["id"]}/members/reviewer",
            json={"role": "session_manager"},
            headers={"X-Workspace-Token": workspace["token"]},
        )
        assert patch.status_code == 200
        anchor_id = _anchor_event_id(client, workspace, channel_name, "legacy")
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "openagents:reviewer",
            "target": f"channel/{channel_name}",
            "payload": {"content": "@agent-alpha please handle this", "reply_to": anchor_id},
            "metadata": {"needs_reply": True},
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert resp.status_code == 200
        meta = resp.json()["data"]["metadata"]
        assert meta["required_responses"] == ["agent-alpha"]
        assert meta["handoff_state"] == "pending"

    def test_manager_mode_semantically_kills_required_response_fields(self, client, workspace):
        channel_name = self._enable_manager_mode(client, workspace)
        anchor_id = _anchor_event_id(client, workspace, channel_name, "Manager thread")
        resp = client.post("/v1/events", json={
            "type": "workspace.message.posted",
            "source": "openagents:manager-bot",
            "target": f"channel/{channel_name}",
            "payload": {"content": "@worker-bot take this", "reply_to": anchor_id},
            "metadata": {
                "needs_reply": True,
                "required_responses": ["worker-bot"],
                "handoff_state": "pending",
            },
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        meta = resp.json()["data"]["metadata"]
        assert "required_responses" not in meta
        assert "handoff_state" not in meta
