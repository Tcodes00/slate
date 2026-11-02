'use client';

import { useEffect, useState } from 'react';
import { Plus } from 'lucide-react';
import { createQuote, fetchOrders, fetchQuotes, updateOrder, updateQuote } from '../lib/api';

const ROLE_RANK = { read_only: 0, standard_user: 1, manager: 2, org_admin: 3 };
const atLeast = (role, min) => (ROLE_RANK[role] ?? 0) >= ROLE_RANK[min];

function money(n) {
  return '$' + Number(n).toLocaleString('en-US');
}

function badgeClass(status) {
  return { Draft: 'draft', Sent: 'sent', Accepted: 'paid', Declined: 'overdue', Pending: 'sent', Fulfilled: 'paid', Cancelled: 'overdue' }[status] || 'draft';
}

function CreateQuoteModal({ onClose, onCreate }) {
  const [form, setForm] = useState({ company: '', description: '', amount: '' });
  const [saving, setSaving] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setSaving(true);
    try {
      await onCreate({ company: form.company, description: form.description, amount: Number(form.amount) || 0 });
      onClose();
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="slate-modal-overlay" onClick={onClose}>
      <form className="slate-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="slate-wordmark" style={{ marginBottom: 0, fontSize: 15 }}>New quote</div>
        <input placeholder="Company" value={form.company} onChange={(e) => setForm({ ...form, company: e.target.value })} required />
        <input placeholder="Description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} required />
        <input placeholder="Amount ($)" type="number" min="0" value={form.amount} onChange={(e) => setForm({ ...form, amount: e.target.value })} required />
        <div className="slate-modal-row">
          <button type="button" className="slate-btn slate-btn-ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="slate-btn slate-btn-primary" disabled={saving}>{saving ? 'Creating…' : 'Create quote'}</button>
        </div>
      </form>
    </div>
  );
}

export default function SalesView({ userRole }) {
  const [quotes, setQuotes] = useState([]);
  const [orders, setOrders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const canWrite = atLeast(userRole, 'standard_user');

  function load() {
    setLoading(true);
    Promise.all([fetchQuotes(), fetchOrders()]).then(([q, o]) => { setQuotes(q); setOrders(o); }).finally(() => setLoading(false));
  }
  useEffect(load, []);

  async function setQuoteStatus(quote, status) {
    await updateQuote(quote.id, status);
    load();
  }

  async function setOrderStatus(order, status) {
    await updateOrder(order.id, status);
    load();
  }

  if (loading) return <div className="slate-empty-note">Loading quotes and orders…</div>;

  return (
    <div>
      <div className="slate-card-title" style={{ marginBottom: 8 }}>Quotes</div>
      <table className="slate-invoice-table" style={{ marginBottom: 20 }}>
        <thead><tr><th>Quote</th><th>Company</th><th>Description</th><th>Amount</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {quotes.map((q) => (
            <tr key={q.id}>
              <td className="num">{q.quote_number}</td>
              <td>{q.company}</td>
              <td>{q.description}</td>
              <td className="num">{money(q.amount)}</td>
              <td><span className={`slate-status-badge ${badgeClass(q.status)}`}>{q.status}</span></td>
              <td>
                {canWrite && q.status === 'Draft' && (
                  <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setQuoteStatus(q, 'Sent')}>Mark sent</button>
                )}
                {canWrite && q.status === 'Sent' && (
                  <div className="slate-invoice-actions">
                    <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setQuoteStatus(q, 'Accepted')}>Accept</button>
                    <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setQuoteStatus(q, 'Declined')}>Decline</button>
                  </div>
                )}
              </td>
            </tr>
          ))}
          {quotes.length === 0 && <tr><td colSpan={6} className="slate-empty-note">No quotes yet.</td></tr>}
        </tbody>
      </table>
      {canWrite && (
        <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setShowCreate(true)} style={{ marginBottom: 24 }}>
          <Plus size={13} /> New quote
        </button>
      )}

      <div className="slate-card-title" style={{ marginBottom: 8 }}>Orders — created automatically when a quote is accepted</div>
      <table className="slate-invoice-table">
        <thead><tr><th>Order</th><th>Company</th><th>Description</th><th>Amount</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {orders.map((o) => (
            <tr key={o.id}>
              <td className="num">{o.order_number}</td>
              <td>{o.company}</td>
              <td>{o.description}</td>
              <td className="num">{money(o.amount)}</td>
              <td><span className={`slate-status-badge ${badgeClass(o.status)}`}>{o.status}</span></td>
              <td>
                {canWrite && o.status === 'Pending' && (
                  <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setOrderStatus(o, 'Fulfilled')}>Mark fulfilled</button>
                )}
              </td>
            </tr>
          ))}
          {orders.length === 0 && <tr><td colSpan={6} className="slate-empty-note">No orders yet — accept a quote to create one.</td></tr>}
        </tbody>
      </table>

      {showCreate && (
        <CreateQuoteModal
          onClose={() => setShowCreate(false)}
          onCreate={async (payload) => { await createQuote(payload); load(); }}
        />
      )}
    </div>
  );
}
