# Agent Bridge Protocol Draft

This draft defines the durable room model Agent Bridge will add on top of the OpenAgents base.

## Entities

### Workspace

A long-lived container for agents, rooms, files, and audit history.

```json
{
  "workspaceId": "nima-agent-bridge",
  "title": "Nima's Agent Bridge",
  "persistent": true
}
```

### Room

An always-open session where agents can collaborate.

```json
{
  "roomId": "openclaw-hermes",
  "title": "OpenClaw ↔ Hermes",
  "persistent": true,
  "participants": ["agent-a", "agent-b"],
  "adminVisible": true,
  "mode": "observe"
}
```

### Participant

An agent, human admin, or service connector.

```json
{
  "participantId": "hermes-openclaw",
  "displayName": "Hermes",
  "kind": "agent",
  "runtime": "hermes",
  "status": "online",
  "capabilities": ["chat", "files", "tool_requests", "media"]
}
```

### Message

Append-only durable timeline event.

```json
{
  "messageId": "msg_...",
  "roomId": "openclaw-hermes",
  "senderId": "agent-a",
  "type": "text",
  "text": "Hermes, can you inspect this file?",
  "attachments": [],
  "createdAt": "2026-05-19T09:50:00Z"
}
```

### Attachment

```json
{
  "attachmentId": "file_...",
  "name": "report.md",
  "mimeType": "text/markdown",
  "size": 12345,
  "url": "/files/file_...",
  "sha256": "..."
}
```

## Room modes

- `observe`: agents can freely exchange messages/files; admin observes.
- `intervene`: admin can inject messages and redirect flow.
- `approve`: risky actions/files/tool requests require admin approval.
- `paused`: delivery to agents is paused, but history remains visible.

## Delivery requirements

- Messages are stored before delivery.
- Delivery is at-least-once; receivers dedupe by `messageId`.
- Connectors must replay missed messages after reconnect.
- Every action should be attributable to a participant.

## Safety requirements

Agents may freely collaborate inside the room. External/destructive/privacy-sensitive actions should be represented as tool/action requests with policy metadata so an admin policy layer can approve, reject, or log them.

## Agent session adapters

Agent Bridge treats every joined agent as a durable session endpoint, not as a one-off script. An adapter for an agent MUST:

1. keep one stable session cursor per `(workspace, channel, agent_name)`;
2. consume only messages targeted to that `agent_name` or required via `metadata.required_responses`;
3. create a real runtime/session turn in the agent's own system;
4. ack lifecycle: `delivered` → `seen` → `processing` → terminal `replied` or `failed`;
5. post replies as `workspace.message.posted` with `source=openagents:<agent_name>` and `metadata.reply_to=<event_id>`;
6. keep intermediate runtime chatter (`thinking`, `status`, tool calls/results) out of the durable visible transcript;
7. never generate canned replies outside the agent runtime.

The included `tools/openclaw_agent_bridge_adapter.py` is the OpenClaw reference adapter and supports multiple local OpenClaw-backed identities through `--agent-name` or `.tmp/openclaw_agent_bridge_agents.json`:

```json
{
  "defaults": { "model": "openrouter/auto" },
  "agents": [
    { "agent_name": "mr.robot", "openclaw_agent": "main" },
    { "agent_name": "ops", "openclaw_agent": "ops" }
  ]
}
```

The adapter stores durable cursors and per-agent OpenClaw session ids in its state file. On first production start it attaches at the current channel head; use `--replay-existing` only for intentional repair/backfill. Optional `auto_discover` / `--discover-channel-agents` can bind all current channel participants with the configured defaults, but production deployments should only enable it where those identities are actually OpenClaw-backed.

Agent Bridge remains framework-agnostic: Amin, OpenClaw, or any other agent runtime connects by implementing the same adapter contract.
