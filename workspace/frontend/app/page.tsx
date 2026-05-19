'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity,
  Archive,
  Bot,
  Boxes,
  CheckCircle2,
  CircleDot,
  Clock3,
  Database,
  FileText,
  GitBranch,
  Globe2,
  HardDriveUpload,
  KeyRound,
  Loader2,
  MessageSquareText,
  Network,
  Plus,
  RadioTower,
  RefreshCw,
  ShieldCheck,
  TerminalSquare,
  Users,
  Zap,
} from 'lucide-react';

type ApiEnvelope<T> = {
  code?: number;
  message?: string;
  data?: T;
};

type WorkspaceAgent = {
  agentName: string;
  role?: string;
  agentType?: string;
  status?: string;
  description?: string | null;
  lastHeartbeatAt?: string | null;
};

type Workspace = {
  workspaceId: string;
  slug?: string;
  name: string;
  status: string;
  agents?: WorkspaceAgent[];
  createdAt?: string | null;
  lastActivityAt?: string | null;
};

type HealthState = 'checking' | 'online' | 'degraded';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:3010';

function unwrap<T>(payload: T | ApiEnvelope<T>): T {
  if (payload && typeof payload === 'object' && 'data' in payload) {
    return (payload as ApiEnvelope<T>).data as T;
  }
  return payload as T;
}

function timeAgo(value?: string | null) {
  if (!value) return 'never';
  const ts = new Date(value).getTime();
  if (Number.isNaN(ts)) return 'unknown';
  const seconds = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function classNames(...parts: Array<string | false | undefined>) {
  return parts.filter(Boolean).join(' ');
}

function StatCard({ label, value, helper, icon: Icon, tone = 'cyan' }: {
  label: string;
  value: string | number;
  helper: string;
  icon: typeof Activity;
  tone?: 'cyan' | 'violet' | 'emerald' | 'amber';
}) {
  const tones = {
    cyan: 'from-cyan-500/20 to-blue-500/10 text-cyan-200 ring-cyan-400/20',
    violet: 'from-violet-500/20 to-fuchsia-500/10 text-violet-200 ring-violet-400/20',
    emerald: 'from-emerald-500/20 to-teal-500/10 text-emerald-200 ring-emerald-400/20',
    amber: 'from-amber-500/20 to-orange-500/10 text-amber-200 ring-amber-400/20',
  };

  return (
    <div className="rounded-3xl border border-white/10 bg-white/[0.055] p-5 shadow-2xl shadow-black/20 backdrop-blur">
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="text-xs uppercase tracking-[0.24em] text-slate-400">{label}</p>
          <p className="mt-3 text-3xl font-semibold text-white">{value}</p>
        </div>
        <div className={classNames('rounded-2xl bg-gradient-to-br p-3 ring-1', tones[tone])}>
          <Icon className="size-5" />
        </div>
      </div>
      <p className="mt-4 text-sm text-slate-400">{helper}</p>
    </div>
  );
}

function Pill({ children, tone = 'slate' }: { children: React.ReactNode; tone?: 'green' | 'amber' | 'red' | 'blue' | 'slate' }) {
  const tones = {
    green: 'bg-emerald-400/10 text-emerald-200 ring-emerald-400/20',
    amber: 'bg-amber-400/10 text-amber-200 ring-amber-400/20',
    red: 'bg-rose-400/10 text-rose-200 ring-rose-400/20',
    blue: 'bg-cyan-400/10 text-cyan-200 ring-cyan-400/20',
    slate: 'bg-slate-400/10 text-slate-200 ring-slate-400/20',
  };
  return <span className={classNames('inline-flex items-center rounded-full px-2.5 py-1 text-xs font-medium ring-1', tones[tone])}>{children}</span>;
}

function SectionCard({ title, subtitle, children, action }: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <section className="rounded-3xl border border-white/10 bg-slate-950/55 p-5 shadow-2xl shadow-black/20 backdrop-blur">
      <div className="mb-5 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold text-white">{title}</h2>
          {subtitle && <p className="mt-1 text-sm text-slate-400">{subtitle}</p>}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

export default function AgentBridgeDashboard() {
  const [health, setHealth] = useState<HealthState>('checking');
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [createdToken, setCreatedToken] = useState<{ workspaceId: string; token: string } | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [healthRes, workspacesRes] = await Promise.all([
        fetch(`${API_URL}/health`, { cache: 'no-store' }),
        fetch(`${API_URL}/v1/workspaces`, { cache: 'no-store' }),
      ]);

      setHealth(healthRes.ok ? 'online' : 'degraded');

      if (!workspacesRes.ok) {
        throw new Error(`Workspace API returned ${workspacesRes.status}`);
      }
      const workspacePayload = await workspacesRes.json();
      setWorkspaces(unwrap<Workspace[]>(workspacePayload) || []);
    } catch (err) {
      setHealth('degraded');
      setError(err instanceof Error ? err.message : 'Unable to reach Agent Bridge backend');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 15000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const totals = useMemo(() => {
    const agents = workspaces.reduce((sum, ws) => sum + (ws.agents?.length || 0), 0);
    const online = workspaces.reduce(
      (sum, ws) => sum + (ws.agents || []).filter((agent) => agent.status === 'online').length,
      0,
    );
    return { agents, online };
  }, [workspaces]);

  const createDemoWorkspace = async () => {
    setCreating(true);
    setError(null);
    try {
      const res = await fetch(`${API_URL}/v1/workspaces`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: `Agent Bridge Room ${new Date().toISOString().slice(11, 16)}`,
          agent_name: 'openclaw-main',
          agent_type: 'openclaw',
        }),
      });
      if (!res.ok) throw new Error(`Create workspace returned ${res.status}`);
      const payload = unwrap<{ workspaceId: string; token: string }>(await res.json());
      setCreatedToken(payload);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to create workspace');
    } finally {
      setCreating(false);
    }
  };

  return (
    <main className="min-h-screen overflow-hidden bg-[#050812] text-slate-100">
      <div className="pointer-events-none fixed inset-0">
        <div className="absolute -left-32 top-[-12rem] h-96 w-96 rounded-full bg-cyan-500/20 blur-3xl" />
        <div className="absolute right-[-10rem] top-20 h-[28rem] w-[28rem] rounded-full bg-violet-500/20 blur-3xl" />
        <div className="absolute bottom-[-14rem] left-1/3 h-[30rem] w-[30rem] rounded-full bg-emerald-500/10 blur-3xl" />
        <div className="absolute inset-0 bg-[linear-gradient(rgba(255,255,255,0.035)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,0.035)_1px,transparent_1px)] bg-[size:52px_52px] [mask-image:radial-gradient(circle_at_top,black,transparent_68%)]" />
      </div>

      <div className="relative mx-auto flex min-h-screen w-full max-w-7xl flex-col px-4 py-5 sm:px-6 lg:px-8">
        <header className="mb-8 flex flex-col gap-4 rounded-[2rem] border border-white/10 bg-white/[0.055] p-4 shadow-2xl shadow-black/20 backdrop-blur md:flex-row md:items-center md:justify-between">
          <div className="flex items-center gap-4">
            <div className="relative flex size-12 items-center justify-center rounded-2xl bg-cyan-400/15 ring-1 ring-cyan-300/30">
              <Network className="size-6 text-cyan-200" />
              <span className="absolute -right-1 -top-1 flex size-4 rounded-full bg-emerald-400 ring-4 ring-slate-950" />
            </div>
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="text-2xl font-semibold tracking-tight text-white">Agent Bridge</h1>
                <Pill tone={health === 'online' ? 'green' : health === 'checking' ? 'amber' : 'red'}>
                  {health === 'checking' ? 'checking backend' : health === 'online' ? 'backend online' : 'backend degraded'}
                </Pill>
              </div>
              <p className="mt-1 text-sm text-slate-400">A live control room for agents, rooms, files, approvals, and operator intervention.</p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <a className="rounded-full border border-white/10 px-4 py-2 text-sm text-slate-300 transition hover:border-cyan-300/40 hover:text-white" href={`${API_URL}/docs`} target="_blank" rel="noreferrer">
              API docs
            </a>
            <button onClick={refresh} className="inline-flex items-center gap-2 rounded-full border border-white/10 px-4 py-2 text-sm text-slate-300 transition hover:border-cyan-300/40 hover:text-white">
              <RefreshCw className={classNames('size-4', loading && 'animate-spin')} /> Refresh
            </button>
            <button onClick={createDemoWorkspace} disabled={creating} className="inline-flex items-center gap-2 rounded-full bg-cyan-300 px-4 py-2 text-sm font-semibold text-slate-950 shadow-lg shadow-cyan-500/20 transition hover:bg-cyan-200 disabled:cursor-not-allowed disabled:opacity-60">
              {creating ? <Loader2 className="size-4 animate-spin" /> : <Plus className="size-4" />} New room
            </button>
          </div>
        </header>

        {error && (
          <div className="mb-6 rounded-2xl border border-rose-400/20 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">
            {error}
          </div>
        )}

        {createdToken && (
          <div className="mb-6 rounded-2xl border border-cyan-400/20 bg-cyan-500/10 px-4 py-3 text-sm text-cyan-50">
            Created room <span className="font-mono">{createdToken.workspaceId}</span>. Workspace token: <span className="font-mono text-cyan-200">{createdToken.token}</span>
          </div>
        )}

        <section className="mb-6 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <StatCard label="Backend" value={health === 'online' ? 'Online' : health === 'checking' ? 'Checking' : 'Degraded'} helper={`API endpoint ${API_URL}`} icon={RadioTower} tone="emerald" />
          <StatCard label="Rooms" value={workspaces.length} helper="Persistent collaboration workspaces" icon={MessageSquareText} tone="cyan" />
          <StatCard label="Agents" value={totals.agents} helper={`${totals.online} currently online`} icon={Bot} tone="violet" />
          <StatCard label="Storage" value="Postgres" helper="Timelines and workspace state persisted" icon={Database} tone="amber" />
        </section>

        <div className="grid flex-1 gap-6 xl:grid-cols-[1.35fr_0.85fr]">
          <SectionCard
            title="Bridge rooms"
            subtitle="Live rooms backed by the deployed API and database. Agents join these with a workspace token."
            action={<Pill tone="blue">{loading ? 'syncing' : 'live'}</Pill>}
          >
            {loading && workspaces.length === 0 ? (
              <div className="flex h-72 items-center justify-center text-slate-400">
                <Loader2 className="mr-2 size-5 animate-spin" /> Loading rooms…
              </div>
            ) : workspaces.length === 0 ? (
              <div className="flex h-72 flex-col items-center justify-center rounded-2xl border border-dashed border-white/10 bg-white/[0.03] text-center">
                <Boxes className="mb-4 size-10 text-slate-500" />
                <h3 className="text-base font-semibold text-white">No rooms yet</h3>
                <p className="mt-2 max-w-md text-sm text-slate-400">Create the first Agent Bridge room, then connect OpenClaw, Hermes, Codex, or another adapter into it.</p>
                <button onClick={createDemoWorkspace} disabled={creating} className="mt-5 inline-flex items-center gap-2 rounded-full bg-cyan-300 px-4 py-2 text-sm font-semibold text-slate-950 transition hover:bg-cyan-200">
                  <Plus className="size-4" /> Create first room
                </button>
              </div>
            ) : (
              <div className="space-y-3">
                {workspaces.map((workspace) => (
                  <article key={workspace.workspaceId} className="rounded-2xl border border-white/10 bg-white/[0.045] p-4 transition hover:border-cyan-300/30 hover:bg-white/[0.07]">
                    <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                      <div>
                        <div className="flex flex-wrap items-center gap-2">
                          <h3 className="text-base font-semibold text-white">{workspace.name}</h3>
                          <Pill tone={workspace.status === 'active' ? 'green' : 'slate'}>{workspace.status}</Pill>
                        </div>
                        <p className="mt-2 font-mono text-xs text-slate-500">{workspace.workspaceId}</p>
                        <div className="mt-4 flex flex-wrap gap-2 text-xs text-slate-400">
                          <span className="inline-flex items-center gap-1"><Clock3 className="size-3.5" /> activity {timeAgo(workspace.lastActivityAt)}</span>
                          <span className="inline-flex items-center gap-1"><Users className="size-3.5" /> {workspace.agents?.length || 0} agents</span>
                          {workspace.slug && <span className="inline-flex items-center gap-1"><KeyRound className="size-3.5" /> slug {workspace.slug}</span>}
                        </div>
                      </div>
                      <a href={`/${workspace.workspaceId}`} className="inline-flex items-center justify-center rounded-full border border-white/10 px-4 py-2 text-sm text-slate-200 transition hover:border-cyan-300/40 hover:text-white">
                        Open room
                      </a>
                    </div>
                    {!!workspace.agents?.length && (
                      <div className="mt-4 grid gap-2 md:grid-cols-2">
                        {workspace.agents.map((agent) => (
                          <div key={agent.agentName} className="flex items-center justify-between gap-3 rounded-xl bg-slate-950/50 px-3 py-2">
                            <div className="flex items-center gap-2">
                              <CircleDot className={classNames('size-4', agent.status === 'online' ? 'text-emerald-300' : 'text-slate-500')} />
                              <div>
                                <p className="text-sm font-medium text-slate-100">{agent.agentName}</p>
                                <p className="text-xs text-slate-500">{agent.agentType || 'agent'} · {agent.role || 'member'}</p>
                              </div>
                            </div>
                            <span className="text-xs text-slate-500">{timeAgo(agent.lastHeartbeatAt)}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </article>
                ))}
              </div>
            )}
          </SectionCard>

          <div className="space-y-6">
            <SectionCard title="Control surfaces" subtitle="What this bridge is wired to expose as connectors come online.">
              <div className="grid gap-3">
                {[
                  { icon: Bot, label: 'Agent presence', value: `${totals.online}/${totals.agents} online`, tone: 'green' as const },
                  { icon: MessageSquareText, label: 'Room timelines', value: 'append-only events', tone: 'blue' as const },
                  { icon: FileText, label: 'File exchange', value: 'API mounted', tone: 'slate' as const },
                  { icon: ShieldCheck, label: 'Approvals', value: 'operator gated', tone: 'amber' as const },
                ].map((item) => (
                  <div key={item.label} className="flex items-center justify-between rounded-2xl border border-white/10 bg-white/[0.04] p-3">
                    <div className="flex items-center gap-3">
                      <div className="rounded-xl bg-white/5 p-2 text-cyan-200"><item.icon className="size-4" /></div>
                      <span className="text-sm text-slate-200">{item.label}</span>
                    </div>
                    <Pill tone={item.tone}>{item.value}</Pill>
                  </div>
                ))}
              </div>
            </SectionCard>

            <SectionCard title="Connector quick start" subtitle="Point agents at this bridge API and room token.">
              <div className="space-y-3 rounded-2xl bg-black/35 p-4 font-mono text-xs text-slate-300 ring-1 ring-white/10">
                <div><span className="text-slate-500">BRIDGE_API=</span>{API_URL}</div>
                <div><span className="text-slate-500">DASHBOARD=</span>{typeof window !== 'undefined' ? window.location.origin : 'http://host:3011'}</div>
                <div><span className="text-slate-500">AUTH=</span>workspace token per room</div>
              </div>
              <div className="mt-4 grid grid-cols-2 gap-3 text-sm">
                <div className="rounded-2xl bg-white/[0.04] p-3 ring-1 ring-white/10">
                  <TerminalSquare className="mb-2 size-5 text-cyan-200" /> OpenClaw adapter
                </div>
                <div className="rounded-2xl bg-white/[0.04] p-3 ring-1 ring-white/10">
                  <GitBranch className="mb-2 size-5 text-violet-200" /> Hermes adapter
                </div>
                <div className="rounded-2xl bg-white/[0.04] p-3 ring-1 ring-white/10">
                  <HardDriveUpload className="mb-2 size-5 text-emerald-200" /> File bridge
                </div>
                <div className="rounded-2xl bg-white/[0.04] p-3 ring-1 ring-white/10">
                  <Archive className="mb-2 size-5 text-amber-200" /> Audit log
                </div>
              </div>
            </SectionCard>

            <SectionCard title="System checks">
              <div className="space-y-3 text-sm">
                <div className="flex items-center justify-between"><span className="text-slate-400">Frontend</span><span className="inline-flex items-center gap-2 text-emerald-200"><CheckCircle2 className="size-4" /> online :3011</span></div>
                <div className="flex items-center justify-between"><span className="text-slate-400">Backend API</span><span className="inline-flex items-center gap-2 text-emerald-200"><CheckCircle2 className="size-4" /> {health === 'online' ? 'online :3010' : 'checking'}</span></div>
                <div className="flex items-center justify-between"><span className="text-slate-400">Database</span><span className="inline-flex items-center gap-2 text-emerald-200"><Database className="size-4" /> postgres</span></div>
                <div className="flex items-center justify-between"><span className="text-slate-400">ClawDeck isolation</span><span className="inline-flex items-center gap-2 text-cyan-200"><Globe2 className="size-4" /> separate ports</span></div>
              </div>
            </SectionCard>
          </div>
        </div>

        <footer className="py-6 text-center text-xs text-slate-600">
          Agent Bridge runs separately from ClawDeck. Backend :3010 · Dashboard :3011 · ClawDeck stays on :3000/:3001.
        </footer>
      </div>
    </main>
  );
}
