import test from 'node:test';
import assert from 'node:assert/strict';
import {
  attachmentIntentEnvelope,
  capabilityDiff,
  capabilityDiffSummary,
  capabilitySnapshot,
  collaborationEventText,
  collaborationSummaryFromSnapshot,
  composerIntentSummary,
  shouldShowActionableBadge,
  shouldShowHandoffStatusPill,
  alternateAgentRecoveryPrefill,
} from '../lib/collaboration-ui.mjs';

function msg(overrides = {}) {
  return {
    id: 'm1',
    senderName: 'openclaw-main',
    senderType: 'agent',
    timestamp: 1,
    targetAgents: [],
    requiredResponses: [],
    responseRequired: false,
    handoffResponses: {},
    acks: [],
    ...overrides,
  };
}

test('event row prefers terminal handoff outcome over ownership moved', () => {
  const text = collaborationEventText(msg({
    responseRequired: true,
    requiredResponses: ['reviewer'],
    handoffResponses: { reviewer: 'failed' },
  }));
  assert.equal(text, 'Handoff failed: reviewer did not reply.');
});

test('standard structured handoff suppresses actionable badge', () => {
  assert.equal(shouldShowActionableBadge(msg({
    targetAgents: ['reviewer'],
    responseRequired: true,
    requiredResponses: ['reviewer'],
  }), 'openclaw-main'), false);
});

test('exceptional targeted message still shows actionable badge', () => {
  assert.equal(shouldShowActionableBadge(msg({
    targetAgents: ['reviewer'],
    responseRequired: false,
    requiredResponses: [],
  }), 'openclaw-main'), true);
});


test('alternate-agent recovery prefill carries explicit takeover scaffold', () => {
  assert.equal(
    alternateAgentRecoveryPrefill('reviewer'),
    '@<agent> previous handoff to reviewer failed. Please take over: '
  );
});

test('only non-terminal handoff pills remain', () => {
  assert.equal(shouldShowHandoffStatusPill('queued'), true);
  assert.equal(shouldShowHandoffStatusPill('processing'), true);
  assert.equal(shouldShowHandoffStatusPill('failed'), false);
  assert.equal(shouldShowHandoffStatusPill('replied'), false);
});


test('composer intent summary reports normal reply mode', () => {
  const summary = composerIntentSummary({
    replyDraft: { sender: 'Alice', type: 'human', text: 'hello' },
    draft: '',
    availableAgents: ['reviewer'],
  });
  assert.equal(summary.mode, 'reply');
  assert.equal(summary.label, 'Replying to Alice');
});

test('composer intent summary reports handoff mode from leading mention', () => {
  const summary = composerIntentSummary({
    draft: '@reviewer can you take this?',
    availableAgents: ['reviewer'],
  });
  assert.equal(summary.mode, 'handoff');
  assert.equal(summary.label, 'Handing off to reviewer');
  assert.equal(summary.detail, 'Aimed at reviewer');
});

test('composer intent summary reports failed-handoff recovery mode', () => {
  const summary = composerIntentSummary({
    draft: alternateAgentRecoveryPrefill('reviewer'),
    failedAgent: 'reviewer',
    availableAgents: ['helper'],
  });
  assert.equal(summary.mode, 'recovery');
  assert.equal(summary.label, 'Recovering from failed handoff to reviewer');
});


test('recovery mode without selected alternate target stays blocked', () => {
  const summary = composerIntentSummary({
    draft: alternateAgentRecoveryPrefill('reviewer'),
    failedAgent: 'reviewer',
    availableAgents: ['helper'],
  });
  assert.equal(summary.mode, 'recovery');
  assert.equal(summary.targetAgent, null);
  assert.equal(summary.requiresExplicitTarget, true);
  assert.equal(summary.detail, 'Choose a new target before sending.');
});


test('attachment intent envelope preserves reply mode metadata', () => {
  const envelope = attachmentIntentEnvelope({
    composerIntent: composerIntentSummary({ replyDraft: { sender: 'Alice', text: 'hello' } }),
    replyDraft: { id: 'm1', sender: 'Alice', text: 'hello' },
  });
  assert.equal(envelope.replyToId, 'm1');
  assert.deepEqual(envelope.targetAgents, []);
  assert.equal(envelope.blocked, false);
});

test('attachment intent envelope preserves handoff target', () => {
  const envelope = attachmentIntentEnvelope({
    composerIntent: composerIntentSummary({ draft: '@reviewer see file', availableAgents: ['reviewer'] }),
    replyDraft: null,
  });
  assert.deepEqual(envelope.targetAgents, ['reviewer']);
  assert.equal(envelope.blocked, false);
});

test('attachment intent envelope blocks recovery without explicit target', () => {
  const envelope = attachmentIntentEnvelope({
    composerIntent: composerIntentSummary({ draft: alternateAgentRecoveryPrefill('reviewer'), failedAgent: 'reviewer', availableAgents: ['helper'] }),
    replyDraft: null,
  });
  assert.equal(envelope.blocked, true);
  assert.deepEqual(envelope.targetAgents, []);
});


test('capability snapshot ignores reconnect reorder noise', () => {
  const a = capabilitySnapshot([], { channelOwner: 'owner', presentAgents: ['owner', 'reviewer'], availableAgents: ['reviewer', 'owner'] });
  const b = capabilitySnapshot([], { channelOwner: 'owner', presentAgents: ['reviewer', 'owner'], availableAgents: ['owner', 'reviewer'] });
  assert.deepEqual(capabilityDiff(a, b), []);
});

test('capability snapshot keeps non-owner leave as presence-only change', () => {
  const a = capabilitySnapshot([], { channelOwner: 'owner', presentAgents: ['owner', 'reviewer'], availableAgents: ['owner', 'reviewer'] });
  const b = capabilitySnapshot([], { channelOwner: 'owner', presentAgents: ['owner'], availableAgents: ['owner', 'reviewer'] });
  assert.deepEqual(capabilityDiff(a, b), ['presence_changed']);
});

test('capability snapshot owner leave clears fallback owner', () => {
  const a = capabilitySnapshot([], { channelOwner: 'owner', presentAgents: ['owner'], availableAgents: ['owner'] });
  const b = capabilitySnapshot([], { channelOwner: 'owner', presentAgents: [], availableAgents: ['owner'] });
  assert.equal(b.ownerAgent, null);
  assert.deepEqual(capabilityDiff(a, b), ['owner_changed', 'presence_changed']);
});

test('capability snapshot pending target swap moves owner and actionable once', () => {
  const first = msg({ requiredResponses: ['a'], responseRequired: true });
  const second = msg({ id: 'm2', requiredResponses: ['b'], responseRequired: true });
  const a = capabilitySnapshot([first], { channelOwner: 'owner', presentAgents: ['owner', 'a', 'b'], availableAgents: ['owner', 'a', 'b'] });
  const b = capabilitySnapshot([first, second], { channelOwner: 'owner', presentAgents: ['owner', 'a', 'b'], availableAgents: ['owner', 'a', 'b'] });
  assert.equal(b.ownerAgent, 'b');
  assert.deepEqual(b.actionableAgents, ['b']);
  assert.deepEqual(capabilityDiff(a, b), ['owner_changed', 'actionable_changed']);
});

test('capability snapshot pending replied clears actionable with no fake owner fallback', () => {
  const first = msg({ requiredResponses: ['a'], responseRequired: true, handoffResponses: { a: 'replied' } });
  const snap = capabilitySnapshot([first], { channelOwner: 'owner', presentAgents: ['a'], availableAgents: ['a'] });
  assert.deepEqual(snap.actionableAgents, []);
  assert.equal(snap.ownerAgent, null);
  const summary = collaborationSummaryFromSnapshot(snap, 'owner');
  assert.equal(summary.owner, 'owner');
});

test('capability snapshot pending failed keeps failed assignee only until human reply', () => {
  const first = msg({ requiredResponses: ['a'], responseRequired: true, handoffResponses: { a: 'failed' } });
  const blocked = capabilitySnapshot([first], { channelOwner: 'owner', presentAgents: ['a'], availableAgents: ['a'] });
  assert.equal(blocked.ownerAgent, 'a');
  const cleared = capabilitySnapshot([first, msg({ id: 'm2', senderType: 'human' })], { channelOwner: 'owner', presentAgents: ['a'], availableAgents: ['a'] });
  assert.deepEqual(cleared.actionableAgents, []);
  assert.equal(cleared.ownerAgent, null);
});


test('capability diff summary ignores reconnect no-op', () => {
  const a = capabilitySnapshot([], { channelOwner: 'owner', presentAgents: ['owner'], availableAgents: ['owner'] });
  const b = capabilitySnapshot([], { channelOwner: 'owner', presentAgents: ['owner'], availableAgents: ['owner'] });
  assert.equal(capabilityDiffSummary(a, b), null);
});

test('capability diff summary reports non-owner leave as presence change', () => {
  const a = capabilitySnapshot([], { channelOwner: 'owner', presentAgents: ['owner', 'reviewer'], availableAgents: ['owner', 'reviewer'] });
  const b = capabilitySnapshot([], { channelOwner: 'owner', presentAgents: ['owner'], availableAgents: ['owner', 'reviewer'] });
  assert.equal(capabilityDiffSummary(a, b), 'Here now: owner.');
});

test('capability diff summary reports owner reassignment once', () => {
  const a = capabilitySnapshot([msg({ requiredResponses: ['a'], responseRequired: true })], { channelOwner: 'owner', presentAgents: ['owner', 'a', 'b'], availableAgents: ['owner', 'a', 'b'] });
  const b = capabilitySnapshot([msg({ requiredResponses: ['b'], responseRequired: true })], { channelOwner: 'owner', presentAgents: ['owner', 'a', 'b'], availableAgents: ['owner', 'a', 'b'] });
  assert.equal(capabilityDiffSummary(a, b), 'Ownership moved to b.');
});

test('capability diff summary reports pending replied as actionable clear', () => {
  const a = capabilitySnapshot([msg({ requiredResponses: ['a'], responseRequired: true })], { channelOwner: 'owner', presentAgents: ['a'], availableAgents: ['a'] });
  const b = capabilitySnapshot([msg({ requiredResponses: ['a'], responseRequired: true, handoffResponses: { a: 'replied' } })], { channelOwner: 'owner', presentAgents: ['a'], availableAgents: ['a'] });
  assert.equal(capabilityDiffSummary(a, b), 'No one owns the thread right now.');
});
