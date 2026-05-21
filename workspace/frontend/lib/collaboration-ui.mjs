export function hasTerminalAck(message, agentName) {
  if (['replied', 'failed'].includes(message.handoffResponses?.[agentName])) return true;
  return (message.acks || []).some((ack) => ack.agentName === agentName && ['replied', 'failed'].includes(ack.status));
}

export function collaborationEventText(message) {
  for (const [agentName, status] of Object.entries(message.handoffResponses || {})) {
    if (status === 'replied') return `Handoff closed: ${agentName} replied.`;
    if (status === 'failed') return `Handoff failed: ${agentName} did not reply.`;
  }
  if (message.responseRequired && (message.requiredResponses || []).length > 0) {
    return `Ownership moved to ${message.requiredResponses.join(', ')}.`;
  }
  return null;
}


function normalizeAgentList(values = []) {
  return [...new Set((values || []).filter(Boolean))].sort();
}

export function capabilitySnapshot(messages, options = {}) {
  const availableAgents = new Set((options.availableAgents || []).filter(Boolean));
  const presentAgents = normalizeAgentList((options.presentAgents || []).filter((name) => availableAgents.size === 0 || availableAgents.has(name)));
  const channelOwner = options.channelOwner || null;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    const requiredResponses = normalizeAgentList(message.requiredResponses || []);
    const pending = requiredResponses.filter((agentName) => !hasTerminalAck(message, agentName) && (availableAgents.size === 0 || availableAgents.has(agentName)));
    const failedAgent = requiredResponses.find((agentName) => message.handoffResponses?.[agentName] === 'failed' && (availableAgents.size === 0 || availableAgents.has(agentName))) || null;
    if (failedAgent) {
      const newerHumanReply = messages.slice(i + 1).some((candidate) => candidate.senderType === 'human');
      if (!newerHumanReply) {
        return { presentAgents, actionableAgents: [failedAgent], ownerAgent: failedAgent, status: 'blocked', failedAgent, failedMessageId: message.id, lastHandoffAt: message.timestamp };
      }
      break;
    }
    if (pending.length > 0) {
      return { presentAgents, actionableAgents: pending, ownerAgent: pending[0] || null, status: 'waiting', failedAgent: null, failedMessageId: null, lastHandoffAt: message.timestamp };
    }
    if (message.responseRequired && requiredResponses.length > 0) break;
  }
  const fallbackOwner = channelOwner && presentAgents.includes(channelOwner) ? channelOwner : null;
  return { presentAgents, actionableAgents: [], ownerAgent: fallbackOwner, status: 'idle', failedAgent: null, failedMessageId: null, lastHandoffAt: null };
}

export function capabilityDiff(previousSnapshot, nextSnapshot) {
  if (!previousSnapshot || !nextSnapshot) return [];
  const reasons = [];
  const prevPresent = JSON.stringify(normalizeAgentList(previousSnapshot.presentAgents));
  const nextPresent = JSON.stringify(normalizeAgentList(nextSnapshot.presentAgents));
  const prevActionable = JSON.stringify(normalizeAgentList(previousSnapshot.actionableAgents));
  const nextActionable = JSON.stringify(normalizeAgentList(nextSnapshot.actionableAgents));
  if ((previousSnapshot.ownerAgent || null) !== (nextSnapshot.ownerAgent || null)) reasons.push('owner_changed');
  if (prevActionable != nextActionable) reasons.push('actionable_changed');
  if (prevPresent != nextPresent) reasons.push('presence_changed');
  return reasons;
}

export function collaborationSummaryFromSnapshot(snapshot, channelOwner) {
  if (snapshot.status === 'blocked' && snapshot.failedAgent) {
    return {
      owner: snapshot.ownerAgent || channelOwner || 'unassigned', waitingOn: snapshot.actionableAgents || [], status: 'blocked', label: 'Handoff failed',
      nextStep: 'Retry handoff, reply here, or mention another agent.',
      reason: `Because the requested reply did not complete.`, lastHandoffAt: snapshot.lastHandoffAt, failedAgent: snapshot.failedAgent, failedMessageId: snapshot.failedMessageId,
    };
  }
  if (snapshot.status === 'waiting' && (snapshot.actionableAgents || []).length > 0) {
    return {
      owner: snapshot.ownerAgent || channelOwner || 'unassigned', waitingOn: snapshot.actionableAgents || [], status: 'waiting', label: 'Waiting for reply',
      nextStep: `${snapshot.actionableAgents.join(', ')} reply expected.`,
      reason: `Because a structured handoff expects ${snapshot.actionableAgents.join(', ')} to reply.`, lastHandoffAt: snapshot.lastHandoffAt, failedAgent: null, failedMessageId: null,
    };
  }
  return {
    owner: snapshot.ownerAgent || channelOwner || 'unassigned', waitingOn: [], status: 'idle', label: 'No reply expected',
    nextStep: 'Reply here or mention an agent directly if you want to redirect the thread.',
    reason: 'Because no agent is currently expected to reply.', lastHandoffAt: null, failedAgent: null, failedMessageId: null,
  };
}

export function shouldShowActionableBadge(message, channelMaster) {
  return (message.targetAgents || []).length > 0
    && message.targetAgents.some((agentName) => agentName !== channelMaster)
    && !(message.responseRequired && (message.requiredResponses || []).length > 0);
}

export function shouldShowHandoffStatusPill(status) {
  return status === 'queued' || status === 'processing';
}

export function alternateAgentRecoveryPrefill(failedAgent) {
  return `@<agent> previous handoff to ${failedAgent} failed. Please take over: `;
}


export function extractLeadingMention(draft, availableAgents = []) {
  const match = String(draft || '').trim().match(/^@([^\s:,.!?]+)/);
  if (!match) return null;
  const raw = match[1];
  if (raw.startsWith('<') && raw.endsWith('>')) return null;
  return availableAgents.find((agentName) => agentName.toLowerCase() === raw.toLowerCase()) || raw;
}

/**
 * @param {{ draft?: string, replyDraft?: { sender?: string, type?: string, text?: string } | null, failedAgent?: string | null, availableAgents?: string[] }} [options]
 */
export function composerIntentSummary({ draft = '', replyDraft = null, failedAgent = null, availableAgents = [] } = {}) {
  if (replyDraft) {
    return {
      mode: 'reply',
      label: `Replying to ${replyDraft.sender || replyDraft.type || 'message'}`,
      detail: replyDraft.text || 'message',
      targetAgent: null,
      requiresExplicitTarget: false,
    };
  }
  const mentionedAgent = extractLeadingMention(draft, availableAgents);
  if (failedAgent && draft.includes(`previous handoff to ${failedAgent} failed`)) {
    return {
      mode: 'recovery',
      label: `Recovering from failed handoff to ${failedAgent}`,
      detail: mentionedAgent ? `New target: ${mentionedAgent}` : 'Choose a new target before sending.',
      targetAgent: mentionedAgent || null,
      requiresExplicitTarget: true,
    };
  }
  if (mentionedAgent) {
    return {
      mode: 'handoff',
      label: `Handing off to ${mentionedAgent}`,
      detail: `Aimed at ${mentionedAgent}`,
      targetAgent: mentionedAgent,
      requiresExplicitTarget: false,
    };
  }
  return null;
}


/**
 * @param {{ composerIntent?: { mode?: string, targetAgent?: string | null, requiresExplicitTarget?: boolean } | null, replyDraft?: { id?: string | null } | null }} [options]
 */
export function attachmentIntentEnvelope({ composerIntent = null, replyDraft = null } = {}) {
  return {
    replyToId: replyDraft?.id || null,
    targetAgents: composerIntent?.targetAgent ? [composerIntent.targetAgent] : [],
    blocked: Boolean(composerIntent?.mode === 'recovery' && composerIntent?.requiresExplicitTarget && !composerIntent?.targetAgent),
  };
}


export function capabilityDiffSummary(previousSnapshot, nextSnapshot) {
  const reasons = capabilityDiff(previousSnapshot, nextSnapshot);
  if (!reasons.length) return null;
  if (reasons.includes('owner_changed')) {
    if (nextSnapshot.ownerAgent && previousSnapshot.ownerAgent && nextSnapshot.ownerAgent !== previousSnapshot.ownerAgent) {
      return `Ownership moved to ${nextSnapshot.ownerAgent}.`;
    }
    if (nextSnapshot.ownerAgent) return `${nextSnapshot.ownerAgent} can act now.`;
    return 'No one owns the thread right now.';
  }
  if (reasons.includes('actionable_changed')) {
    if ((nextSnapshot.actionableAgents || []).length > 0) return `Now waiting on ${nextSnapshot.actionableAgents.join(', ')}.`;
    return 'No agent reply is pending now.';
  }
  if (reasons.includes('presence_changed')) {
    return `Here now: ${(nextSnapshot.presentAgents || []).join(', ') || 'no agents'}.`;
  }
  return null;
}
