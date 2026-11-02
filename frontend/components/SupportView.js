'use client';

import { useEffect, useState } from 'react';
import { Plus, X } from 'lucide-react';
import { addTicketMessage, createTicket, fetchTicketMessages, fetchTickets, updateTicket } from '../lib/api';

const ROLE_RANK = { read_only: 0, standard_user: 1, manager: 2, org_admin: 3 };
const atLeast = (role, min) => (ROLE_RANK[role] ?? 0) >= ROLE_RANK[min];
const STATUSES = ['Open', 'In Progress', 'Resolved', 'Closed'];

function priorityBadge(p) {
  return { Low: 'draft', Normal: 'sent', High: 'overdue', Urgent: 'overdue' }[p] || 'draft';
}

function CreateTicketModal({ onClose, onCreate }) {
  const [form, setForm] = useState({ subject: '', company: '', priority: 'Normal' });
  const [saving, setSaving] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setSaving(true);
    try {
      await onCreate(form);
      onClose();
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="slate-modal-overlay" onClick={onClose}>
      <form className="slate-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="slate-wordmark" style={{ marginBottom: 0, fontSize: 15 }}>New ticket</div>
        <input placeholder="Subject" value={form.subject} onChange={(e) => setForm({ ...form, subject: e.target.value })} required />
        <input placeholder="Company (optional)" value={form.company} onChange={(e) => setForm({ ...form, company: e.target.value })} />
        <select value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })}>
          <option>Low</option><option>Normal</option><option>High</option><option>Urgent</option>
        </select>
        <div className="slate-modal-row">
          <button type="button" className="slate-btn slate-btn-ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="slate-btn slate-btn-primary" disabled={saving}>{saving ? 'Creating…' : 'Create ticket'}</button>
        </div>
      </form>
    </div>
  );
}

function TicketPanel({ ticket, userRole, onClose, onChanged }) {
  const [messages, setMessages] = useState([]);
  const [reply, setReply] = useState('');
  const canWrite = atLeast(userRole, 'standard_user');

  function load() {
    fetchTicketMessages(ticket.id).then(setMessages);
  }
  useEffect(load, [ticket]);

  async function setStatus(status) {
    const updated = await updateTicket(ticket.id, { status });
    onChanged(updated);
  }

  async function sendReply() {
    if (!reply.trim()) return;
    await addTicketMessage(ticket.id, 'agent', reply.trim());
    setReply('');
    load();
    onChanged({ ...ticket, message_count: ticket.message_count + 1 });
  }

  return (
    <>
      <div className="slate-panel-overlay" onClick={onClose} />
      <div className="slate-panel">
        <button className="slate-panel-close" onClick={onClose} aria-label="Close"><X size={16} /></button>
        <div className="slate-panel-eyebrow">{ticket.company || 'No company linked'}</div>
        <h3 className="slate-panel-title">{ticket.subject}</h3>
        <span className={`slate-status-badge ${priorityBadge(ticket.priority)}`}>{ticket.priority}</span>

        <div className="slate-panel-stages" style={{ marginTop: 14 }}>
          {STATUSES.map((s) => (
            <button key={s} className={`slate-stage-btn ${ticket.status === s ? 'active' : ''}`} onClick={() => setStatus(s)} disabled={!canWrite}>
              {s}
            </button>
          ))}
        </div>

        <div className="slate-panel-section">
          <div className="slate-panel-label">Thread</div>
          {messages.length === 0 && <div className="slate-panel-muted">No messages yet.</div>}
          {messages.map((m) => (
            <div className="slate-activity-log-item" key={m.id}>
              <strong>{m.sender}</strong>: {m.content}
              <span className="slate-panel-muted"> · {new Date(m.created_at).toLocaleString()}</span>
            </div>
          ))}
          {canWrite ? (
            <div className="slate-activity-add-row">
              <input placeholder="Reply as agent…" value={reply} onChange={(e) => setReply(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && sendReply()} />
              <button className="slate-btn slate-btn-primary slate-btn-sm" onClick={sendReply}>Send</button>
            </div>
          ) : (
            <div className="slate-empty-note" style={{ marginTop: 8 }}>Read-only access — viewing only.</div>
          )}
        </div>
      </div>
    </>
  );
}

export default function SupportView({ userRole }) {
  const [tickets, setTickets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(null);
  const [showCreate, setShowCreate] = useState(false);
  const canWrite = atLeast(userRole, 'standard_user');

  function load() {
    setLoading(true);
    fetchTickets().then(setTickets).finally(() => setLoading(false));
  }
  useEffect(load, []);

  if (loading) return <div className="slate-empty-note">Loading tickets…</div>;

  return (
    <div>
      <table className="slate-invoice-table">
        <thead><tr><th>Subject</th><th>Company</th><th>Priority</th><th>Status</th><th>Messages</th></tr></thead>
        <tbody>
          {tickets.map((t) => (
            <tr key={t.id} onClick={() => setSelected(t)} style={{ cursor: 'pointer' }}>
              <td>{t.subject}</td>
              <td>{t.company || '—'}</td>
              <td><span className={`slate-status-badge ${priorityBadge(t.priority)}`}>{t.priority}</span></td>
              <td><span className="slate-status-badge sent">{t.status}</span></td>
              <td className="num">{t.message_count}</td>
            </tr>
          ))}
          {tickets.length === 0 && <tr><td colSpan={5} className="slate-empty-note">No tickets yet.</td></tr>}
        </tbody>
      </table>

      {canWrite && (
        <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setShowCreate(true)} style={{ marginTop: 14 }}>
          <Plus size={13} /> New ticket
        </button>
      )}

      {selected && (
        <TicketPanel
          ticket={selected}
          userRole={userRole}
          onClose={() => setSelected(null)}
          onChanged={(updated) => {
            setTickets(tickets.map((t) => (t.id === updated.id ? updated : t)));
            setSelected(updated);
          }}
        />
      )}

      {showCreate && (
        <CreateTicketModal onClose={() => setShowCreate(false)} onCreate={async (p) => { await createTicket(p); load(); }} />
      )}
    </div>
  );
}
