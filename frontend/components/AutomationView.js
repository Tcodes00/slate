'use client';

import { useEffect, useState } from 'react';
import { Plus, Zap } from 'lucide-react';
import { createRule, fetchRules, toggleRule } from '../lib/api';
import WorkflowsPanel from './WorkflowsPanel';

const ROLE_RANK = { read_only: 0, standard_user: 1, manager: 2, org_admin: 3 };
const atLeast = (role, min) => (ROLE_RANK[role] ?? 0) >= ROLE_RANK[min];
const STAGES = ['Lead', 'Qualified', 'Proposal', 'Won'];

function NewRuleModal({ onClose, onCreate }) {
  const [form, setForm] = useState({ name: '', trigger_stage: 'Won', action_note: '' });
  const [saving, setSaving] = useState(false);
  async function submit(e) {
    e.preventDefault();
    setSaving(true);
    try { await onCreate(form); onClose(); } finally { setSaving(false); }
  }
  return (
    <div className="slate-modal-overlay" onClick={onClose}>
      <form className="slate-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="slate-wordmark" style={{ marginBottom: 0, fontSize: 15 }}>New automation rule</div>
        <input placeholder="Rule name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
        <div style={{ fontSize: 12, color: 'var(--ink-faint)' }}>When a CRM deal moves to:</div>
        <select value={form.trigger_stage} onChange={(e) => setForm({ ...form, trigger_stage: e.target.value })}>
          {STAGES.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <div style={{ fontSize: 12, color: 'var(--ink-faint)' }}>Create a task with this title:</div>
        <input placeholder="e.g. Schedule onboarding call" value={form.action_note} onChange={(e) => setForm({ ...form, action_note: e.target.value })} />
        <div className="slate-modal-row">
          <button type="button" className="slate-btn slate-btn-ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="slate-btn slate-btn-primary" disabled={saving}>{saving ? 'Creating…' : 'Create rule'}</button>
        </div>
      </form>
    </div>
  );
}

export default function AutomationView({ userRole }) {
  const [rules, setRules] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const canWrite = atLeast(userRole, 'manager');

  function load() {
    setLoading(true);
    fetchRules().then(setRules).finally(() => setLoading(false));
  }
  useEffect(load, []);

  async function toggle(rule) {
    await toggleRule(rule.id, !rule.enabled);
    load();
  }

  if (loading) return <div className="slate-empty-note">Loading rules…</div>;

  return (
    <div>
      <p style={{ fontSize: 12.5, color: 'var(--ink-faint)', marginBottom: 14 }}>
        Rules are the simple kind: one real, wired trigger — a CRM deal changing stage. "Runs" below
        counts actual executions, triggered from the CRM tab, not a simulated number. For multi-step
        automation (webhooks, schedules, HTTP calls, AI, branching) use Workflows further down.
      </p>

      <table className="slate-invoice-table">
        <thead><tr><th>Rule</th><th>Trigger</th><th>Action</th><th>Runs</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {rules.map((r) => (
            <tr key={r.id}>
              <td>{r.name}</td>
              <td>Deal → {r.trigger_stage}</td>
              <td>Create task: {r.action_note || '(default title)'}</td>
              <td className="num"><Zap size={11} style={{ verticalAlign: -1 }} /> {r.run_count}</td>
              <td><span className={`slate-status-badge ${r.enabled ? 'paid' : 'draft'}`}>{r.enabled ? 'Enabled' : 'Disabled'}</span></td>
              <td>{canWrite && <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => toggle(r)}>{r.enabled ? 'Disable' : 'Enable'}</button>}</td>
            </tr>
          ))}
          {rules.length === 0 && <tr><td colSpan={6} className="slate-empty-note">No rules yet.</td></tr>}
        </tbody>
      </table>
      {canWrite && (
        <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setShowCreate(true)} style={{ marginTop: 14 }}>
          <Plus size={13} /> New rule
        </button>
      )}
      {showCreate && <NewRuleModal onClose={() => setShowCreate(false)} onCreate={async (p) => { await createRule(p); load(); }} />}
      <WorkflowsPanel userRole={userRole} />
    </div>
  );
}
