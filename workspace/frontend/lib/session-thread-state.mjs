export function pickActiveChannel(channels, currentChannel, workspaceId, storage) {
  const activeChannels = [...(channels || [])]
    .filter((channel) => channel.status !== 'deleted')
    .sort((a, b) => (b.last_event_at || b.created_at || 0) - (a.last_event_at || a.created_at || 0));

  const availableNames = new Set(activeChannels.map((channel) => String(channel.address || '').replace(/^channel\//, '')));
  if (currentChannel && availableNames.has(currentChannel)) return currentChannel;

  try {
    const saved = storage?.getItem?.(`agentBridgeCurrentChannel:${workspaceId}`) || '';
    if (saved && availableNames.has(saved)) return saved;
  } catch {}

  const firstAddress = activeChannels[0]?.address || '';
  return String(firstAddress).replace(/^channel\//, '');
}

export function messagesForChannel(messagesByChannel, channel) {
  if (!channel) return [];
  return messagesByChannel?.[channel] || [];
}

export function reconcileMessagesByChannel(messagesByChannel, channel, serverMessages, reconcileMessages) {
  if (!channel) return messagesByChannel || {};
  const previous = messagesForChannel(messagesByChannel, channel);
  return {
    ...(messagesByChannel || {}),
    [channel]: reconcileMessages(previous, serverMessages),
  };
}

export function appendMessageForChannel(messagesByChannel, channel, message) {
  if (!channel) return messagesByChannel || {};
  return {
    ...(messagesByChannel || {}),
    [channel]: [...messagesForChannel(messagesByChannel, channel), message],
  };
}

export function pruneChannelState(messagesByChannel, channelNames) {
  const keep = new Set(channelNames || []);
  return Object.fromEntries(
    Object.entries(messagesByChannel || {}).filter(([channel]) => keep.has(channel))
  );
}
