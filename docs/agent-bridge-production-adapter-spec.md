# Agent Bridge Production Adapter Spec

This spec captures the production hardening required for Agent Bridge session adapters. It is based on live adapter testing and external review feedback.

## Goals

Agent Bridge should behave like a real shared session transport for many runtime types (OpenClaw, Hermes, custom agents), not like a dumb event poster.

A production adapter must provide:

- stable `agent_name` identity routing;
- durable per-agent attempts;
- reliable handoff state under reconnects/races;
- clean visible transcript with no thinking/tool spam;
- anchored replies with canonical IDs;
- explicit runtime capability/enrollment records;
- first-class file identities and indexes.

## Handoff State Model

Current metadata-only handoff state is useful but brittle. Production state should move to a durable store keyed by:

```text
(message_id, agent_name, attempt_id)
```

Each row should include at least:

- `message_id` — canonical source message/event id;
- `agent_name` — stable delivery identity, not display label;
- `attempt_id` — unique id per processing attempt;
- `status` — one of the explicit states below;
- `version` — monotonic CAS/version guard;
- `lease_expires_at` — processing lease deadline;
- `reply_message_id` — canonical reply event/message id when available;
- `detail` — bounded diagnostic text;
- `created_at`, `updated_at`.

Metadata on the source event may cache/display a summarized view, but it must not be the only source of truth.

## Status State Machine

Required statuses:

- `queued` — targeted and waiting for an adapter;
- `delivered` — adapter received the message;
- `seen` — runtime/session accepted it for processing;
- `processing` — runtime is actively working under a lease;
- `paused` — runtime/operator intentionally paused;
- `stalled` — processing lease expired without heartbeat;
- `replied` — attempt produced an anchored reply;
- `failed` — attempt ended in failure.

`failed` is terminal for an attempt, not terminal forever for the message. Retrying creates a new `attempt_id`.

## CAS / Race Safety

Avoid last-write-wins corruption when duplicate workers or late reconnects update the same agent/message.

Updates should use either:

1. CAS/versioned writes (`WHERE version = previous_version`), or
2. append-only attempt records with a derived latest state.

The UI should prefer latest valid attempt by `(message_id, agent_name, attempt_created_at/seq)`.

## Leases and Heartbeats

A `processing` state must include `lease_expires_at`.

Adapters should renew leases while work is active. If the lease expires, Agent Bridge should mark the attempt `stalled` or derive `stalled` at query time.

This prevents the UI from showing “processing” forever after adapter death.

## Canonical Message IDs

Reply heuristics are compatibility fallback only. Core protocol should use canonical IDs:

- `message_id` — canonical id for every persisted message/file/handoff source;
- `seq` — monotonic per-channel sequence for incremental sync;
- `reply_to_message_id` — canonical parent id;
- `reply_message_id` — canonical produced reply id.

Adapters should always send `reply_to_message_id`/`metadata.reply_to`. Backend auto-anchor remains only for legacy clients.

## Runtime Contract

Command templates alone are not enough for Hermes/custom runtimes. A runtime connector must define:

### Request fields

- `agent_name`
- `session_id`
- `message_id`
- `attempt_id`
- `reply_to_message_id`
- `channel_id`
- `workspace_id`
- `content`
- optional file/resource references
- timeout/lease settings

### Stdout schema

Runtimes should emit JSON when possible:

```json
{
  "type": "reply",
  "text": "visible reply text",
  "status": "replied",
  "retryable": false,
  "metadata": {}
}
```

Allowed output events:

- `reply` — final visible text;
- `progress` — non-durable or separate progress stream, not transcript spam;
- `file` — produced file/resource reference;
- `error` — structured failure.

Plain text stdout is accepted as a final visible reply for simple runtimes.

### Exit-code semantics

- `0` + reply text/JSON = `replied`;
- `0` + no reply = `failed` with `empty_reply`;
- non-zero = `failed`;
- timeout = `failed` or `stalled` depending on whether the adapter process is alive;
- stderr is diagnostic only and must be bounded before storing.

### Retry classification

Failures should include `retryable: true|false` where possible. The backend should allow explicit retry to create a new `attempt_id`.

## Processed-State Dedupe

Adapter-side dedupe must be scoped by at least:

```text
(event_id, agent_name, attempt_id)
```

Dedupe by `event_id` alone is insufficient once retries and multiple agents exist.

## Frontend Sync

The transcript should not be the only source of truth for operational state.

Frontend should use:

- incremental channel cursor sync by `seq`;
- separate handoff-state query keyed by visible message ids;
- separate files/resources index.

This avoids full reload lag and missing state transitions.

## Files as First-Class Session Resources

Files need durable identity, not just uploaded artifacts:

- `file_id`;
- `version`;
- `checksum`;
- `visibility`;
- `target_agents`;
- `uploader`;
- durable download/fetch API;
- optional source message/reply linkage.

## Enrollment and Runtime Capabilities

Auto-discovery of participants is not enough. It can create fake participants and dead handoffs.

Agent Bridge should add backend connector/runtime capability records:

- `agent_name`;
- `runtime` (`openclaw`, `hermes`, `command`, etc.);
- `capabilities` (chat, files, progress, tools, streaming, max timeout);
- `status` / `last_seen`;
- connector config reference (never secrets in public records);
- health/lease info;
- supported stdout/protocol version.

Adapters should only bind auto-discovered agents when a matching enabled connector exists.

## Implementation Order

1. Add durable handoff attempt table + migration.
2. Add attempt-aware ack endpoint/state machine (`attempt_id`, `lease_expires_at`, CAS/version).
3. Update adapter dedupe to `(event_id, agent_name, attempt_id)` and make `failed` retryable via new attempts.
4. Add stalled derivation/worker for expired leases.
5. Add canonical `message_id`, `seq`, `reply_to_message_id`, `reply_message_id` fields.
6. Add runtime connector/capability records and disable unsafe auto-bind by default.
7. Add runtime stdout schema validation and retryability classification.
8. Add separate frontend queries for handoff state and files/resources.
9. Promote files to first-class resources.
10. Keep metadata summaries only as denormalized UI cache, not source of truth.
