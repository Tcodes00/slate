'use client';

import { useCallback, useEffect, useState } from 'react';
import { Plus, Send, X } from 'lucide-react';
import { createAgent, fetchAgents, fetchHandoffs, invokeAgent, startHandoff, toggleAgent } from '../lib/api';
import FlagGate from './FlagGate';
import { useLiveEvent } from './LiveProvider';

const ROLE_RANK = { read_only: 0, standard_user: 1, manager: 2, org_admin: 3 };
const atLeast = (role, min) => (ROLE_RANK[role] ?? 0) >= ROLE_RANK[min];

function TestPanel({ agent, onClose }) {
  const [message, setMessage] = useState('');
  const [reply, setReply] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function send() {
    if (!message.trim()) return;
    setLoading(true);
    setError('');
    setReply('');
    try {
      const res = await invokeAgent(agent.id, message.trim());
      setReply(res.reply);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <div className="slate-panel-overlay" onClick={onClose} />
      <div className="slate-panel">
        <button className="slate-panel-close" onClick={onClose} aria-label="Close"><X size={16} /></button>
        <div className="slate-panel-eyebrow">Test this agent</div>
        <h3 className="slate-panel-title">{agent.name}</h3>
        <div className="slate-panel-muted" style={{ fontSize: 12, marginBottom: 14 }}>{agent.persona_prompt}</div>

        <div className="slate-activity-add-row">
          <input placeholder="Send a message…" value={message} onChange={(e) => setMessage(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && send()} />
          <button className="slate-btn slate-btn-primary slate-btn-sm" onClick={send} disabled={loading}><Send size={13} /></button>
        </div>

        {loading && <div className="slate-empty-note" style={{ marginTop: 10 }}>Thinking…</div>}
        {error && <div className="slate-login-error" style={{ marginTop: 10 }}>{error}</div>}
        {reply && (
          <div className="slate-chat-bubble assistant" style={{ marginTop: 10, maxWidth: '100%' }}>{reply}</div>
        )}
      </div>
    </>
  );
}

function NewAgentModal({ onClose, onCreate }) {
  const [form, setForm] = useState({ name: '', persona_prompt: '' });
  const [saving, setSaving] = useState(false);
  async function submit(e) {
    e.preventDefault();
    setSaving(true);
    try { await onCreate(form); onClose(); } finally { setSaving(false); }
  }
  return (
    <div className="slate-modal-overlay" onClick={onClose}>
      <form className="slate-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="slate-wordmark" style={{ marginBottom: 0, fontSize: 15 }}>New agent</div>
        <input placeholder="Agent name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
        <input placeholder="Persona / system prompt" value={form.persona_prompt} onChange={(e) => setForm({ ...form, persona_prompt: e.target.value })} required />
        <div className="slate-modal-row">
          <button type="button" className="slate-btn slate-btn-ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="slate-btn slate-btn-primary" disabled={saving}>{saving ? 'Creating…' : 'Create agent'}</button>
        </div>
      </form>
    </div>
  );
}

const STEP_BADGE = { pending: 'draft', running: 'sent', done: 'paid', failed: 'overdue' };
const RUN_BADGE = { planning: 'sent', running: 'sent', completed: 'paid', failed: 'overdue' };

function HandoffRunCard({ run, active, onOpen }) {
  return (
    <button type="button" className={`slate-handoff-card ${active ? 'active' : ''}`} onClick={() => onOpen(run)}>
      <div className="slate-handoff-card-top">
        <span className="slate-handoff-goal">{run.goal}</span>
        <span className={`slate-status-badge ${RUN_BADGE[run.status] || 'draft'}`}>{run.status}</span>
      </div>
      <div className="slate-flag-desc">
        {new Date(run.created_at).toLocaleString()} · {run.steps.length} step{run.steps.length === 1 ? '' : 's'}
      </div>
    </button>
  );
}

function HandoffDetail({ run }) {
  if (!run) return <div className="slate-empty-note">Select a run.</div>;
  return (
    <div className="slate-handoff-detail">
      <div className="slate-panel-eyebrow">Goal</div>
      <div style={{ fontWeight: 600, marginBottom: 10 }}>{run.goal}</div>
      {run.plan_summary && <div className="slate-flag-desc" style={{ marginBottom: 14 }}>{run.plan_summary}</div>}

      <div className="slate-handoff-steps">
        {run.steps.map((s) => (
          <div className={`slate-handoff-step ${s.status}`} key={s.id}>
            <div className="slate-handoff-step-marker">{s.step_number}</div>
            <div className="slate-handoff-step-body">
              <div className="slate-handoff-step-top">
                <span className="slate-handoff-step-title">{s.title}</span>
                <span className="slate-handoff-step-agent">{s.assigned_agent_name}</span>
              </div>
              <span className={`slate-status-badge ${STEP_BADGE[s.status] || 'draft'}`} style={{ marginTop: 4 }}>{s.status}</span>
              {s.output && <div className="slate-handoff-step-output">{s.output}</div>}
            </div>
          </div>
        ))}
        {run.steps.length === 0 && <div className="slate-empty-note">Planning the steps…</div>}
      </div>

      {run.final_result && (
        <div className="slate-handoff-result">
          <div className="slate-panel-eyebrow">Result</div>
          {run.final_result}
        </div>
      )}
      {run.error && <div className="slate-login-error" style={{ marginTop: 10 }}>{run.error}</div>}
    </div>
  );
}

function HandoffSection() {
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [goal, setGoal] = useState('');
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState('');
  const [openId, setOpenId] = useState(null);

  useEffect(() => {
    fetchHandoffs().then((data) => {
      setRuns(data);
      if (data.length) setOpenId((prev) => prev ?? data[0].id);
    }).finally(() => setLoading(false));
  }, []);

  const upsertRun = useCallback((updated) => {
    setRuns((prev) => {
      const idx = prev.findIndex((r) => r.id === updated.id);
      if (idx === -1) return [updated, ...prev];
      const next = [...prev];
      next[idx] = updated;
      return next;
    });
  }, []);

  useLiveEvent('agent_run.updated', upsertRun);
  useLiveEvent('agent_run.step', useCallback((data) => {
    setRuns((prev) => prev.map((r) => {
      if (r.id !== data.run_id) return r;
      const steps = [...r.steps];
      const idx = steps.findIndex((s) => s.id === data.step.id);
      if (idx === -1) steps.push(data.step); else steps[idx] = { ...steps[idx], ...data.step };
      return { ...r, steps };
    }));
  }, []));

  async function start(e) {
    e.preventDefault();
    if (!goal.trim()) return;
    setStarting(true);
    setError('');
    try {
      const run = await startHandoff(goal.trim());
      setGoal('');
      upsertRun(run);
      setOpenId(run.id);
    } catch (err) {
      setError(err.message);
    } finally {
      setStarting(false);
    }
  }

  const openRun = runs.find((r) => r.id === openId) || null;

  return (
    <div className="slate-handoff-section">
      <div className="slate-card-title">Multi-agent hand-off</div>
      <p className="slate-view-intro">
        Give it a goal — a planner breaks it into steps, hands each one to the best-fit agent above (or
        a general executor if none fit), and each step sees what the previous ones produced.
      </p>
      <form className="slate-activity-add-row" onSubmit={start} style={{ marginBottom: 16 }}>
        <input
          placeholder="e.g. Draft a win-back plan for our three stalled deals"
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          disabled={starting}
        />
        <button className="slate-btn slate-btn-primary slate-btn-sm" type="submit" disabled={starting || !goal.trim()}>
          {starting ? 'Starting…' : 'Run'}
        </button>
      </form>
      {error && <div className="slate-login-error" style={{ marginBottom: 12 }}>{error}</div>}

      {loading ? (
        <div className="slate-empty-note">Loading past runs…</div>
      ) : (
        <div className="slate-handoff-layout">
          <div className="slate-handoff-list">
            {runs.map((r) => <HandoffRunCard key={r.id} run={r} active={r.id === openId} onOpen={(run) => setOpenId(run.id)} />)}
            {runs.length === 0 && <div className="slate-empty-note">No hand-off runs yet.</div>}
          </div>
          <div className="slate-chart-card">
            <HandoffDetail run={openRun} />
          </div>
        </div>
      )}
    </div>
  );
}

export default function AgentBuilderView({ userRole }) {
  const [agents, setAgents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(null);
  const [showCreate, setShowCreate] = useState(false);
  const canWrite = atLeast(userRole, 'manager');

  function load() {
    setLoading(true);
    fetchAgents().then(setAgents).finally(() => setLoading(false));
  }
  useEffect(load, []);

  async function toggle(agent) {
    await toggleAgent(agent.id, !agent.enabled);
    load();
  }

  if (loading) return <div className="slate-empty-note">Loading agents…</div>;

  return (
    <div>
      <p style={{ fontSize: 12.5, color: 'var(--ink-faint)', marginBottom: 14 }}>
        Each agent is invoked through the same real Copilot infrastructure as the main AI Copilot —
        a configured persona, not a separate orchestration engine. Click one to test it live.
      </p>

      <table className="slate-invoice-table">
        <thead><tr><th>Agent</th><th>Persona</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {agents.map((a) => (
            <tr key={a.id}>
              <td onClick={() => setSelected(a)} style={{ cursor: 'pointer', fontWeight: 700 }}>{a.name}</td>
              <td style={{ maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{a.persona_prompt}</td>
              <td><span className={`slate-status-badge ${a.enabled ? 'paid' : 'draft'}`}>{a.enabled ? 'Enabled' : 'Disabled'}</span></td>
              <td>{canWrite && <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => toggle(a)}>{a.enabled ? 'Disable' : 'Enable'}</button>}</td>
            </tr>
          ))}
          {agents.length === 0 && <tr><td colSpan={4} className="slate-empty-note">No agents yet.</td></tr>}
        </tbody>
      </table>
      {canWrite && (
        <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setShowCreate(true)} style={{ marginTop: 14 }}>
          <Plus size={13} /> New agent
        </button>
      )}
      {selected && <TestPanel agent={selected} onClose={() => setSelected(null)} />}
      {showCreate && <NewAgentModal onClose={() => setShowCreate(false)} onCreate={async (p) => { await createAgent(p); load(); }} />}

      <div className="slate-section-divider" />
      <FlagGate flagKey="multi_agent_handoff" label="Multi-agent hand-off" userRole={userRole}>
        <HandoffSection />
      </FlagGate>
    </div>
  );
}
