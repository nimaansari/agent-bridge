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
