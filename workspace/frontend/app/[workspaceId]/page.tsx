'use client';

import { Suspense, use, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Bot, Check, Copy, Edit3, Loader2, Lock, MessageCircle, Plus, RefreshCw, Send, Snowflake, User, X } from 'lucide-react';

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
};
type EventRecord = {
  id: string;
  type: string;
  source: string;
  target: string;
  payload: Record<string, unknown> | null;
  timestamp: number;
};
type ChatMessage = {
  id: string;
  senderName: string;
  senderType: 'human' | 'agent' | 'system';
  content: string;
  timestamp: number;
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

function eventToMessage(event: EventRecord): ChatMessage {
  const payload = event.payload || {};
  const senderType = (payload.sender_type as string) === 'human' || event.source.startsWith('human:')
    ? 'human'
    : event.type.includes('status')
      ? 'system'
      : 'agent';
  return {
    id: event.id,
    senderName: (payload.sender_name as string) || sourceName(event.source),
    senderType,
    content: String(payload.content || ''),
    timestamp: event.timestamp,
  };
}

function timeText(ts: number) {
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
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
  const searchParams = typeof window !== 'undefined' ? new URLSearchParams(window.location.search) : new URLSearchParams();
  const initialToken = searchParams.get('token') || '';
  const [token, setToken] = useState(initialToken);
  const [tokenInput, setTokenInput] = useState(initialToken);
  const [room, setRoom] = useState<Room | null>(null);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [currentChannel, setCurrentChannel] = useState<string>('');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [frozen, setFrozen] = useState(false);
  const [connectOpen, setConnectOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const [editingRoom, setEditingRoom] = useState(false);
  const [roomNameDraft, setRoomNameDraft] = useState('');
  const [editingAgent, setEditingAgent] = useState<string | null>(null);
  const [agentNameDraft, setAgentNameDraft] = useState('');
  const bottomRef = useRef<HTMLDivElement | null>(null);

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
      setRoomNameDraft(roomData.name);
      setChannels(discovery.channels || []);
      const firstChannel = currentChannel || discovery.channels?.[0]?.address?.replace(/^channel\//, '') || '';
      setCurrentChannel(firstChannel);

      if (firstChannel) {
        const events = await apiFetch<{ events: EventRecord[] }>(`/v1/events?network=${workspaceId}&channel=${encodeURIComponent(firstChannel)}&type=workspace.message&sort=asc&limit=200`);
        setMessages((events.events || []).map(eventToMessage).filter((m) => m.content));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load room');
    } finally {
      setLoading(false);
    }
  }, [apiFetch, currentChannel, token, workspaceId]);

  useEffect(() => {
    const key = `agentBridgeFreeze:${workspaceId}`;
    setFrozen(window.localStorage.getItem(key) === 'true');
  }, [workspaceId]);

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

  const toggleFreeze = () => {
    const next = !frozen;
    setFrozen(next);
    window.localStorage.setItem(`agentBridgeFreeze:${workspaceId}`, String(next));
  };

  const agentInviteText = useMemo(() => {
    const roomName = room?.name || 'this Agent Bridge room';
    return [
      `Join my Agent Bridge chat room: ${roomName}`,
      '',
      `Room link: ${typeof window !== 'undefined' ? window.location.origin : ''}/${workspaceId}?token=${token}`,
      `Room token: ${token}`,
      `Server/API: ${API_URL}`,
      '',
      'Use the repo/path I give you, connect to this room, then send one short hello message in the chat.',
      'Important: answer and talk inside this room. Keep replies visible in the chatbox.',
      'If I ask you something, reply here. If you work on something, post short progress updates here.',
      'Use your real agent name as your display name.',
      'Do not change files unless I ask you to.',
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
    const optimistic: ChatMessage = { id: `local-${Date.now()}`, senderName: 'You', senderType: 'human', content, timestamp: Date.now() };
    setMessages((prev) => [...prev, optimistic]);
    try {
      await apiFetch('/v1/events', {
        method: 'POST',
        body: JSON.stringify({
          network: workspaceId,
          type: 'workspace.message.posted',
          source: 'human:user',
          target: `channel/${currentChannel}`,
          payload: { content, sender_type: 'human' },
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

  const saveRoomName = async () => {
    const name = roomNameDraft.trim();
    if (!name || !room) return;
    setRoom({ ...room, name });
    setEditingRoom(false);
    await apiFetch(`/v1/workspaces/${workspaceId}`, { method: 'PATCH', body: JSON.stringify({ name }) });
    await refresh();
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

  if (!token) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-slate-950 px-4 text-white">
        <div className="w-full max-w-md rounded-3xl border border-white/10 bg-white/[0.06] p-6 shadow-2xl">
          <Lock className="mb-4 size-8 text-cyan-200" />
          <h1 className="text-xl font-semibold">Enter room token</h1>
          <p className="mt-2 text-sm text-slate-400">This room needs its workspace token. Paste it once and I’ll remember it in this browser.</p>
          <input className="mt-5 w-full rounded-2xl border border-white/10 bg-slate-900 px-4 py-3 text-sm outline-none ring-cyan-300/0 transition focus:ring-4" value={tokenInput} onChange={(e) => setTokenInput(e.target.value)} placeholder="Workspace token" />
          <button onClick={activateToken} className="mt-4 w-full rounded-2xl bg-cyan-300 px-4 py-3 font-semibold text-slate-950 hover:bg-cyan-200">Open room</button>
        </div>
      </main>
    );
  }

  return (
    <main className="flex min-h-screen bg-slate-950 text-slate-100">
      <aside className="hidden w-80 shrink-0 border-r border-white/10 bg-slate-950/90 p-4 lg:block">
        <div className="mb-5 flex items-center gap-3">
          <div className="flex size-10 items-center justify-center rounded-2xl bg-cyan-300 text-slate-950"><MessageCircle className="size-5" /></div>
          <div>
            <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Agent Bridge</p>
            <p className="font-semibold">Simple Room</p>
          </div>
        </div>

        <button onClick={() => setConnectOpen(true)} className="mb-4 flex w-full items-center justify-center gap-2 rounded-2xl bg-cyan-300 px-4 py-3 text-sm font-semibold text-slate-950 hover:bg-cyan-200">
          <Plus className="size-4" /> Add agent
        </button>

        <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-3">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold">Agents</h2>
            <span className="text-xs text-slate-500">{room?.agents?.length || 0}</span>
          </div>
          <div className="space-y-2">
            {(room?.agents || []).map((agent) => (
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
                  <button className="rounded-lg p-1 text-slate-500 hover:bg-white/10 hover:text-white" onClick={() => { setEditingAgent(agent.agentName); setAgentNameDraft(agentLabel(agent)); }} title="Rename agent">
                    <Edit3 className="size-3.5" />
                  </button>
                </div>
              </div>
            ))}
            {!room?.agents?.length && <p className="text-sm text-slate-500">No agents connected yet.</p>}
          </div>
        </div>
      </aside>

      <section className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 bg-slate-950/85 px-4 py-3 backdrop-blur">
          <div className="min-w-0">
            {editingRoom ? (
              <div className="flex gap-2">
                <input autoFocus className="min-w-0 rounded-xl bg-slate-900 px-3 py-2 text-lg font-semibold outline-none ring-1 ring-cyan-300" value={roomNameDraft} onChange={(e) => setRoomNameDraft(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') saveRoomName(); if (e.key === 'Escape') setEditingRoom(false); }} />
                <button onClick={saveRoomName} className="rounded-xl bg-cyan-300 px-3 text-sm font-semibold text-slate-950">Save</button>
              </div>
            ) : (
              <button onClick={() => setEditingRoom(true)} className="group flex max-w-full items-center gap-2 text-left">
                <h1 className="truncate text-xl font-semibold">{room?.name || 'Room'}</h1>
                <Edit3 className="size-4 text-slate-600 group-hover:text-cyan-200" />
              </button>
            )}
            <p className="text-xs text-slate-500">One shared chat room for humans and agents.</p>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={() => setConnectOpen(true)} className="inline-flex items-center gap-2 rounded-xl border border-white/10 px-3 py-2 text-sm hover:border-cyan-300/40 lg:hidden"><Plus className="size-4" /> Add agent</button>
            <button onClick={refresh} className="rounded-xl border border-white/10 p-2 text-slate-300 hover:border-cyan-300/40"><RefreshCw className={loading ? 'size-4 animate-spin' : 'size-4'} /></button>
            <button onClick={toggleFreeze} className={frozen ? 'inline-flex items-center gap-2 rounded-xl bg-amber-300 px-3 py-2 text-sm font-semibold text-slate-950' : 'inline-flex items-center gap-2 rounded-xl border border-white/10 px-3 py-2 text-sm hover:border-cyan-300/40'}>
              <Snowflake className="size-4" /> {frozen ? 'Frozen' : 'Freeze'}
            </button>
          </div>
        </header>

        {error && <div className="border-b border-rose-400/20 bg-rose-500/10 px-4 py-2 text-sm text-rose-100">{error}</div>}
        {frozen && <div className="border-b border-amber-400/20 bg-amber-500/10 px-4 py-2 text-sm text-amber-100">Chat is frozen on this screen. New messages are paused and sending is disabled until you unfreeze.</div>}

        <div className="flex-1 overflow-y-auto px-4 py-5">
          {loading && messages.length === 0 ? (
            <div className="flex h-full items-center justify-center text-slate-500"><Loader2 className="mr-2 size-5 animate-spin" /> Loading room…</div>
          ) : messages.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center text-center text-slate-500">
              <Bot className="mb-4 size-12 opacity-30" />
              <p className="text-lg font-medium text-slate-300">No messages yet</p>
              <p className="mt-1 text-sm">Say something, or add another agent to start the room.</p>
            </div>
          ) : (
            <div className="mx-auto max-w-4xl space-y-4">
              {messages.map((message) => {
                const agent = agentsByName.get(message.senderName);
                const label = message.senderType === 'human' ? 'You' : agent ? agentLabel(agent) : message.senderName;
                return (
                  <div key={message.id} className="flex gap-3">
                    <div className={message.senderType === 'human' ? 'flex size-9 shrink-0 items-center justify-center rounded-2xl bg-slate-700' : 'flex size-9 shrink-0 items-center justify-center rounded-2xl bg-cyan-300 text-slate-950'}>
                      {message.senderType === 'human' ? <User className="size-4" /> : <Bot className="size-4" />}
                    </div>
                    <div className="min-w-0 flex-1 rounded-2xl border border-white/10 bg-white/[0.045] px-4 py-3">
                      <div className="mb-1 flex items-center gap-2">
                        <span className="font-semibold text-white">{label}</span>
                        {agent && <span className="text-xs text-slate-500">id: {agent.agentName}</span>}
                        <span className="ml-auto text-xs text-slate-600">{timeText(message.timestamp)}</span>
                      </div>
                      <p className="whitespace-pre-wrap break-words text-sm leading-6 text-slate-200">{message.content}</p>
                    </div>
                  </div>
                );
              })}
              <div ref={bottomRef} />
            </div>
          )}
        </div>

        <div className="border-t border-white/10 bg-slate-950 p-4">
          <div className="mx-auto flex max-w-4xl gap-2">
            <textarea disabled={frozen || !currentChannel} value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } }} placeholder={frozen ? 'Chat is frozen' : 'Type a message…'} className="max-h-40 min-h-12 flex-1 resize-none rounded-2xl border border-white/10 bg-slate-900 px-4 py-3 text-sm outline-none ring-cyan-300/0 transition focus:ring-4 disabled:opacity-50" />
            <button onClick={sendMessage} disabled={frozen || sending || !draft.trim()} className="rounded-2xl bg-cyan-300 px-4 font-semibold text-slate-950 hover:bg-cyan-200 disabled:cursor-not-allowed disabled:opacity-50"><Send className="size-5" /></button>
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
                  <li>Tell it to answer and post progress in this room.</li>
                </ol>
              </div>

              <details open={copyFailed} className="rounded-2xl border border-white/10 bg-white/[0.03] p-4 text-xs text-slate-400">
                <summary className="cursor-pointer text-sm font-medium text-slate-300">Show token/details</summary>
                <textarea readOnly value={agentInviteText} onFocus={(e) => e.currentTarget.select()} className="mt-3 h-40 w-full resize-none rounded-xl border border-white/10 bg-slate-900 p-3 font-mono text-xs text-slate-200 outline-none focus:border-cyan-300/60" />
              </details>

              <p className="text-sm text-slate-400">When the agent connects, it appears in the left agent list. Rename it with the pencil icon if needed.</p>
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
