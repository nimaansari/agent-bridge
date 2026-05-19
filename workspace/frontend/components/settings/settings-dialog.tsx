'use client';

import { useState, useEffect } from 'react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogTrigger,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Settings, Copy, Check, Bot } from 'lucide-react';
import { workspaceApi } from '@/lib/api';
import { useWorkspace } from '@/lib/workspace-context';
import { useCopyToClipboard } from '@/hooks/use-copy-to-clipboard';
import { toast } from 'sonner';
import { AgentAvatar } from '@/components/agents/agent-avatar';
import type { Workspace, WorkspaceAgent } from '@/lib/types';

interface SettingsDialogProps {
  workspace: Workspace | null;
}

export function SettingsDialog({ workspace }: SettingsDialogProps) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState(workspace?.name || '');
  const [saving, setSaving] = useState(false);
  const [displayNames, setDisplayNames] = useState<Record<string, string>>({});
  const [descriptions, setDescriptions] = useState<Record<string, string>>({});
  const { refreshWorkspace } = useWorkspace();
  const { isCopied: urlCopied, copyToClipboard: copyUrl } = useCopyToClipboard();
  const { isCopied: tokenCopied, copyToClipboard: copyToken } = useCopyToClipboard();

  // Sync descriptions from workspace agents when dialog opens
  useEffect(() => {
    if (open && workspace?.agents) {
      const names: Record<string, string> = {};
      const descs: Record<string, string> = {};
      for (const agent of workspace.agents) {
        names[agent.agentName] = agent.displayName || agent.agentName;
        descs[agent.agentName] = agent.description || '';
      }
      setDisplayNames(names);
      setDescriptions(descs);
    }
  }, [open, workspace?.agents]);

  if (!workspace) return null;

  const workspaceUrl = typeof window !== 'undefined'
    ? `${window.location.origin}/${workspace.workspaceId}${window.location.search}`
    : '';

  const handleSave = async () => {
    if (!name.trim()) return;
    setSaving(true);
    try {
      // Save workspace name
      await workspaceApi.updateWorkspace({ name: name.trim() });

      // Save agent descriptions
      const updates = workspace.agents.map((agent) => {
        const newDisplayName = (displayNames[agent.agentName] ?? agent.agentName).trim();
        const oldDisplayName = agent.displayName || agent.agentName;
        const newDesc = descriptions[agent.agentName] ?? '';
        const oldDesc = agent.description || '';
        if (newDesc !== oldDesc || newDisplayName !== oldDisplayName) {
          return workspaceApi.updateMember(agent.agentName, {
            display_name: newDisplayName === agent.agentName ? '' : newDisplayName,
            description: newDesc,
          });
        }
        return null;
      }).filter(Boolean);

      await Promise.all(updates);
      await refreshWorkspace();
      toast.success('Settings saved');
      setOpen(false);
    } catch {
      toast.error('Failed to save settings');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(v) => { setOpen(v); if (v) setName(workspace.name); }}>
      <DialogTrigger asChild>
        <Button variant="ghost" mode="icon" size="sm">
          <Settings className="size-4" />
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Workspace Settings</DialogTitle>
        </DialogHeader>

        <div className="space-y-6 py-4">
          {/* Workspace name */}
          <div className="space-y-2">
            <Label>Workspace Name</Label>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="My Workspace"
            />
          </div>

          {/* Workspace URL */}
          <div className="space-y-2">
            <Label variant="secondary">Workspace URL</Label>
            <div className="flex items-center gap-2">
              <Input
                value={workspaceUrl}
                readOnly
                className="text-xs font-mono"
              />
              <Button
                variant="outline"
                size="icon"
                onClick={() => copyUrl(workspaceUrl)}
              >
                {urlCopied ? <Check className="size-4" /> : <Copy className="size-4" />}
              </Button>
            </div>
          </div>

          {/* Workspace ID */}
          <div className="space-y-2">
            <Label variant="secondary">Workspace ID</Label>
            <div className="flex items-center gap-2">
              <Input
                value={workspace.workspaceId}
                readOnly
                className="text-xs font-mono"
              />
              <Button
                variant="outline"
                size="icon"
                onClick={() => copyToken(workspace.workspaceId)}
              >
                {tokenCopied ? <Check className="size-4" /> : <Copy className="size-4" />}
              </Button>
            </div>
          </div>

          {/* Agent Descriptions */}
          {workspace.agents.length > 0 && (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <Bot className="size-4 text-muted-foreground" />
                <Label>Agent Descriptions</Label>
              </div>
              <p className="text-xs text-muted-foreground">
                Describe each agent&apos;s role and capabilities so other agents know when to delegate work.
              </p>
              <div className="space-y-4">
                {workspace.agents.map((agent) => (
                  <AgentDescriptionField
                    key={agent.agentName}
                    agent={agent}
                    displayName={displayNames[agent.agentName] || agent.displayName || agent.agentName}
                    onDisplayNameChange={(v) => setDisplayNames((prev) => ({ ...prev, [agent.agentName]: v }))}
                    value={descriptions[agent.agentName] || ''}
                    onChange={(v) => setDescriptions((prev) => ({ ...prev, [agent.agentName]: v }))}
                  />
                ))}
              </div>
            </div>
          )}

        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button onClick={handleSave} disabled={saving || !name.trim()}>
            {saving ? 'Saving...' : 'Save'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function AgentDescriptionField({
  agent,
  displayName,
  onDisplayNameChange,
  value,
  onChange,
}: {
  agent: WorkspaceAgent;
  displayName: string;
  onDisplayNameChange: (v: string) => void;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2">
        <AgentAvatar name={agent.agentName} size={24} />
        <span className="text-sm font-medium">{displayName || agent.agentName}</span>
        <span className="text-xs text-muted-foreground">
          id: {agent.agentName} &middot; {agent.agentType || 'unknown'} &middot; {agent.status}
        </span>
      </div>
      <Input
        className="ml-8 text-sm"
        placeholder="Display name shown in the dashboard"
        value={displayName}
        onChange={(e) => onDisplayNameChange(e.target.value)}
      />
      {agent.workingDir && (
        <p className="text-xs text-muted-foreground font-mono ml-8">
          {agent.workingDir}
        </p>
      )}
      <Textarea
        className="ml-8 text-sm min-h-[60px]"
        placeholder={`Describe what ${agent.agentName} does, e.g. "Manages the Python SDK codebase"`}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}
