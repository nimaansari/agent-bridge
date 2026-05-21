import test from 'node:test';
import assert from 'node:assert/strict';
import {
  appendMessageForChannel,
  messagesForChannel,
  pickActiveChannel,
  pruneChannelState,
  reconcileMessagesByChannel,
} from '../lib/session-thread-state.mjs';

const channels = [
  { address: 'channel/session-a', status: 'active', created_at: 1, last_event_at: 10 },
  { address: 'channel/session-b', status: 'active', created_at: 2, last_event_at: 20 },
];

test('pickActiveChannel restores saved selected session on reload', () => {
  const storage = { getItem: (key) => key === 'agentBridgeCurrentChannel:ws-1' ? 'session-a' : null };
  assert.equal(pickActiveChannel(channels, '', 'ws-1', storage), 'session-a');
});

test('reconcileMessagesByChannel keeps A/B session threads isolated', () => {
  const state = { 'session-a': [{ id: 'a1' }], 'session-b': [{ id: 'b1' }] };
  const next = reconcileMessagesByChannel(state, 'session-a', [{ id: 'a2' }], (_prev, server) => server);
  assert.deepEqual(messagesForChannel(next, 'session-a'), [{ id: 'a2' }]);
  assert.deepEqual(messagesForChannel(next, 'session-b'), [{ id: 'b1' }]);
});

test('appendMessageForChannel does not bleed optimistic rows across sessions', () => {
  const state = { 'session-a': [{ id: 'a1' }], 'session-b': [{ id: 'b1' }] };
  const next = appendMessageForChannel(state, 'session-b', { id: 'b-local' });
  assert.deepEqual(messagesForChannel(next, 'session-a'), [{ id: 'a1' }]);
  assert.deepEqual(messagesForChannel(next, 'session-b'), [{ id: 'b1' }, { id: 'b-local' }]);
});

test('pruneChannelState removes deleted session state only', () => {
  const state = { 'session-a': [{ id: 'a1' }], 'session-b': [{ id: 'b1' }] };
  assert.deepEqual(pruneChannelState(state, ['session-b']), { 'session-b': [{ id: 'b1' }] });
});
