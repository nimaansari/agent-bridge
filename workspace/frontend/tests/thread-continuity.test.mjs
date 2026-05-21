import test from 'node:test';
import assert from 'node:assert/strict';
import { reconcileMessages, restoreReplyDraft } from '../lib/thread-continuity.mjs';

function msg(overrides = {}) {
  return {
    id: 'm1',
    timestamp: 1,
    pending: false,
    clientMessageId: null,
    ...overrides,
  };
}

test('optimistic pending survives reload until matching server row arrives', () => {
  const optimistic = msg({ id: 'local-1', timestamp: 10, pending: true, clientMessageId: 'c1' });
  const merged = reconcileMessages([optimistic], [msg({ id: 'server-1', timestamp: 5 })]);
  assert.equal(merged.some((message) => message.id === 'local-1'), true);
});

test('optimistic row merges once with matching server event', () => {
  const optimistic = msg({ id: 'local-1', timestamp: 10, pending: true, clientMessageId: 'c1' });
  const server = msg({ id: 'server-1', timestamp: 11, clientMessageId: 'c1' });
  const merged = reconcileMessages([optimistic], [server]);
  assert.equal(merged.filter((message) => message.clientMessageId === 'c1').length, 1);
  assert.equal(merged[0].id, 'server-1');
});

test('stale reply target is discarded and draft can be preserved separately', () => {
  const result = restoreReplyDraft({ id: 'missing', text: 'hello' }, [msg({ id: 'm2' })]);
  assert.equal(result.replyDraft, null);
  assert.equal(result.dropped, true);
});

test('valid reply target is restored when message still exists', () => {
  const result = restoreReplyDraft({ id: 'm2', text: 'hello' }, [msg({ id: 'm2' })]);
  assert.deepEqual(result.replyDraft, { id: 'm2', text: 'hello' });
  assert.equal(result.dropped, false);
});
