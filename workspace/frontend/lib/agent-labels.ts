import type { WorkspaceAgent } from './types';

export function agentDisplayName(agent?: Pick<WorkspaceAgent, 'agentName' | 'displayName'> | null): string {
  if (!agent) return 'Agent';
  return agent.displayName?.trim() || agent.agentName;
}

export function displayNameForAgentName(agentName: string, agents: WorkspaceAgent[]): string {
  const agent = agents.find((a) => a.agentName === agentName);
  return agentDisplayName(agent || { agentName, displayName: null });
}
