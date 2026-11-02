'use client';

import { useEffect, useState } from 'react';
import { KeyRound, Play, Plus, Trash2, Workflow as WorkflowIcon } from 'lucide-react';
import {
  createWorkflow, createWorkflowCredential, deleteWorkflow, deleteWorkflowCredential, fetchWorkflow, fetchWorkflowCredentials,
  fetchWorkflowRun, fetchWorkflowRuns, fetchWorkflows, rotateWebhookSecret, runWorkflow, setWorkflowEnabled, updateWorkflow, validateWorkflow,
} from '../lib/api';
import FlagGate from './FlagGate';

const TEMPLATES = {
  manual: {
    label: 'Blank — manual trigger',
    definition: {
      nodes: [{ id: 'trigger', type: 'trigger', params: { kind: 'manual' } }, { id: 'greet', type: 'set', params: { values: { message: 'Hello {{ trigger.name | default("world") }}' } } }],
      edges: [{ from: 'trigger', to: 'greet' }],
    },
  },
  webhook_ai: {
    label: 'Webhook → AI summary → notify the team',
    definition: {
      nodes: [
        { id: 'trigger', type: 'trigger', params: { kind: 'webhook' } },
        { id: 'summarize', type: 'ai_prompt', params: { prompt: 'Summarise this in one sentence: {{ trigger.text }}', max_tokens: 120 } },
        { id: 'tell', type: 'notify', params: { message: '{{ summarize.text }}' } },
      ],
      edges: [{ from: 'trigger', to: 'summarize' }, { from: 'summarize', to: 'tell' }],
    },
  },
  deal_won: {
    label: 'Deal won → create an onboarding ticket',
    definition: {
      nodes: [
        { id: 'trigger', type: 'trigger', params: { kind: 'event', event_type: 'deal.updated', filter: { stage: 'Won' } } },
        { id: 'ticket', type: 'create_record', params: { entity: 'ticket', fields: { subject: 'Onboard {{ trigger.data.name }}', priority: 'High', company: '{{ trigger.data.company }}' } } },
      ],
      edges: [{ from: 'trigger', to: 'ticket' }],
    },
  },
  health: {
    label: 'Every 5 minutes → check a URL, alert on failure',
    definition: {
      nodes: [
        { id: 'trigger', type: 'trigger', params: { kind: 'schedule', cron: '*/5 * * * *' } },
        { id: 'check', type: 'http_request', params: { method: 'GET', url: 'https://example.com/health' }, on_error: 'branch', retry: { max_attempts: 2, backoff_seconds: 2 } },
        { id: 'alert', type: 'notify', params: { message: 'Health check failed: {{ check.error }}' } },
      ],
      edges: [{ from: 'trigger', to: 'check' }, { from: 'check', to: 'alert', branch: 'error' }],
    },
  },
};

const pretty = (value) => JSON.stringify(value, null, 2);
const STATUS_CLASS = { succeeded: 'paid', running: 'sent', queued: 'sent', failed: 'overdue', skipped: 'draft' };

function RunView({ run }) {
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ fontSize: 12.5, marginBottom: 6 }}>
        Run #{run.id} <span className={`slate-status-badge ${STATUS_CLASS[run.status] || 'draft'}`}>{run.status}</span> · {run.trigger_type} · v{run.workflow_version}
        {run.error && <div className="slate-login-error" style={{ marginTop: 6 }}>{run.error}</div>}
      </div>
      <table className="slate-invoice-table">
        <thead><tr><th>Node</th><th>Type</th><th>Status</th><th>Tries</th><th>ms</th></tr></thead>
        <tbody>
          {(run.nodes || []).map((n) => (
            <tr key={n.node_id}>
              <td>
                <details>
                  <summary>{n.node_id}</summary>
                  {n.error && <div style={{ color: '#B3261E', fontSize: 12 }}>{n.error}</div>}
                  <pre style={{ fontSize: 11, maxWidth: 360, overflow: 'auto' }}>{pretty({ input: n.input, output: n.output })}</pre>
                </details>
              </td>
              <td>{n.node_type}</td>
              <td><span className={`slate-status-badge ${STATUS_CLASS[n.status] || 'draft'}`}>{n.status}</span></td>
              <td className="num">{n.attempts}</td>
              <td className="num">{n.duration_ms ?? ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CredentialsBox() {
  const EMPTY = { name: '', kind: 'bearer', secret: '', allowed_hosts: '', header_name: '', username: '' };
  const [creds, setCreds] = useState([]);
  const [form, setForm] = useState(EMPTY);
  const [error, setError] = useState('');
  const load = () => fetchWorkflowCredentials().then(setCreds).catch(() => {});
  useEffect(() => { load(); }, []);

  async function add(e) {
    e.preventDefault();
    setError('');
    try {
      await createWorkflowCredential({ ...form, allowed_hosts: form.allowed_hosts.split(',').map((h) => h.trim()).filter(Boolean) });
      setForm(EMPTY);
      load();
    } catch (err) {
      setError(err.message);
    }
  }

  return (
    <details style={{ marginTop: 18 }}>
      <summary style={{ cursor: 'pointer', fontSize: 13, fontWeight: 600 }}><KeyRound size={13} style={{ verticalAlign: -2 }} /> Credentials ({creds.length})</summary>
      <p style={{ fontSize: 12, color: 'var(--ink-faint)', margin: '8px 0' }}>
        Secrets HTTP nodes can use by name. They are encrypted, never shown again, and each can only be sent to the hosts you list.
      </p>
      {creds.map((c) => (
        <div className="slate-flag-row" key={c.id}>
          <div className="slate-flag-info">
            <div className="slate-flag-label">{c.name} <span className="slate-plugin-version">{c.kind}</span></div>
            <div className="slate-flag-desc">{c.allowed_hosts.join(', ')}</div>
          </div>
          <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" aria-label={`Delete credential ${c.name}`} onClick={() => deleteWorkflowCredential(c.id).then(load).catch((err) => setError(err.message))}><Trash2 size={13} /></button>
        </div>
      ))}
      <form onSubmit={add} className="slate-branding-form" style={{ maxWidth: 420, marginTop: 10 }}>
        <input id="cred-name" placeholder="Name, e.g. crm-api" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
        <select id="cred-kind" value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
          <option value="bearer">Bearer token</option>
          <option value="header">Custom header</option>
          <option value="basic">Basic auth</option>
        </select>
        {form.kind === 'header' && <input placeholder="Header name, e.g. X-API-Key" value={form.header_name} onChange={(e) => setForm({ ...form, header_name: e.target.value })} required />}
        {form.kind === 'basic' && <input placeholder="Username" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} required />}
        <input id="cred-secret" type="password" autoComplete="new-password" placeholder="Secret" value={form.secret} onChange={(e) => setForm({ ...form, secret: e.target.value })} required />
        <input id="cred-hosts" placeholder="Allowed hosts, comma separated (api.example.com, *.example.com)" value={form.allowed_hosts} onChange={(e) => setForm({ ...form, allowed_hosts: e.target.value })} required />
        {error && <div className="slate-login-error">{error}</div>}
        <button type="submit" className="slate-btn slate-btn-ghost slate-btn-sm">Add credential</button>
      </form>
    </details>
  );
}

function Editor({ workflow, onChanged, onDeleted }) {
  const [text, setText] = useState(pretty(workflow.definition));
  const [input, setInput] = useState('{}');
  const [problems, setProblems] = useState([]);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [runs, setRuns] = useState([]);
  const [run, setRun] = useState(null);
  const [secret, setSecret] = useState(workflow.webhook_secret || '');

  const loadRuns = () => fetchWorkflowRuns(workflow.id).then(setRuns).catch(() => {});
  useEffect(() => { loadRuns(); }, [workflow.id]); // eslint-disable-line react-hooks/exhaustive-deps

  function parse(source) {
    try {
      return JSON.parse(source);
    } catch (e) {
      setError(`Not valid JSON: ${e.message}`);
      return null;
    }
  }

  async function check() {
    setError('');
    const definition = parse(text);
    if (!definition) return;
    const result = await validateWorkflow(definition);
    setProblems(result.errors);
    setMessage(result.valid ? 'The definition is valid.' : '');
  }

  async function save() {
    setError('');
    setMessage('');
    const definition = parse(text);
    if (!definition) return;
    try {
      await updateWorkflow(workflow.id, { definition });
      setProblems([]);
      setMessage('Saved.');
      onChanged();
    } catch (err) {
      setError(err.message);
    }
  }

  async function execute() {
    setError('');
    const payload = parse(input);
    if (!payload) return;
    try {
      setRun(await runWorkflow(workflow.id, payload));
      loadRuns();
      onChanged();
    } catch (err) {
      setError(err.message);
    }
  }

  async function toggle() {
    setError('');
    try {
      await setWorkflowEnabled(workflow.id, !workflow.enabled);
      onChanged();
    } catch (err) {
      setError(err.message);
    }
  }

  return (
    <div className="slate-chart-card" style={{ marginTop: 14 }}>
      <div className="slate-card-title">{workflow.name} <span className="slate-plugin-version">v{workflow.version} · {workflow.trigger_type}</span></div>
      <div style={{ display: 'flex', gap: 8, margin: '8px 0', flexWrap: 'wrap' }}>
        <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" onClick={toggle}>{workflow.enabled ? 'Disable' : 'Enable'}</button>
        <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" aria-label="Delete workflow"
          onClick={() => deleteWorkflow(workflow.id).then(onDeleted).catch((err) => setError(err.message))}><Trash2 size={13} /> Delete</button>
      </div>

      {workflow.webhook_url && (
        <div style={{ fontSize: 12, marginBottom: 10, wordBreak: 'break-all' }}>
          Webhook: <code>{workflow.webhook_url}</code>
          {secret && <div style={{ marginTop: 4 }}>Signing secret (shown once — copy it now): <code>{secret}</code></div>}
          <div>
            <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" style={{ marginTop: 4 }}
              onClick={() => rotateWebhookSecret(workflow.id).then((r) => setSecret(r.webhook_secret)).catch((err) => setError(err.message))}>Rotate secret</button>
          </div>
        </div>
      )}

      <label className="slate-branding-label" htmlFor="wf-definition">Definition (JSON: nodes and edges)</label>
      <textarea id="wf-definition" value={text} onChange={(e) => setText(e.target.value)} spellCheck={false}
        style={{ width: '100%', minHeight: 220, fontFamily: 'var(--font-mono)', fontSize: 12 }} />
      {problems.length > 0 && <ul style={{ color: '#B3261E', fontSize: 12.5 }}>{problems.map((p) => <li key={p}>{p}</li>)}</ul>}
      <div style={{ display: 'flex', gap: 8, margin: '8px 0' }}>
        <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" onClick={check}>Validate</button>
        <button type="button" className="slate-btn slate-btn-primary slate-btn-sm" onClick={save}>Save</button>
      </div>

      <label className="slate-branding-label" htmlFor="wf-input">Test input (JSON)</label>
      <textarea id="wf-input" value={input} onChange={(e) => setInput(e.target.value)} spellCheck={false} style={{ width: '100%', minHeight: 56, fontFamily: 'var(--font-mono)', fontSize: 12 }} />
      <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" style={{ margin: '8px 0' }} onClick={execute}><Play size={12} /> Run now</button>

      {error && <div className="slate-login-error">{error}</div>}
      {message && !error && <div style={{ fontSize: 12.5, color: 'var(--ink-soft)' }}>{message}</div>}
      {run && <RunView run={run} />}

      <div className="slate-card-title" style={{ marginTop: 16 }}>Recent runs</div>
      {runs.length === 0 && <div className="slate-empty-note">No runs yet.</div>}
      {runs.slice(0, 10).map((r) => (
        <div key={r.id} className="slate-flag-row" style={{ padding: '8px 12px' }}>
          <div className="slate-flag-info">
            <div className="slate-flag-label">#{r.id} <span className={`slate-status-badge ${STATUS_CLASS[r.status] || 'draft'}`}>{r.status}</span> <span className="slate-plugin-version">{r.trigger_type}</span></div>
            {r.error && <div className="slate-flag-desc">{r.error}</div>}
          </div>
          <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => fetchWorkflowRun(r.id).then(setRun)}>Details</button>
        </div>
      ))}
    </div>
  );
}

export default function WorkflowsPanel({ userRole }) {
  const [workflows, setWorkflows] = useState(null);
  const [selected, setSelected] = useState(null);
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({ name: '', template: 'manual' });
  const [error, setError] = useState('');

  const load = () => fetchWorkflows().then(setWorkflows).catch(() => setWorkflows([]));
  useEffect(() => { load(); }, []);

  async function open(id) {
    setSelected(await fetchWorkflow(id));
  }

  async function create(e) {
    e.preventDefault();
    setError('');
    try {
      const created = await createWorkflow({ name: form.name, definition: TEMPLATES[form.template].definition });
      setAdding(false);
      setForm({ name: '', template: 'manual' });
      setSelected(created);
      load();
    } catch (err) {
      setError(err.message);
    }
  }

  async function refresh() {
    await load();
    if (selected) setSelected(await fetchWorkflow(selected.id));
  }

  return (
    <div style={{ marginTop: 28 }}>
      <div className="slate-card-title" style={{ display: 'flex', alignItems: 'center', gap: 6 }}><WorkflowIcon size={14} /> Workflows</div>
      <FlagGate flagKey="workflow_engine" label="Workflow engine" userRole={userRole}>
        <p style={{ fontSize: 12.5, color: 'var(--ink-faint)', margin: '6px 0 12px' }}>
          Graphs of steps: triggers (manual, signed webhook, schedule, or an event like a deal moving to Won) feeding HTTP, AI, branching,
          parallel and error-handling nodes. Values are wired together with {'{{ templates }}'} that can read data but never run code.
        </p>
        <table className="slate-invoice-table">
          <thead><tr><th>Workflow</th><th>Trigger</th><th>Status</th><th>Last run</th><th></th></tr></thead>
          <tbody>
            {(workflows || []).map((w) => (
              <tr key={w.id}>
                <td>{w.name}</td>
                <td>{w.trigger_type}</td>
                <td><span className={`slate-status-badge ${w.enabled ? 'paid' : 'draft'}`}>{w.enabled ? 'Enabled' : 'Disabled'}</span></td>
                <td>{w.last_status ? <span className={`slate-status-badge ${STATUS_CLASS[w.last_status] || 'draft'}`}>{w.last_status}</span> : '—'}</td>
                <td><button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => open(w.id)}>Open</button></td>
              </tr>
            ))}
            {workflows && workflows.length === 0 && <tr><td colSpan={5} className="slate-empty-note">No workflows yet.</td></tr>}
          </tbody>
        </table>

        {!adding && (
          <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" style={{ marginTop: 12 }} onClick={() => setAdding(true)}><Plus size={13} /> New workflow</button>
        )}
        {adding && (
          <form onSubmit={create} className="slate-branding-form" style={{ maxWidth: 420, marginTop: 12 }}>
            <input id="wf-name" placeholder="Workflow name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
            <select id="wf-template" value={form.template} onChange={(e) => setForm({ ...form, template: e.target.value })}>
              {Object.entries(TEMPLATES).map(([key, t]) => <option key={key} value={key}>{t.label}</option>)}
            </select>
            {error && <div className="slate-login-error">{error}</div>}
            <div style={{ display: 'flex', gap: 8 }}>
              <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setAdding(false)}>Cancel</button>
              <button type="submit" className="slate-btn slate-btn-primary slate-btn-sm">Create (starts disabled)</button>
            </div>
          </form>
        )}

        {selected && <Editor key={selected.id + ':' + selected.version} workflow={selected} onChanged={refresh} onDeleted={() => { setSelected(null); load(); }} />}
        <CredentialsBox />
      </FlagGate>
    </div>
  );
}
