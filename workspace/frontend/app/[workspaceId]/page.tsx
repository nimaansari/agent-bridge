'use client';

import { Suspense, use, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Bot, Check, Copy, Download, Edit3, FileText, Loader2, Lock, MessageCircle, Paperclip, Plus, RefreshCw, Reply, Send, Snowflake, Trash2, User, X } from 'lucide-react';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:3010';

type ApiEnvelope<T> = { code?: number; message?: string; data?: T };
type Agent = {
  agentName: string;
  displayName: string | null;
  role: string;
  agentType: string | null;
  status: string;
  description: string | null;
  lastHeartbeatAt: string | null;
};
type Room = {
  workspaceId: string;
  slug: string;
  name: string;
  settings: Record<string, unknown>;
  status: string;
  agents: Agent[];
};
type Channel = {
  address: string;
  title: string | null;
  participants: string[];
  master: string | null;
  status: string;
  created_at?: number | null;
  last_event_at?: number | null;
};
type EventRecord = {
  id: string;
  type: string;
  source: string;
  target: string;
  payload: Record<string, unknown> | null;
  metadata?: Record<string, unknown> | null;
  timestamp: number;
};
type ChatMessage = {
  id: string;
  senderName: string;
  senderType: 'human' | 'agent' | 'system';
  content: string;
  timestamp: number;
  attachments: Attachment[];
  replyTo: ReplyTo | null;
  acks: MessageAck[];
  responseRequired: boolean;
  requiredResponses: string[];
  handoffResponses: Record<string, string>;
};

type MessageAck = {
  agentName: string;
  status: string;
  timestamp: number;
};

type ReplyTo = {
  id: string;
  type?: string;
  source?: string;
  sender?: string;
  text: string;
};

type Attachment = {
  fileId: string;
  filename: string;
  contentType: string;
  size: number;
};

function unwrap<T>(payload: T | ApiEnvelope<T>): T {
  if (payload && typeof payload === 'object' && 'data' in payload) return (payload as ApiEnvelope<T>).data as T;
  return payload as T;
}

function agentLabel(agent?: Pick<Agent, 'agentName' | 'displayName'> | null) {
  return agent?.displayName?.trim() || agent?.agentName || 'Agent';
}

function sourceName(source: string) {
  return source.replace(/^openagents:/, '').replace(/^human:/, '');
}

function channelName(address: string) {
  return address.replace(/^channel\//, '');
}

function pickRoomChannel(channels: Channel[], currentChannel: string, workspaceId: string) {
  const activeChannels = channels
    .filter((channel) => channel.status !== 'deleted')
    .sort((a, b) => (b.last_event_at || b.created_at || 0) - (a.last_event_at || a.created_at || 0));

  const availableNames = new Set(activeChannels.map((channel) => channelName(channel.address)));
  if (currentChannel && availableNames.has(currentChannel)) return currentChannel;

  try {
    const saved = window.localStorage.getItem(`agentBridgeCurrentChannel:${workspaceId}`) || '';
    if (saved && availableNames.has(saved)) return saved;
  } catch {}

  return activeChannels[0]?.address ? channelName(activeChannels[0].address) : '';
}

function eventToMessage(event: EventRecord): ChatMessage {
  const payload = event.payload || {};
  const metadata = event.metadata || {};
  const rawReply = payload.reply_to || payload.replyTo;
  const replyTo = rawReply && typeof rawReply === 'object'
    ? rawReply as ReplyTo
    : null;
  const attachments = Array.isArray(payload.attachments)
    ? (payload.attachments as Attachment[])
    : payload.file_id
      ? [{
        fileId: String(payload.file_id),
        filename: String(payload.filename || 'file'),
        contentType: String(payload.content_type || 'application/octet-stream'),
        size: Number(payload.size || 0),
      }]
      : [];
  const senderType = (payload.sender_type as string) === 'human' || event.source.startsWith('human:')
    ? 'human'
    : event.type.includes('status')
      ? 'system'
      : 'agent';
  const content = String(payload.content || (event.type === 'workspace.file.uploaded' ? 'Shared a file' : ''));
  return {
    id: event.id,
    senderName: (payload.sender_name as string) || sourceName(event.source),
    senderType,
    content,
    timestamp: event.timestamp,
    attachments,
    replyTo,
    acks: [],
    responseRequired: Boolean(metadata.response_required),
    requiredResponses: Array.isArray(metadata.required_responses) ? metadata.required_responses as string[] : [],
    handoffResponses: typeof metadata.handoff_responses === 'object' && metadata.handoff_responses !== null
      ? Object.fromEntries(Object.entries(metadata.handoff_responses as Record<string, { status?: unknown }>).map(([agent, value]) => [agent, String(value?.status || '')]))
      : {},
  };
}

function eventsToMessages(events: EventRecord[]): ChatMessage[] {
  const ackMap = new Map<string, MessageAck[]>();
  for (const event of events) {
    if (event.type !== 'workspace.message.ack') continue;
    const payload = event.payload || {};
    const messageId = String(payload.message_id || '');
    const agentName = String(payload.agent_name || sourceName(event.source));
    const status = String(payload.status || 'seen');
    if (!messageId) continue;
    const list = ackMap.get(messageId) || [];
    const existing = list.find((ack) => ack.agentName === agentName);
    if (existing) {
      if (event.timestamp >= existing.timestamp) {
        existing.status = status;
        existing.timestamp = event.timestamp;
      }
    } else {
      list.push({ agentName, status, timestamp: event.timestamp });
    }
    ackMap.set(messageId, list);
  }
  return events
    .filter((event) => event.type !== 'workspace.message.ack')
    .map(eventToMessage)
    .map((message) => ({ ...message, acks: ackMap.get(message.id) || [] }))
    .filter((message) => message.content || message.attachments.length)
    .sort((a, b) => a.timestamp - b.timestamp);
}

function fileSize(bytes: number) {
  if (!bytes) return 'file';
  const units = ['B', 'KB', 'MB', 'GB'];
  let size = bytes;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size >= 10 || unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
}

function timeText(ts: number) {
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function ackLabel(status: string) {
  if (status === 'processing') return 'processing';
  if (status === 'replied') return 'replied';
  if (status === 'failed') return 'failed';
  if (status === 'seen') return 'seen';
  return 'delivered';
}

function hasTerminalAck(message: ChatMessage, agentName: string) {
  if (['replied', 'failed'].includes(message.handoffResponses[agentName])) return true;
  return message.acks.some((ack) => ack.agentName === agentName && ['replied', 'failed'].includes(ack.status));
}

async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {}

  try {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.setAttribute('readonly', 'true');
    textarea.style.position = 'fixed';
    textarea.style.left = '-9999px';
    textarea.style.top = '0';
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(textarea);
    return ok;
  } catch {
    return false;
  }
}

function RoomPageContent({ workspaceId }: { workspaceId: string }) {
  const [token, setToken] = useState('');
  const [tokenInput, setTokenInput] = useState('');
  const [room, setRoom] = useState<Room | null>(null);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [currentChannel, setCurrentChannel] = useState<string>('');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [frozen, setFrozen] = useState(false);
  const [savingFreeze, setSavingFreeze] = useState(false);
  const [connectOpen, setConnectOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const [editingRoom, setEditingRoom] = useState(false);
  const [savingRoomName, setSavingRoomName] = useState(false);
  const [roomNameDraft, setRoomNameDraft] = useState('');
  const [editingAgent, setEditingAgent] = useState<string | null>(null);
  const [agentNameDraft, setAgentNameDraft] = useState('');
  const [removingAgent, setRemovingAgent] = useState<string | null>(null);
  const [replyDraft, setReplyDraft] = useState<ReplyTo | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    const searchParams = new URLSearchParams(window.location.search);
    const nextToken = searchParams.get('token') || (() => {
      try {
        const stored = JSON.parse(window.localStorage.getItem('agentBridgeWorkspaceTokens') || '{}');
        return stored[workspaceId] || '';
      } catch {
        return '';
      }
    })();
    if (nextToken) {
      setToken(nextToken);
      setTokenInput(nextToken);
    }
  }, [workspaceId]);

  const authHeaders = useCallback(() => ({
    'Content-Type': 'application/json',
    ...(token ? { 'X-Workspace-Token': token } : {}),
  }), [token]);

  const apiFetch = useCallback(async <T,>(path: string, options: RequestInit = {}): Promise<T> => {
    const res = await fetch(`${API_URL}${path}`, {
      ...options,
      cache: 'no-store',
      headers: { ...authHeaders(), ...(options.headers || {}) },
    });
    if (!res.ok) throw new Error(await res.text());
    return unwrap<T>(await res.json());
  }, [authHeaders]);

  const refresh = useCallback(async () => {
    if (!token) return;
    setError(null);
    try {
      const [roomData, discovery] = await Promise.all([
        apiFetch<Room>(`/v1/workspaces/${workspaceId}`),
        apiFetch<{ agents: Array<{ address: string; display_name: string | null; role: string; agent_type: string | null; status: string; description: string | null; last_heartbeat_at: string | null }>; channels: Channel[] }>(`/v1/discover?network=${workspaceId}`),
      ]);

      const agents = discovery.agents.map((a) => ({
        agentName: sourceName(a.address),
        displayName: a.display_name || null,
        role: a.role,
        agentType: a.agent_type || null,
        status: a.status,
        description: a.description || null,
        lastHeartbeatAt: a.last_heartbeat_at || null,
      }));
      setRoom({ ...roomData, agents });
      setFrozen(Boolean(roomData.settings?.frozen));
      if (!editingRoom && !savingRoomName) {
        setRoomNameDraft(roomData.name);
      }
      const sortedChannels = (discovery.channels || [])
        .filter((channel) => channel.status !== 'deleted')
        .sort((a, b) => (b.last_event_at || b.created_at || 0) - (a.last_event_at || a.created_at || 0));
      setChannels(sortedChannels);
      const firstChannel = pickRoomChannel(sortedChannels, currentChannel, workspaceId);
      setCurrentChannel(firstChannel);
      if (firstChannel) {
        try {
          window.localStorage.setItem(`agentBridgeCurrentChannel:${workspaceId}`, firstChannel);
        } catch {}
      }

      if (firstChannel) {
        const events = await apiFetch<{ events: EventRecord[] }>(`/v1/events?network=${workspaceId}&channel=${encodeURIComponent(firstChannel)}&type=workspace&sort=desc&limit=200`);
        setMessages(eventsToMessages(events.events || []));
      } else {
        setMessages([]);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load session');
    } finally {
      setLoading(false);
    }
  }, [apiFetch, currentChannel, editingRoom, savingRoomName, token, workspaceId]);

  useEffect(() => { refresh(); }, [refresh]);

  useEffect(() => {
    if (!token || frozen) return;
    const timer = window.setInterval(refresh, 2500);
    return () => window.clearInterval(timer);
  }, [frozen, refresh, token]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages.length]);

  const agentsByName = useMemo(() => new Map((room?.agents || []).map((a) => [a.agentName, a])), [room?.agents]);

  const activateToken = () => {
    if (!tokenInput.trim()) return;
    const next = tokenInput.trim();
    setToken(next);
    try {
      window.history.replaceState(null, '', `/${workspaceId}?token=${encodeURIComponent(next)}`);
      const stored = JSON.parse(window.localStorage.getItem('agentBridgeWorkspaceTokens') || '{}');
      stored[workspaceId] = next;
      window.localStorage.setItem('agentBridgeWorkspaceTokens', JSON.stringify(stored));
    } catch {}
  };

  const toggleFreeze = async () => {
    if (!room || savingFreeze) return;
    const next = !frozen;
    const previousFrozen = frozen;
    const previousRoom = room;
    const settings = { ...(room.settings || {}), frozen: next };
    setSavingFreeze(true);
    setFrozen(next);
    setRoom({ ...room, settings });
    try {
      const updatedRoom = await apiFetch<Room>(`/v1/workspaces/${workspaceId}`, {
        method: 'PATCH',
        body: JSON.stringify({ settings }),
      });
      setRoom({ ...updatedRoom, agents: previousRoom.agents });
      setFrozen(Boolean(updatedRoom.settings?.frozen));
    } catch (err) {
      setFrozen(previousFrozen);
      setRoom(previousRoom);
      setError(err instanceof Error ? err.message : 'Failed to update freeze state');
    } finally {
      setSavingFreeze(false);
    }
  };

  const agentInviteText = useMemo(() => {
    const roomName = room?.name || 'this Agent Bridge session';
    return [
      `Join my Agent Bridge chat session: ${roomName}`,
      '',
      `Session link: ${typeof window !== 'undefined' ? window.location.origin : ''}/${workspaceId}?token=${token}`,
      `Session token: ${token}`,
      `Server/API: ${API_URL}`,
      '',
      'Use the repo/path I give you, connect to this session, then send one short hello message in the chat.',
      'Important: answer and talk inside this session. Keep replies visible in the chatbox.',
      'Agent chat messages must reply to a specific session event: include payload.reply_to (or metadata.reply_to) with the event id you are answering. Status/thinking updates may be unanchored.',
      'If I ask you something, reply here. If you work on something, post short progress updates here.',
      'Files are shared in this same session: watch for workspace.file.uploaded events, download with GET /v1/files/{file_id}, and upload any file type with POST /v1/files/base64 using your source openagents:<agent_name> and this channel_name.',
      'Use your stable agent_name as your delivery identity; display names are labels only.',
      'Do not change repo files unless I ask you to.',
    ].join('\n');
  }, [room?.name, token, workspaceId]);

  const copyAgentInvite = async () => {
    const ok = await copyText(agentInviteText);
    setCopied(ok);
    setCopyFailed(!ok);
    setTimeout(() => {
      setCopied(false);
      setCopyFailed(false);
    }, 2200);
  };

  const sendMessage = async () => {
    const content = draft.trim();
    if (!content || !currentChannel || sending || frozen) return;
    setSending(true);
    setDraft('');
    const replyTo = replyDraft;
    setReplyDraft(null);
    const optimistic: ChatMessage = { id: `local-${Date.now()}`, senderName: 'You', senderType: 'human', content, timestamp: Date.now(), attachments: [], replyTo, acks: [], responseRequired: false, requiredResponses: [], handoffResponses: {} };
    setMessages((prev) => [...prev, optimistic]);
    try {
      await apiFetch('/v1/events', {
        method: 'POST',
        body: JSON.stringify({
          network: workspaceId,
          type: 'workspace.message.posted',
          source: 'human:user',
          target: `channel/${currentChannel}`,
          payload: { content, sender_type: 'human', ...(replyTo ? { reply_to: replyTo } : {}) },
          metadata: replyTo ? { reply_to: replyTo.id } : {},
          visibility: 'channel',
        }),
      });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to send message');
    } finally {
      setSending(false);
    }
  };

  const fileUrl = (fileId: string) => `${API_URL}/v1/files/${encodeURIComponent(fileId)}?token=${encodeURIComponent(token)}`;

  const beginReply = (message: ChatMessage) => {
    setReplyDraft({
      id: message.id,
      type: message.senderType,
      sender: message.senderName,
      text: message.content || message.attachments[0]?.filename || 'file',
    });
  };

  const uploadFiles = async (fileList: FileList | null) => {
    const files = Array.from(fileList || []);
    if (!files.length || !currentChannel || uploading || frozen) return;
    setUploading(true);
    setError(null);
    try {
      for (const file of files) {
        const formData = new FormData();
        formData.append('file', file);
        formData.append('network', workspaceId);
        formData.append('channel_name', currentChannel);
        formData.append('source', 'human:user');
        const res = await fetch(`${API_URL}/v1/files`, {
          method: 'POST',
          headers: token ? { 'X-Workspace-Token': token } : {},
          body: formData,
        });
        if (!res.ok) throw new Error(await res.text());
      }
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to upload file');
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const saveRoomName = async () => {
    const name = roomNameDraft.trim();
    if (!name || !room || savingRoomName) return;
    const previousRoom = room;
    setSavingRoomName(true);
    setRoom({ ...room, name });
    try {
      const updatedRoom = await apiFetch<Room>(`/v1/workspaces/${workspaceId}`, {
        method: 'PATCH',
        body: JSON.stringify({ name }),
      });
      setRoom({ ...updatedRoom, agents: previousRoom.agents });
      setRoomNameDraft(updatedRoom.name);
      setEditingRoom(false);
      await refresh();
    } catch (err) {
      setRoom(previousRoom);
      setError(err instanceof Error ? err.message : 'Failed to rename session');
    } finally {
      setSavingRoomName(false);
    }
  };

  const saveAgentName = async (agent: Agent) => {
    const displayName = agentNameDraft.trim();
    setEditingAgent(null);
    await apiFetch(`/v1/workspaces/${workspaceId}/members/${encodeURIComponent(agent.agentName)}`, {
      method: 'PATCH',
      body: JSON.stringify({ display_name: displayName === agent.agentName ? '' : displayName }),
    });
    await refresh();
  };

  const removeAgent = async (agent: Agent) => {
    if (removingAgent) return;
    const label = agentLabel(agent);
    if (!window.confirm(`Remove ${label} from this session? They will need a fresh invite to rejoin.`)) return;
    setRemovingAgent(agent.agentName);
    setError(null);
    try {
      await apiFetch(`/v1/workspaces/${workspaceId}/members/${encodeURIComponent(agent.agentName)}`, {
        method: 'DELETE',
      });
      setRoom((prev) => prev ? { ...prev, agents: prev.agents.filter((a) => a.agentName !== agent.agentName) } : prev);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to remove ${label}`);
    } finally {
      setRemovingAgent(null);
    }
  };

  const renderAgentCard = (agent: Agent) => (
    <div key={agent.agentName} className="rounded-xl bg-slate-900/80 p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          {editingAgent === agent.agentName ? (
            <input autoFocus className="w-full rounded-lg bg-slate-800 px-2 py-1 text-sm outline-none ring-1 ring-cyan-300" value={agentNameDraft} onChange={(e) => setAgentNameDraft(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') saveAgentName(agent); if (e.key === 'Escape') setEditingAgent(null); }} />
          ) : (
            <p className="truncate text-sm font-semibold">{agentLabel(agent)}</p>
          )}
          <p className="truncate text-xs text-slate-500">id: {agent.agentName}</p>
          <p className="mt-1 text-xs text-slate-400">{agent.agentType || 'agent'} · {agent.status}</p>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2">
        <button className="inline-flex items-center justify-center gap-1.5 rounded-lg border border-white/10 px-2 py-1.5 text-xs text-slate-300 hover:border-cyan-300/40 hover:text-white" onClick={() => { setEditingAgent(agent.agentName); setAgentNameDraft(agentLabel(agent)); }} title="Rename agent">
          <Edit3 className="size-3.5" /> Rename
        </button>
        <button disabled={removingAgent === agent.agentName} className="inline-flex items-center justify-center gap-1.5 rounded-lg border border-rose-300/20 px-2 py-1.5 text-xs text-rose-100 hover:bg-rose-500/10 disabled:cursor-not-allowed disabled:opacity-50" onClick={() => removeAgent(agent)} title="Remove agent from session">
          {removingAgent === agent.agentName ? <Loader2 className="size-3.5 animate-spin" /> : <Trash2 className="size-3.5" />} Remove
        </button>
      </div>
    </div>
  );

  if (!token) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-slate-950 px-4 text-white">
        <div className="w-full max-w-md rounded-3xl border border-white/10 bg-white/[0.06] p-6 shadow-2xl">
          <Lock className="mb-4 size-8 text-cyan-200" />
          <h1 className="text-xl font-semibold">Enter session token</h1>
          <p className="mt-2 text-sm text-slate-400">This session needs its token. Paste it once and I’ll remember it in this browser.</p>
          <input className="mt-5 w-full rounded-2xl border border-white/10 bg-slate-900 px-4 py-3 text-sm outline-none ring-cyan-300/0 transition focus:ring-4" value={tokenInput} onChange={(e) => setTokenInput(e.target.value)} placeholder="Session token" />
          <button onClick={activateToken} className="mt-4 w-full rounded-2xl bg-cyan-300 px-4 py-3 font-semibold text-slate-950 hover:bg-cyan-200">Open session</button>
        </div>
      </main>
    );
  }

  return (
    <main className="fixed inset-0 flex overflow-hidden bg-slate-950 text-slate-100">
      <aside className="hidden h-full w-80 shrink-0 overflow-hidden border-r border-white/10 bg-slate-950/90 p-4 lg:flex lg:flex-col">
        <div className="mb-5 flex items-center gap-3">
          <div className="flex size-10 items-center justify-center rounded-2xl bg-cyan-300 text-slate-950"><MessageCircle className="size-5" /></div>
          <div>
            <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Agent Bridge</p>
            <p className="font-semibold">Session</p>
          </div>
        </div>

        <button onClick={() => setConnectOpen(true)} className="mb-4 flex w-full items-center justify-center gap-2 rounded-2xl bg-cyan-300 px-4 py-3 text-sm font-semibold text-slate-950 hover:bg-cyan-200">
          <Plus className="size-4" /> Add agent
        </button>

        <div className="min-h-0 flex-1 overflow-y-auto rounded-2xl border border-white/10 bg-white/[0.04] p-3 pr-2">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold">Agents</h2>
            <span className="text-xs text-slate-500">{room?.agents?.length || 0}</span>
          </div>
          <div className="space-y-2">
            {(room?.agents || []).map(renderAgentCard)}
            {!room?.agents?.length && <p className="text-sm text-slate-500">No agents connected yet.</p>}
          </div>
        </div>
      </aside>

      <section className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        <header className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-white/10 bg-slate-950/85 px-4 py-3 backdrop-blur">
          <div className="min-w-0">
            {editingRoom ? (
              <div className="flex gap-2">
                <input autoFocus disabled={savingRoomName} className="min-w-0 rounded-xl bg-slate-900 px-3 py-2 text-lg font-semibold outline-none ring-1 ring-cyan-300 disabled:opacity-60" value={roomNameDraft} onChange={(e) => setRoomNameDraft(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') saveRoomName(); if (e.key === 'Escape' && !savingRoomName) setEditingRoom(false); }} />
                <button disabled={savingRoomName || !roomNameDraft.trim()} onClick={saveRoomName} className="rounded-xl bg-cyan-300 px-3 text-sm font-semibold text-slate-950 disabled:cursor-not-allowed disabled:opacity-60">{savingRoomName ? 'Saving…' : 'Save'}</button>
              </div>
            ) : (
              <button onClick={() => setEditingRoom(true)} className="group flex max-w-full items-center gap-2 text-left">
                <h1 className="truncate text-xl font-semibold">{room?.name || 'Session'}</h1>
                <Edit3 className="size-4 text-slate-600 group-hover:text-cyan-200" />
              </button>
            )}
            <p className="text-xs text-slate-500">One shared session for humans and agents.</p>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={() => setConnectOpen(true)} className="inline-flex items-center gap-2 rounded-xl border border-white/10 px-3 py-2 text-sm hover:border-cyan-300/40 lg:hidden"><Plus className="size-4" /> Add agent</button>
            <button onClick={refresh} className="rounded-xl border border-white/10 p-2 text-slate-300 hover:border-cyan-300/40"><RefreshCw className={loading ? 'size-4 animate-spin' : 'size-4'} /></button>
            <button disabled={savingFreeze} onClick={toggleFreeze} className={frozen ? 'inline-flex items-center gap-2 rounded-xl bg-amber-300 px-3 py-2 text-sm font-semibold text-slate-950 disabled:opacity-60' : 'inline-flex items-center gap-2 rounded-xl border border-white/10 px-3 py-2 text-sm hover:border-cyan-300/40 disabled:opacity-60'}>
              <Snowflake className="size-4" /> {savingFreeze ? 'Saving…' : frozen ? 'Frozen' : 'Freeze'}
            </button>
          </div>
        </header>

        {error && <div className="shrink-0 border-b border-rose-400/20 bg-rose-500/10 px-4 py-2 text-sm text-rose-100">{error}</div>}
        {frozen && <div className="shrink-0 border-b border-amber-400/20 bg-amber-500/10 px-4 py-2 text-sm text-amber-100">Session is frozen. Humans and agents cannot send chat messages until you unfreeze.</div>}

        <div className="shrink-0 border-b border-white/10 bg-slate-950/90 px-4 py-3 lg:hidden">
          <details className="rounded-2xl border border-white/10 bg-white/[0.04] p-3">
            <summary className="cursor-pointer text-sm font-semibold text-slate-100">Manage agents ({room?.agents?.length || 0})</summary>
            <div className="mt-3 grid gap-2">
              {(room?.agents || []).map(renderAgentCard)}
              {!room?.agents?.length && <p className="text-sm text-slate-500">No agents connected. New sessions start empty — use Add agent when you want one to join.</p>}
            </div>
          </details>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-5">
          {loading && messages.length === 0 ? (
            <div className="flex h-full items-center justify-center text-slate-500"><Loader2 className="mr-2 size-5 animate-spin" /> Loading session…</div>
          ) : messages.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center text-center text-slate-500">
              <Bot className="mb-4 size-12 opacity-30" />
              <p className="text-lg font-medium text-slate-300">No messages yet</p>
              <p className="mt-1 text-sm">Say something, or add another agent to start the session.</p>
            </div>
          ) : (
            <div className="mx-auto max-w-4xl space-y-4">
              {messages.map((message) => {
                const agent = agentsByName.get(message.senderName);
                const label = message.senderType === 'human' ? 'You' : agent ? agentLabel(agent) : message.senderName;
                return (
                  <div key={message.id} id={`message-${message.id}`} className="group flex gap-3 scroll-mt-20">
                    <div className={message.senderType === 'human' ? 'flex size-9 shrink-0 items-center justify-center rounded-2xl bg-slate-700' : 'flex size-9 shrink-0 items-center justify-center rounded-2xl bg-cyan-300 text-slate-950'}>
                      {message.senderType === 'human' ? <User className="size-4" /> : <Bot className="size-4" />}
                    </div>
                    <div className="min-w-0 flex-1 rounded-2xl border border-white/10 bg-white/[0.045] px-4 py-3">
                      <div className="mb-1 flex items-center gap-2">
                        <span className="font-semibold text-white">{label}</span>
                        {agent && <span className="text-xs text-slate-500">id: {agent.agentName}</span>}
                        {message.replyTo && <span className="rounded-full bg-cyan-300/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-cyan-100">reply</span>}
                        <span className="ml-auto text-xs text-slate-600">{timeText(message.timestamp)}</span>
                        <button onClick={() => beginReply(message)} className="rounded-lg p-1 text-slate-500 opacity-0 transition hover:bg-white/10 hover:text-cyan-100 group-hover:opacity-100" title="Reply to this message"><Reply className="size-4" /></button>
                      </div>
                      {message.replyTo && (
                        <button onClick={() => document.getElementById(`message-${message.replyTo?.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })} className="mb-3 block max-w-full rounded-xl border-l-2 border-cyan-300/70 bg-slate-950/70 px-3 py-2 text-left text-xs text-slate-400 hover:text-slate-200">
                          <span className="block font-semibold text-cyan-100">Replying to {sourceName(message.replyTo.sender || message.replyTo.type || 'message')}</span>
                          <span className="line-clamp-2 break-words">{message.replyTo.text || 'message'}</span>
                        </button>
                      )}
                      {message.content && <p className="whitespace-pre-wrap break-words text-sm leading-6 text-slate-200">{message.content}</p>}
                      {message.attachments.length > 0 && (
                        <div className="mt-3 grid gap-2">
                          {message.attachments.map((file) => (
                            <a key={file.fileId} href={fileUrl(file.fileId)} target="_blank" rel="noreferrer" className="flex items-center gap-3 rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm text-slate-200 hover:border-cyan-300/50 hover:text-white">
                              <FileText className="size-4 shrink-0 text-cyan-200" />
                              <span className="min-w-0 flex-1 truncate">{file.filename}</span>
                              <span className="text-xs text-slate-500">{fileSize(file.size)}</span>
                              <Download className="size-4 shrink-0 text-slate-500" />
                            </a>
                          ))}
                        </div>
                      )}
                      {message.acks.length > 0 && (
                        <div className="mt-3 flex flex-wrap gap-1.5">
                          {message.acks.map((ack) => (
                            <span key={`${message.id}-${ack.agentName}`} className="rounded-full border border-white/10 bg-slate-950/70 px-2 py-0.5 text-[11px] text-slate-400">
                              {ack.agentName}: {ackLabel(ack.status)}
                            </span>
                          ))}
                        </div>
                      )}
                      {message.responseRequired && message.requiredResponses.length > 0 && (
                        <div className="mt-3 flex flex-wrap gap-1.5">
                          {message.requiredResponses.map((agentName) => {
                            const done = hasTerminalAck(message, agentName);
                            return (
                              <span key={`${message.id}-required-${agentName}`} className={done ? 'rounded-full border border-emerald-300/20 bg-emerald-300/10 px-2 py-0.5 text-[11px] text-emerald-100' : 'rounded-full border border-amber-300/20 bg-amber-300/10 px-2 py-0.5 text-[11px] text-amber-100'}>
                                {done ? `${agentName}: answered` : `${agentName}: response needed`}
                              </span>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
              <div ref={bottomRef} />
            </div>
          )}
        </div>

        <div className="shrink-0 border-t border-white/10 bg-slate-950 p-4">
          <div className="mx-auto max-w-4xl">
            {replyDraft && (
              <div className="mb-2 flex items-center gap-3 rounded-2xl border border-cyan-300/20 bg-cyan-300/10 px-3 py-2 text-sm text-cyan-50">
                <Reply className="size-4 shrink-0" />
                <div className="min-w-0 flex-1">
                  <div className="text-xs font-semibold uppercase tracking-wide text-cyan-100">Replying to {sourceName(replyDraft.sender || replyDraft.type || 'message')}</div>
                  <div className="truncate text-slate-200">{replyDraft.text || 'message'}</div>
                </div>
                <button onClick={() => setReplyDraft(null)} className="rounded-lg p-1 text-cyan-100 hover:bg-white/10"><X className="size-4" /></button>
              </div>
            )}
            <div className="flex gap-2">
            <input ref={fileInputRef} type="file" multiple className="hidden" onChange={(e) => uploadFiles(e.currentTarget.files)} />
            <button onClick={() => fileInputRef.current?.click()} disabled={frozen || uploading || !currentChannel} className="rounded-2xl border border-white/10 px-4 text-slate-300 hover:border-cyan-300/40 hover:text-white disabled:cursor-not-allowed disabled:opacity-50" title="Attach files">
              {uploading ? <Loader2 className="size-5 animate-spin" /> : <Paperclip className="size-5" />}
            </button>
            <textarea disabled={frozen || !currentChannel} value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } }} placeholder={frozen ? 'Chat is frozen' : 'Type a message…'} className="max-h-40 min-h-12 flex-1 resize-none rounded-2xl border border-white/10 bg-slate-900 px-4 py-3 text-sm outline-none ring-cyan-300/0 transition focus:ring-4 disabled:opacity-50" />
            <button onClick={sendMessage} disabled={frozen || sending || !draft.trim()} className="rounded-2xl bg-cyan-300 px-4 font-semibold text-slate-950 hover:bg-cyan-200 disabled:cursor-not-allowed disabled:opacity-50"><Send className="size-5" /></button>
            </div>
          </div>
        </div>
      </section>

      {connectOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
          <div className="w-full max-w-lg rounded-3xl border border-white/10 bg-slate-950 p-6 shadow-2xl">
            <div className="mb-4 flex items-start justify-between gap-4">
              <div>
                <h2 className="text-xl font-semibold">Add agent</h2>
                <p className="mt-1 text-sm text-slate-400">Copy one invite. Paste it to the other agent with the repo you want it to use.</p>
              </div>
              <button onClick={() => setConnectOpen(false)} className="rounded-xl p-2 text-slate-400 hover:bg-white/10 hover:text-white"><X className="size-5" /></button>
            </div>

            <div className="space-y-3">
              <button onClick={copyAgentInvite} className="flex w-full items-center justify-center gap-3 rounded-2xl bg-cyan-300 px-4 py-4 text-base font-semibold text-slate-950 hover:bg-cyan-200">
                {copied ? <Check className="size-5" /> : <Copy className="size-5" />}
                {copied ? 'Copied invite' : copyFailed ? 'Select text below' : 'Copy agent invite'}
              </button>
              {copyFailed && (
                <p className="rounded-xl border border-amber-300/20 bg-amber-300/10 px-3 py-2 text-sm text-amber-100">
                  Browser copy was blocked. Select the invite text below and copy it manually.
                </p>
              )}

              <div className="rounded-2xl bg-black/35 p-4 text-sm leading-6 text-slate-300 ring-1 ring-white/10">
                <p className="mb-2 font-semibold text-slate-100">What to do:</p>
                <ol className="list-decimal space-y-1 pl-5">
                  <li>Click <span className="text-cyan-100">Copy agent invite</span>.</li>
                  <li>Paste it to the agent.</li>
                  <li>Give that agent the repo/path.</li>
                  <li>The agent joins and says hello here.</li>
                  <li>Tell it to answer and post progress in this session.</li>
                </ol>
              </div>

              <details open={copyFailed} className="rounded-2xl border border-white/10 bg-white/[0.03] p-4 text-xs text-slate-400">
                <summary className="cursor-pointer text-sm font-medium text-slate-300">Show token/details</summary>
                <textarea readOnly value={agentInviteText} onFocus={(e) => e.currentTarget.select()} className="mt-3 h-40 w-full resize-none rounded-xl border border-white/10 bg-slate-900 p-3 font-mono text-xs text-slate-200 outline-none focus:border-cyan-300/60" />
              </details>

              <p className="text-sm text-slate-400">When the agent connects, it appears in the left agent list for this session. Rename it with the pencil icon if needed.</p>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}

export default function WorkspacePage({ params }: { params: Promise<{ workspaceId: string }> }) {
  const { workspaceId } = use(params);
  return (
    <Suspense fallback={<main className="flex min-h-screen items-center justify-center bg-slate-950 text-slate-400"><Loader2 className="mr-2 size-5 animate-spin" /> Loading…</main>}>
      <RoomPageContent workspaceId={workspaceId} />
    </Suspense>
  );
}
