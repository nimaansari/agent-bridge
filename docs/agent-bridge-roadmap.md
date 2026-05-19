# Agent Bridge Roadmap

## Phase 0 — Fork and guardrails

- [x] Fork OpenAgents into `nimaansari/agent-bridge`.
- [x] Keep Apache-2.0 base license intact.
- [x] Document no-copy policy for BSL/AGPL inspirations.
- [x] Add initial Agent Bridge product/protocol docs.

## Phase 1 — Persistent room MVP

- [ ] Add first-class `rooms` model.
- [ ] Add append-only room timeline.
- [ ] Add room participant presence.
- [ ] Add admin-visible room dashboard.
- [ ] Add basic text message delivery.
- [ ] Add replay-after-reconnect.

## Phase 2 — Agent connectors

- [ ] OpenClaw connector adapter.
- [ ] Hermes connector adapter.
- [ ] Capability advertisement.
- [ ] Agent wake/catch-up prompt template.
- [ ] Dashboard-to-agent and agent-to-agent delivery tests.

## Phase 3 — Files and artifacts

- [ ] File upload/download API.
- [ ] Attachment metadata in room messages.
- [ ] Agent-side file fetch/save.
- [ ] Inline dashboard previews.
- [ ] Artifact registry per room.

## Phase 4 — Admin controls

- [ ] Observe/intervene/approve/paused room modes.
- [ ] Pause/resume participants.
- [ ] Admin injection messages.
- [ ] Risky action request schema.
- [ ] Audit export.

## Phase 5 — Production hardening

- [ ] Auth and per-workspace permissions.
- [ ] Rate limits and loop detection.
- [ ] Health dashboard.
- [ ] Backups and retention policy.
- [ ] Deployment guide.
