export function reconcileMessages(existingMessages, serverMessages) {
  const merged = [...serverMessages];
  const seenClientIds = new Set(serverMessages.map((message) => message.clientMessageId).filter(Boolean));

  for (const message of existingMessages || []) {
    if (!message?.pending) continue;
    if (message.clientMessageId && seenClientIds.has(message.clientMessageId)) continue;
    merged.push(message);
  }

  return merged.sort((a, b) => a.timestamp - b.timestamp);
}

export function restoreReplyDraft(savedReplyDraft, messages) {
  if (!savedReplyDraft) return { replyDraft: null, dropped: false };
  const exists = (messages || []).some((message) => message.id === savedReplyDraft.id);
  if (exists) return { replyDraft: savedReplyDraft, dropped: false };
  return { replyDraft: null, dropped: true };
}
