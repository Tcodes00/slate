'use client';

import { useEffect, useState } from 'react';
import { fetchAuditLog } from '../lib/api';

export default function AuditLogView({ userRole }) {
  const [entries, setEntries] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    fetchAuditLog().then(setEntries).catch((e) => setError(e.message));
  }, []);

  if (userRole !== 'org_admin') {
    return <div className="slate-empty-note">Only Org Admins can view the audit log.</div>;
  }
  if (error) return <div className="slate-login-error">{error}</div>;
  if (!entries) return <div className="slate-empty-note">Loading audit log…</div>;

  return (
    <div>
      <p style={{ fontSize: 12.5, color: 'var(--ink-faint)', marginBottom: 14 }}>
        Append-only — nothing in this codebase updates or deletes a row here. Currently logs
        purchase order approvals, teammate invites, and MFA changes; extend <code>log_audit()</code>{' '}
        calls to any other endpoint that should be tracked.
      </p>
      <table className="slate-invoice-table">
        <thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Resource</th></tr></thead>
        <tbody>
          {entries.map((e) => (
            <tr key={e.id}>
              <td className="num" style={{ whiteSpace: 'nowrap' }}>{new Date(e.created_at).toLocaleString()}</td>
              <td>{e.actor_name || 'system'}</td>
              <td><span className="slate-status-badge sent">{e.action}</span></td>
              <td>{e.resource_type}{e.resource_id ? ` #${e.resource_id}` : ''}</td>
            </tr>
          ))}
          {entries.length === 0 && <tr><td colSpan={4} className="slate-empty-note">No audit entries yet.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}
