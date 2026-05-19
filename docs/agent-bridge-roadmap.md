# Agent Bridge Roadmap

## Phase 0 — Fork and guardrails

- [x] Fork OpenAgents into this repository.
- [x] Keep Apache-2.0 base license intact.
- [x] Document no-copy policy for BSL/AGPL inspirations.
- [x] Add initial Agent Bridge product/protocol docs.

## Phase 1 — Persistent room MVP

- [ ] Ubuntu server deployment scripts/services.
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
- [ ] Runtime connector records keyed by stable `agent_name`.
- [ ] Validate auto-discovered agents against enabled runtime capabilities.
- [ ] Agent wake/catch-up prompt template.
- [ ] Dashboard-to-agent and agent-to-agent delivery tests.

## Phase 2.5 — Production handoff state

See `docs/agent-bridge-production-adapter-spec.md` for the full contract.

- [ ] Durable handoff attempt table keyed by `(message_id, agent_name, attempt_id)`.
- [ ] CAS/versioned attempt updates or append-only attempt derivation.
- [ ] Explicit state machine: queued, delivered, seen, processing, paused, stalled, replied, failed.
- [ ] Processing leases with `lease_expires_at` and stalled derivation.
- [ ] Retry endpoint that creates a new `attempt_id`; `failed` is terminal only for the attempt.
- [ ] Adapter dedupe scoped by `(event_id, agent_name, attempt_id)`.
- [ ] Canonical message ids: `message_id`, channel `seq`, `reply_to_message_id`, `reply_message_id`.
- [ ] Separate handoff-state query for frontend instead of transcript-derived truth.
- [ ] Runtime stdout schema, exit-code semantics, timeout behavior, stderr policy, retryability classification.

## Phase 3 — Files and artifacts

- [ ] File upload/download API.
- [ ] Attachment metadata in room messages.
- [ ] Agent-side file fetch/save.
- [ ] Inline dashboard previews.
- [ ] Artifact registry per room.
- [ ] First-class file identity: `file_id`, version, checksum, visibility, targets, uploader, durable fetch API.

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
