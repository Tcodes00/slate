'use client';

import { useEffect, useState } from 'react';
import { CheckCircle2, Server, Sparkles, Trash2, XCircle } from 'lucide-react';
import { createModelEndpoint, deleteModelEndpoint, fetchAIStudioSettings, fetchModelEndpoints, testModelEndpoint, updateAIStudioSettings } from '../lib/api';

const ROLE_RANK = { read_only: 0, standard_user: 1, manager: 2, org_admin: 3 };
const atLeast = (role, min) => (ROLE_RANK[role] ?? 0) >= ROLE_RANK[min];

// Only the Anthropic list is curated per-model — OpenAI/Gemini use a
// free-text field instead, since which of their models is actually live
// changes more often than this file would keep up with.
const ANTHROPIC_MODELS = [
  { value: '', label: "Use Anthropic's own default" },
  { value: 'claude-haiku-4-5-20251001', label: 'Claude Haiku 4.5 — fastest, cheapest' },
  { value: 'claude-sonnet-5', label: 'Claude Sonnet 5 — balanced (default)' },
  { value: 'claude-opus-4-8', label: 'Claude Opus 4.8 — most capable' },
];

function EndpointsPanel({ onChanged }) {
  const EMPTY = { name: '', kind: 'chat', base_url: '', model: '', api_key: '', auth_header: 'Authorization' };
  const [endpoints, setEndpoints] = useState(null); // null until loaded, and for anyone who isn't an org admin
  const [form, setForm] = useState(EMPTY);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [tests, setTests] = useState({});

  const load = () => fetchModelEndpoints().then(setEndpoints).catch(() => setEndpoints(null));
  useEffect(() => { load(); }, []);

  async function add(e) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      await createModelEndpoint(form);
      setForm(EMPTY);
      await load();
      onChanged();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function test(id) {
    setTests((t) => ({ ...t, [id]: { running: true } }));
    try {
      const result = await testModelEndpoint(id);
      setTests((t) => ({ ...t, [id]: result }));
    } catch (err) {
      setTests((t) => ({ ...t, [id]: { ok: false, error: err.message } }));
    }
  }

  async function remove(id) {
    setError('');
    try {
      await deleteModelEndpoint(id);
      await load();
      onChanged();
    } catch (err) {
      setError(err.message);
    }
  }

  if (endpoints === null) return null;

  return (
    <div className="slate-chart-card" style={{ maxWidth: 520, marginTop: 16 }}>
      <div className="slate-card-title" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <Server size={13} /> Custom &amp; local models
      </div>
      <p style={{ fontSize: 12.5, color: 'var(--ink-faint)', marginBottom: 12 }}>
        Point this workspace at your own OpenAI-compatible model server — vLLM, Ollama, LM Studio, a LiteLLM gateway, a
        fine-tuned model behind any of them. Chat endpoints then appear above as selectable providers. API keys are
        encrypted and never shown again.
      </p>

      {endpoints.length === 0 && <div className="slate-empty-note">No custom endpoints yet.</div>}
      <div className="slate-flag-list" style={{ marginBottom: 14 }}>
        {endpoints.map((e) => (
          <div className="slate-flag-row" key={e.id}>
            <div className="slate-flag-info">
              <div className="slate-flag-label">{e.name} <span className="slate-plugin-version">{e.kind}</span></div>
              <div className="slate-flag-desc" style={{ wordBreak: 'break-all' }}>{e.base_url} · {e.model}{e.has_api_key ? ' · key saved' : ''}</div>
              {tests[e.id] && !tests[e.id].running && (
                <div className="slate-flag-desc" style={{ color: tests[e.id].ok ? '#1E9E5A' : '#B3261E' }}>
                  {tests[e.id].ok ? `Works — ${tests[e.id].latency_ms} ms` : `Failed — ${tests[e.id].error}`}
                </div>
              )}
            </div>
            <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => test(e.id)}>Test</button>
            <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" aria-label={`Delete ${e.name}`} onClick={() => remove(e.id)}><Trash2 size={13} /></button>
          </div>
        ))}
      </div>

      <form onSubmit={add} className="slate-branding-form">
        <label className="slate-branding-label" htmlFor="ep-name">Name</label>
        <input id="ep-name" placeholder="Acme vLLM" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
        <label className="slate-branding-label" htmlFor="ep-url">Base URL</label>
        <input id="ep-url" placeholder="http://vllm.internal:8000/v1" value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} required />
        <label className="slate-branding-label" htmlFor="ep-model">Model</label>
        <input id="ep-model" placeholder="meta-llama/Llama-3.1-8B-Instruct" value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })} required />
        <label className="slate-branding-label" htmlFor="ep-key">API key (optional)</label>
        <input id="ep-key" type="password" autoComplete="new-password" placeholder="Leave blank if the server needs none" value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} />
        {error && <div className="slate-login-error" style={{ marginTop: 8 }}>{error}</div>}
        <div className="slate-modal-row" style={{ marginTop: 12 }}>
          <button type="submit" className="slate-btn slate-btn-primary" disabled={busy}>{busy ? 'Adding…' : 'Add endpoint'}</button>
        </div>
      </form>
    </div>
  );
}

export default function AIStudioView({ userRole }) {
  const [settings, setSettings] = useState(null);
  const [draftModel, setDraftModel] = useState('');
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const canEdit = atLeast(userRole, 'org_admin');

  useEffect(() => {
    fetchAIStudioSettings().then((data) => {
      setSettings(data);
      setDraftModel(data.model || '');
    });
  }, []);

  async function selectProvider(provider) {
    setSaving(true);
    setSaved(false);
    try {
      const updated = await updateAIStudioSettings(provider, '');
      setSettings(updated);
      setDraftModel(updated.model || '');
      setSaved(true);
    } finally {
      setSaving(false);
    }
  }

  async function saveModel(e) {
    e.preventDefault();
    setSaving(true);
    setSaved(false);
    try {
      const updated = await updateAIStudioSettings(settings.provider, draftModel);
      setSettings(updated);
      setSaved(true);
    } finally {
      setSaving(false);
    }
  }

  if (!settings) return <div className="slate-empty-note">Loading settings…</div>;

  return (
    <>
    <div className="slate-chart-card" style={{ maxWidth: 520 }}>
      <div className="slate-card-title" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <Sparkles size={13} /> AI provider & model
      </div>
      <p style={{ fontSize: 12.5, color: 'var(--ink-faint)', marginBottom: 12 }}>
        This changes which provider and model actually answer the next AI Copilot question, Agent
        Builder invocation, and multi-agent hand-off run for this workspace — not a display setting.
      </p>

      <div className="slate-plugin-list" style={{ marginBottom: 14 }}>
        {settings.available_providers.map((p) => (
          <button
            type="button"
            key={p.key}
            className="slate-plugin-card"
            style={{ textAlign: 'left', cursor: canEdit ? 'pointer' : 'default', border: settings.provider === p.key ? '1px solid var(--accent)' : undefined, boxShadow: settings.provider === p.key ? '0 0 0 1px var(--accent)' : undefined }}
            disabled={!canEdit || saving}
            onClick={() => selectProvider(p.key)}
          >
            <div className="slate-plugin-icon">
              {p.is_configured ? <CheckCircle2 size={16} color="#1E9E5A" /> : <XCircle size={16} color="var(--ink-faint)" />}
            </div>
            <div className="slate-plugin-body">
              <div className="slate-plugin-name">{p.name} {settings.provider === p.key && <span className="slate-plugin-version">selected</span>}</div>
              <div className="slate-flag-desc">
                {p.is_configured ? `Ready — default model ${p.default_model}` : 'No API key configured on the server for this provider'}
              </div>
            </div>
          </button>
        ))}
      </div>

      <form onSubmit={saveModel}>
        <label className="slate-branding-label" htmlFor="ai-model">Model</label>
        {settings.provider === 'anthropic' ? (
          <select
            id="ai-model" value={draftModel} onChange={(e) => setDraftModel(e.target.value)} disabled={!canEdit || saving}
            style={{ width: '100%', border: '1px solid var(--line)', borderRadius: 8, padding: '10px 12px', fontSize: 13 }}
          >
            {ANTHROPIC_MODELS.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
          </select>
        ) : (
          <input
            id="ai-model" value={draftModel} onChange={(e) => setDraftModel(e.target.value)}
            placeholder={`Leave blank for the default (${settings.available_providers.find((p) => p.key === settings.provider)?.default_model})`}
            disabled={!canEdit || saving}
          />
        )}
        {canEdit && (
          <button className="slate-btn slate-btn-primary slate-btn-sm" type="submit" disabled={saving} style={{ marginTop: 10, width: 'auto' }}>
            {saving ? 'Saving…' : 'Save model'}
          </button>
        )}
      </form>

      <div style={{ marginTop: 10, fontSize: 12, color: 'var(--ink-faint)' }}>
        Currently answering with: <span className="num">{settings.provider} / {settings.model}</span>
      </div>
      {saved && <div className="slate-generate-invoice-note" style={{ marginTop: 10 }}>Saved — takes effect on the next call.</div>}
      {!canEdit && <div className="slate-empty-note" style={{ marginTop: 10 }}>Only Org Admins can change this.</div>}
    </div>
    {canEdit && <EndpointsPanel onChanged={() => fetchAIStudioSettings().then(setSettings)} />}
    </>
  );
}
