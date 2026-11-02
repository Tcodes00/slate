'use client';

import { useEffect, useState } from 'react';
import { Plus, Shield } from 'lucide-react';
import { fetchTeam, inviteTeammate } from '../lib/api';

const ROLE_LABELS = {
  org_admin: 'Org Admin',
  manager: 'Manager',
  standard_user: 'Standard User',
  read_only: 'Read Only',
};

const ROLE_OPTIONS = ['read_only', 'standard_user', 'manager', 'org_admin'];

function InviteModal({ onClose, onInvite }) {
  const [form, setForm] = useState({ name: '', email: '', password: '', role: 'standard_user' });
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setError('');
    setSaving(true);
    try {
      await onInvite(form);
      onClose();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="slate-modal-overlay" onClick={onClose}>
      <form className="slate-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="slate-wordmark" style={{ marginBottom: 0, fontSize: 15 }}>
          Add teammate
        </div>
        <input placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
        <input type="email" placeholder="Email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} required />
        <input type="password" placeholder="Temporary password" minLength={6} value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required />
        <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
          {ROLE_OPTIONS.map((r) => (
            <option key={r} value={r}>{ROLE_LABELS[r]}</option>
          ))}
        </select>
        {error && <div className="slate-login-error">{error}</div>}
        <div className="slate-modal-row">
          <button type="button" className="slate-btn slate-btn-ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="slate-btn slate-btn-primary" disabled={saving}>{saving ? 'Adding…' : 'Add teammate'}</button>
        </div>
      </form>
    </div>
  );
}

export default function TeamView({ currentUser }) {
  const [team, setTeam] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showInvite, setShowInvite] = useState(false);
  const isAdmin = currentUser.role === 'org_admin';

  function load() {
    setLoading(true);
    fetchTeam().then(setTeam).finally(() => setLoading(false));
  }

  useEffect(load, []);

  if (loading) return <div className="slate-empty-note">Loading team…</div>;

  return (
    <div>
      <table className="slate-invoice-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Email</th>
            <th>Role</th>
          </tr>
        </thead>
        <tbody>
          {team.map((t) => (
            <tr key={t.id}>
              <td>{t.name}{t.id === currentUser.id ? ' (you)' : ''}</td>
              <td>{t.email}</td>
              <td><span className="slate-status-badge sent">{ROLE_LABELS[t.role] || t.role}</span></td>
            </tr>
          ))}
        </tbody>
      </table>

      <div style={{ marginTop: 14 }}>
        {isAdmin ? (
          <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setShowInvite(true)}>
            <Plus size={13} /> Add teammate
          </button>
        ) : (
          <div className="slate-empty-note">
            <Shield size={12} style={{ verticalAlign: -2 }} /> Only Org Admins can add teammates.
          </div>
        )}
      </div>

      {showInvite && (
        <InviteModal
          onClose={() => setShowInvite(false)}
          onInvite={async (payload) => {
            await inviteTeammate(payload);
            load();
          }}
        />
      )}
    </div>
  );
}
