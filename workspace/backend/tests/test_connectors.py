# -*- coding: utf-8 -*-
"""Tests for durable connector enrollment and runtime session mapping."""


class TestConnectors:
    def test_enroll_connector_and_list_capabilities(self, client, workspace):
        resp = client.put("/v1/connectors/agent-beta", json={
            "network": workspace["id"],
            "runtime_type": "openclaw",
            "command_template": "openclaw run {{session_id}}",
            "supports_threads": True,
            "supports_reply_anchor": True,
            "supports_files": True,
            "supports_seen_ack": True,
            "supports_processing_ack": True,
            "supports_cancel": True,
            "supports_freeze": True,
            "worker_id": "worker-1",
            "metadata": {"host": "test"},
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert resp.status_code == 200
        connector = resp.json()["data"]["connector"]
        assert connector["agent_name"] == "agent-beta"
        assert connector["runtime_type"] == "openclaw"
        assert connector["capabilities"]["supports_threads"] is True
        assert connector["capabilities"]["supports_reply_anchor"] is True
        assert connector["enabled"] is True

        listed = client.get("/v1/connectors", params={"network": workspace["id"]}, headers={
            "X-Workspace-Token": workspace["token"],
        })
        assert listed.status_code == 200
        assert [row["agent_name"] for row in listed.json()["data"]["connectors"]] == ["agent-beta"]

    def test_connector_heartbeat_updates_health(self, client, workspace):
        client.put("/v1/connectors/agent-beta", json={
            "network": workspace["id"],
            "runtime_type": "hermes",
        }, headers={"X-Workspace-Token": workspace["token"]})

        resp = client.post("/v1/connectors/agent-beta/heartbeat", json={
            "network": workspace["id"],
            "worker_id": "hermes-worker-7",
            "status": "online",
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert resp.status_code == 200
        connector = resp.json()["data"]["connector"]
        assert connector["status"] == "online"
        assert connector["worker_id"] == "hermes-worker-7"
        assert connector["last_heartbeat"] is not None

    def test_runtime_session_mapping_is_durable_per_session_agent(self, client, workspace):
        session_id = workspace["channel"]["name"]
        resp = client.put(f"/v1/sessions/{session_id}/agents/agent-beta/runtime-session", json={
            "network": workspace["id"],
            "runtime_session_id": "openclaw-session-123",
            "runtime_type": "openclaw",
            "cursor": "evt_10",
            "last_seq": 10,
            "metadata": {"resume": True},
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert resp.status_code == 200
        mapping = resp.json()["data"]["runtime_session"]
        assert mapping["session_id"] == session_id
        assert mapping["agent_name"] == "agent-beta"
        assert mapping["runtime_session_id"] == "openclaw-session-123"
        assert mapping["cursor"] == "evt_10"
        assert mapping["last_seq"] == 10

        listed = client.get(f"/v1/sessions/{session_id}/runtime-sessions", params={
            "network": workspace["id"],
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert listed.status_code == 200
        rows = listed.json()["data"]["runtime_sessions"]
        assert len(rows) == 1
        assert rows[0]["runtime_session_id"] == "openclaw-session-123"
