# -*- coding: utf-8 -*-
"""
Tests for workspace CRUD endpoints.
"""

import pytest


class TestCreateWorkspace:
    """POST /v1/workspaces — create a workspace."""

    def test_create_workspace(self, client):
        """Create workspace returns ID, slug, token, and default channel."""
        resp = client.post("/v1/workspaces", json={
            "name": "My Workspace",
            "agent_name": "test-agent",
        })
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "workspaceId" in data
        assert "slug" in data
        assert "token" in data
        assert "channel" in data
        assert data["name"] == "My Workspace"

    def test_create_workspace_has_channel_with_master(self, client):
        """Default channel has the creating agent as master and participant."""
        resp = client.post("/v1/workspaces", json={
            "name": "Test",
            "agent_name": "agent-alpha",
        })
        channel = resp.json()["data"]["channel"]
        assert channel["masterAgent"] == "agent-alpha"
        assert "agent-alpha" in channel["participants"]

    def test_create_workspace_without_agent_starts_empty(self, client):
        """New human-created sessions do not automatically add an agent."""
        resp = client.post("/v1/workspaces", json={"name": "Empty Session"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["channel"]["masterAgent"] is None
        assert data["channel"]["participants"] == []

        detail = client.get(
            f"/v1/workspaces/{data['workspaceId']}",
            headers={"X-Workspace-Token": data["token"]},
        )
        assert detail.json()["data"]["agents"] == []

    def test_create_workspace_with_email(self, client):
        """Creator email is stored."""
        resp = client.post("/v1/workspaces", json={
            "name": "Test",
            "agent_name": "agent-alpha",
            "creator_email": "user@example.com",
        })
        data = resp.json()["data"]
        ws_id = data["workspaceId"]
        detail = client.get(f"/v1/workspaces/{ws_id}",
                            headers={"X-Workspace-Token": data["token"]})
        assert detail.json()["data"]["creatorEmail"] == "user@example.com"


class TestGetWorkspace:
    """GET /v1/workspaces/{id} — get workspace details."""

    def test_get_workspace_by_id(self, client, workspace):
        """Fetch workspace by ID."""
        resp = client.get(f"/v1/workspaces/{workspace['id']}",
                          headers={"X-Workspace-Token": workspace["token"]})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["workspaceId"] == workspace["id"]
        assert data["name"] == workspace["name"]

    def test_get_workspace_by_slug(self, client, workspace):
        """Fetch workspace by slug."""
        resp = client.get(f"/v1/workspaces/{workspace['slug']}",
                          headers={"X-Workspace-Token": workspace["token"]})
        assert resp.status_code == 200
        assert resp.json()["data"]["workspaceId"] == workspace["id"]

    def test_get_workspace_includes_agents(self, client, workspace):
        """Workspace detail includes agent list."""
        resp = client.get(f"/v1/workspaces/{workspace['id']}",
                          headers={"X-Workspace-Token": workspace["token"]})
        agents = resp.json()["data"]["agents"]
        assert len(agents) >= 1
        assert agents[0]["agentName"] == "agent-alpha"
        assert agents[0]["role"] == "master"

    def test_get_nonexistent_workspace(self, client):
        """Nonexistent workspace returns 404."""
        resp = client.get("/v1/workspaces/nonexistent")
        assert resp.status_code == 404


class TestUpdateWorkspace:
    """PATCH /v1/workspaces/{id} — update workspace."""

    def test_update_name(self, client, workspace):
        """Update workspace name."""
        resp = client.patch(f"/v1/workspaces/{workspace['id']}", json={
            "name": "Updated Name",
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert resp.status_code == 200
        assert resp.json()["data"]["name"] == "Updated Name"

    def test_update_settings(self, client, workspace):
        """Update workspace settings."""
        resp = client.patch(f"/v1/workspaces/{workspace['id']}", json={
            "settings": {"theme": "dark"},
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert resp.status_code == 200
        assert resp.json()["data"]["settings"]["theme"] == "dark"

    def test_update_settings_strips_deleted_depth_keys(self, client, workspace):
        """Deleted depth settings are discarded on write and never echoed back."""
        resp = client.patch(f"/v1/workspaces/{workspace['id']}", json={
            "settings": {
                "theme": "dark",
                "agent_reply_budget": 9,
                "max_agent_reply_depth": 11,
            },
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert resp.status_code == 200
        settings = resp.json()["data"]["settings"]
        assert settings["theme"] == "dark"
        assert "agent_reply_budget" not in settings
        assert "max_agent_reply_depth" not in settings
        policy = resp.json()["data"]["collaborationPolicy"]
        assert "agentReplyBudget" not in policy

    def test_get_workspace_does_not_echo_deleted_depth_fields(self, client, workspace):
        """Workspace detail omits deleted depth fields from settings and policy."""
        client.patch(f"/v1/workspaces/{workspace['id']}", json={
            "settings": {
                "agent_reply_budget": 3,
                "max_agent_reply_depth": 4,
                "require_needs_reply_for_agent_wake": True,
            },
        }, headers={"X-Workspace-Token": workspace["token"]})
        resp = client.get(f"/v1/workspaces/{workspace['id']}", headers={"X-Workspace-Token": workspace["token"]})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "agent_reply_budget" not in data["settings"]
        assert "max_agent_reply_depth" not in data["settings"]
        assert "agentReplyBudget" not in data["collaborationPolicy"]


class TestDeleteWorkspace:
    """DELETE /v1/workspaces/{id} — soft-delete workspace."""

    def test_delete_workspace(self, client, workspace):
        """Soft-delete sets status to 'deleted'."""
        resp = client.delete(
            f"/v1/workspaces/{workspace['id']}",
            headers={"X-Workspace-Token": workspace["token"]},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "deleted"

    def test_deleted_workspace_hidden_from_list(self, client, workspace):
        """Deleted workspace doesn't appear in list."""
        client.delete(
            f"/v1/workspaces/{workspace['id']}",
            headers={"X-Workspace-Token": workspace["token"]},
        )
        resp = client.get("/v1/workspaces")
        ids = [w["workspaceId"] for w in resp.json()["data"]]
        assert workspace["id"] not in ids

    # ------------------------------------------------------------------
    # Auth enforcement (CVE-1)
    # ------------------------------------------------------------------

    def test_delete_no_credentials_returns_401(self, client, workspace):
        """Unauthenticated DELETE is rejected — workspace must not be deleted."""
        resp = client.delete(f"/v1/workspaces/{workspace['id']}")
        assert resp.status_code == 401

        # Workspace must still exist
        get = client.get(
            f"/v1/workspaces/{workspace['id']}",
            headers={"X-Workspace-Token": workspace["token"]},
        )
        assert get.status_code == 200

    def test_delete_wrong_token_returns_401(self, client, workspace):
        """Wrong token is rejected — workspace must not be deleted."""
        resp = client.delete(
            f"/v1/workspaces/{workspace['id']}",
            headers={"X-Workspace-Token": "not-the-right-token"},
        )
        assert resp.status_code == 401

    def test_delete_by_slug_with_valid_token(self, client, workspace):
        """Deletion by slug also works with a valid token."""
        resp = client.delete(
            f"/v1/workspaces/{workspace['slug']}",
            headers={"X-Workspace-Token": workspace["token"]},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "deleted"

    def test_delete_nonexistent_workspace_returns_404(self, client):
        """Deleting a nonexistent workspace returns 404 regardless of token."""
        resp = client.delete(
            "/v1/workspaces/00000000-0000-0000-0000-000000000000",
            headers={"X-Workspace-Token": "any-token"},
        )
        assert resp.status_code == 404

    def test_deleted_workspace_is_no_longer_accessible(self, client, workspace):
        """After deletion the workspace returns 404 on GET."""
        client.delete(
            f"/v1/workspaces/{workspace['id']}",
            headers={"X-Workspace-Token": workspace["token"]},
        )
        resp = client.get(
            f"/v1/workspaces/{workspace['id']}",
            headers={"X-Workspace-Token": workspace["token"]},
        )
        assert resp.status_code == 404

    def test_delete_already_deleted_workspace_returns_404(self, client, workspace):
        """A second DELETE on an already-deleted workspace returns 404."""
        headers = {"X-Workspace-Token": workspace["token"]}
        client.delete(f"/v1/workspaces/{workspace['id']}", headers=headers)
        resp = client.delete(f"/v1/workspaces/{workspace['id']}", headers=headers)
        assert resp.status_code == 404


class TestListWorkspaces:
    """GET /v1/workspaces — list workspaces."""

    def test_list_empty(self, client):
        """Empty workspace list."""
        resp = client.get("/v1/workspaces")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    def test_list_returns_workspaces(self, client, workspace):
        """Workspaces appear in list."""
        resp = client.get("/v1/workspaces")
        assert len(resp.json()["data"]) >= 1

    def test_list_filter_by_agent(self, client, workspace):
        """Filter workspaces by agent membership."""
        resp = client.get("/v1/workspaces", params={"agent_name": "agent-alpha"})
        assert len(resp.json()["data"]) >= 1

        resp2 = client.get("/v1/workspaces", params={"agent_name": "nonexistent"})
        assert resp2.json()["data"] == []


class TestRotateToken:
    """POST /v1/workspaces/{id}/rotate-token — rotate workspace token."""

    def test_rotate_with_valid_token(self, client, workspace):
        """Rotating with current token returns a new token."""
        resp = client.post(
            f"/v1/workspaces/{workspace['id']}/rotate-token",
            headers={"X-Workspace-Token": workspace["token"]},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "token" in data
        assert data["token"] != workspace["token"]
        assert data["workspace_id"] == workspace["id"]

    def test_old_token_stops_working(self, client, workspace):
        """After rotation, the old token should no longer work."""
        old_token = workspace["token"]
        resp = client.post(
            f"/v1/workspaces/{workspace['id']}/rotate-token",
            headers={"X-Workspace-Token": old_token},
        )
        new_token = resp.json()["data"]["token"]

        # Old token should fail
        resp2 = client.post(
            f"/v1/workspaces/{workspace['id']}/rotate-token",
            headers={"X-Workspace-Token": old_token},
        )
        assert resp2.status_code == 401

        # New token should work
        resp3 = client.post(
            f"/v1/workspaces/{workspace['id']}/rotate-token",
            headers={"X-Workspace-Token": new_token},
        )
        assert resp3.status_code == 200

    def test_rotate_no_credentials(self, client, workspace):
        """Rotation without credentials returns 401."""
        resp = client.post(f"/v1/workspaces/{workspace['id']}/rotate-token")
        assert resp.status_code == 401

    def test_rotate_nonexistent_workspace(self, client):
        """Rotation on nonexistent workspace returns 404."""
        resp = client.post(
            "/v1/workspaces/nonexistent/rotate-token",
            headers={"X-Workspace-Token": "any"},
        )
        assert resp.status_code == 404

    def test_new_token_works_for_join(self, client, workspace):
        """After rotation, agents can join using the new token."""
        resp = client.post(
            f"/v1/workspaces/{workspace['id']}/rotate-token",
            headers={"X-Workspace-Token": workspace["token"]},
        )
        new_token = resp.json()["data"]["token"]

        # Join with new token
        join_resp = client.post("/v1/join", json={
            "agent_name": "new-agent",
            "token": new_token,
            "network": workspace["id"],
        })
        assert join_resp.status_code == 200


class TestRemoveMember:
    """DELETE /v1/workspaces/{id}/members/{agent_name} — remove member."""

    def test_remove_member(self, client, workspace):
        """Remove an agent from workspace."""
        # Join an agent first
        client.post("/v1/join", json={
            "agent_name": "agent-to-remove",
            "token": workspace["token"],
            "network": workspace["id"],
        })

        # Remove it
        resp = client.delete(
            f"/v1/workspaces/{workspace['id']}/members/agent-to-remove",
            headers={"X-Workspace-Token": workspace["token"]},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["removed"] is True
        assert resp.json()["data"]["channels_removed"] >= 1

        # Verify agent no longer in discover or channel participants
        disc = client.get("/v1/discover", params={"network": workspace["id"]},
                          headers={"X-Workspace-Token": workspace["token"]})
        data = disc.json()["data"]
        names = [a["address"] for a in data["agents"]]
        assert "openagents:agent-to-remove" not in names
        for channel in data["channels"]:
            assert "agent-to-remove" not in channel["participants"]
            assert channel["master"] != "agent-to-remove"

    def test_remove_nonexistent_member(self, client, workspace):
        """Removing nonexistent member returns 404."""
        resp = client.delete(
            f"/v1/workspaces/{workspace['id']}/members/nonexistent-agent",
            headers={"X-Workspace-Token": workspace["token"]},
        )
        assert resp.status_code == 404

    def test_remove_no_credentials(self, client, workspace):
        """Removal without credentials returns 401."""
        resp = client.delete(
            f"/v1/workspaces/{workspace['id']}/members/agent-alpha",
        )
        assert resp.status_code == 401

class TestManagerModeWorkspace:
    def test_collaboration_policy_reuses_session_manager_and_active_task(self, client, workspace):
        resp = client.patch(f"/v1/workspaces/{workspace["id"]}", json={
            "settings": {
                "agent_collaboration_mode": "manager",
                "session_manager_agent": "manager-bot",
                "active_task": "Ship release",
            },
        }, headers={"X-Workspace-Token": workspace["token"]})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["settings"]["session_manager_agent"] == "manager-bot"
        assert data["settings"]["active_task"] == "Ship release"
        policy = data["collaborationPolicy"]
        assert policy["mode"] == "manager"
        assert policy["sessionManagerAgent"] == "manager-bot"

    def test_update_member_sets_current_task_and_status(self, client, workspace):
        resp = client.patch(
            f"/v1/workspaces/{workspace["id"]}/members/agent-alpha",
            json={"current_task": "Review PR", "task_status": "working", "role": "session_manager"},
            headers={"X-Workspace-Token": workspace["token"]},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["currentTask"] == "Review PR"
        assert data["taskStatus"] == "working"
        assert data["role"] == "session_manager"

        detail = client.get(
            f"/v1/workspaces/{workspace["id"]}",
            headers={"X-Workspace-Token": workspace["token"]},
        )
        member = next(agent for agent in detail.json()["data"]["agents"] if agent["agentName"] == "agent-alpha")
        assert member["currentTask"] == "Review PR"
        assert member["taskStatus"] == "working"
