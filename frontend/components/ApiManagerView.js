'use client';

import { useEffect, useState } from 'react';
import { Copy, Plus } from 'lucide-react';
import { createApiKey, createSelfServeKey, fetchApiKeys, revokeApiKey } from '../lib/api';

const ROLE_RANK = { read_only: 0, standard_user: 1, manager: 2, org_admin: 3 };
const atLeast = (role, min) => (ROLE_RANK[role] ?? 0) >= ROLE_RANK[min];

function NewKeyReveal({ created, onClose }) {
  return (
    <div className="slate-modal-overlay" onClick={onClose}>
      <div className="slate-modal" onClick={(e) => e.stopPropagation()}>
        <div className="slate-wordmark" style={{ marginBottom: 0, fontSize: 15 }}>Key created</div>
        <p style={{ fontSize: 12.5, color: 'var(--ink-soft)' }}>
          Copy this now — it won't be shown again. Only the prefix is kept on file after this.
        </p>
        <div className="num" style={{ background: 'var(--paper)', border: '1px solid var(--line)', borderRadius: 8, padding: 10, fontSize: 12, wordBreak: 'break-all' }}>
          {created.full_key}
        </div>
        <button
          className="slate-btn slate-btn-ghost slate-btn-sm"
          onClick={() => navigator.clipboard?.writeText(created.full_key)}
        >
          <Copy size={12} /> Copy
        </button>
        <button className="slate-btn slate-btn-primary" onClick={onClose}>Done</button>
      </div>
    </div>
  );
}

function NewKeyModal({ onClose, onCreate }) {
  const [name, setName] = useState('');
  const [saving, setSaving] = useState(false);
  async function submit(e) {
    e.preventDefault();
    setSaving(true);
    try { await onCreate(name); } finally { setSaving(false); }
  }
  return (
    <div className="slate-modal-overlay" onClick={onClose}>
      <form className="slate-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="slate-wordmark" style={{ marginBottom: 0, fontSize: 15 }}>New API key</div>
        <input placeholder="What's this key for?" value={name} onChange={(e) => setName(e.target.value)} required />
        <div className="slate-modal-row">
          <button type="button" className="slate-btn slate-btn-ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="slate-btn slate-btn-primary" disabled={saving}>{saving ? 'Creating…' : 'Create key'}</button>
        </div>
      </form>
    </div>
  );
}

export default function ApiManagerView({ userRole }) {
  const [keys, setKeys] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [revealed, setRevealed] = useState(null);
  const isAdmin = atLeast(userRole, 'org_admin');
  const canSelfServe = atLeast(userRole, 'standard_user');

  function load() {
    setLoading(true);
    fetchApiKeys().then(setKeys).finally(() => setLoading(false));
  }
  useEffect(load, []);

  async function doRevoke(key) {
    await revokeApiKey(key.id);
    load();
  }

  if (loading) return <div className="slate-empty-note">Loading API keys…</div>;

  return (
    <div>
      <p style={{ fontSize: 12.5, color: 'var(--ink-faint)', marginBottom: 14 }}>
        API Manager (admin-issued) and Developer Portal (self-serve) share this same key system —
        the interactive API reference lives at the backend's <code>/docs</code> (FastAPI's real,
        auto-generated OpenAPI explorer), always in sync with the actual endpoints.
      </p>

      <table className="slate-invoice-table">
        <thead><tr><th>Name</th><th>Key</th><th>Type</th><th>Uses</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {keys.map((k) => (
            <tr key={k.id}>
              <td>{k.name}</td>
              <td className="num">{k.key_prefix}</td>
              <td>{k.is_self_serve ? 'Self-serve' : 'Admin-issued'}</td>
              <td className="num">{k.usage_count}</td>
              <td><span className={`slate-status-badge ${k.revoked ? 'overdue' : 'paid'}`}>{k.revoked ? 'Revoked' : 'Active'}</span></td>
              <td>{isAdmin && !k.revoked && <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => doRevoke(k)}>Revoke</button>}</td>
            </tr>
          ))}
          {keys.length === 0 && <tr><td colSpan={6} className="slate-empty-note">No API keys yet.</td></tr>}
        </tbody>
      </table>

      {(isAdmin || canSelfServe) && (
        <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setShowCreate(true)} style={{ marginTop: 14 }}>
          <Plus size={13} /> New key
        </button>
      )}

      {showCreate && (
        <NewKeyModal
          onClose={() => setShowCreate(false)}
          onCreate={async (name) => {
            const created = isAdmin ? await createApiKey(name) : await createSelfServeKey(name);
            setShowCreate(false);
            setRevealed(created);
            load();
          }}
        />
      )}
      {revealed && <NewKeyReveal created={revealed} onClose={() => setRevealed(null)} />}
    </div>
  );
}
