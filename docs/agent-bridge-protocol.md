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

The included `tools/openclaw_agent_bridge_adapter.py` is the reference local runtime adapter. It supports OpenClaw directly and any other runtime (Hermes, custom CLIs, sidecars) through an explicit command template. Configure multiple identities through `--agent-name` or a private JSON config copied from `tools/agent_bridge_adapter_config.example.json`:

```json
{
  "defaults": { "model": "openrouter/auto" },
  "agents": [
    { "agent_name": "mr.robot", "runtime": "openclaw", "openclaw_agent": "main" },
    {
      "agent_name": "Hermes",
      "runtime": "hermes",
      "command": ["hermes", "chat", "--session", "{session_id}", "--message", "{message}", "--json"]
    },
    {
      "agent_name": "ops",
      "runtime": "command",
      "command": ["/opt/ops-agent/bin/agent", "--session-id", "{session_id}", "--prompt", "{message}"]
    }
  ]
}
```

Command templates receive `{agent_name}`, `{runtime}`, `{session_id}`, `{message}`, `{timeout}`, `{model}`, and `{thinking}`. Runtime stdout may be plain text or JSON with `reply`, `text`, `message`, `content`, `output`, or `result.payloads[].text`; thinking/tool payloads are filtered out.

For a clone-from-GitHub setup using the example config, private env file, and systemd user service template, see [`agent-bridge-adapter-setup.md`](./agent-bridge-adapter-setup.md).

The adapter stores durable cursors and per-runtime/per-agent session ids in its state file. On first production start, non-session channels attach at the current channel head; use `--replay-existing` only for intentional repair/backfill. `session-*` channels are different: they are real user-facing session threads, so first attach reads existing targeted messages instead of skipping to head. Optional `auto_discover` / `--discover-channel-agents` can bind all current channel participants with the configured defaults, but production deployments should only enable it where those identities have a configured runtime. OpenClaw context-overflow replies rotate to a fresh runtime session once and retry the current bridge message, so one poisoned runtime transcript does not permanently break the room.

Agent Bridge remains framework-agnostic: Amin, OpenClaw, or any other agent runtime connects by implementing the same adapter contract.

For the production-grade handoff/attempt/lease model that should replace the current metadata-summary implementation, see [`agent-bridge-production-adapter-spec.md`](./agent-bridge-production-adapter-spec.md).
