'use client';

import { useEffect, useState } from 'react';
import { Download, Plus } from 'lucide-react';
import { createInvoice, downloadInvoicePdf, fetchFinanceSummary, fetchInvoices, updateInvoice } from '../lib/api';

const ROLE_RANK = { read_only: 0, standard_user: 1, manager: 2, org_admin: 3 };
const atLeast = (role, min) => (ROLE_RANK[role] ?? 0) >= ROLE_RANK[min];

function money(n) {
  return '$' + Number(n).toLocaleString('en-US');
}

function badgeClass(inv) {
  if (inv.is_overdue) return 'overdue';
  return inv.status.toLowerCase();
}

function badgeLabel(inv) {
  if (inv.is_overdue) return 'Overdue';
  return inv.status;
}

function CreateInvoiceModal({ onClose, onCreate }) {
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
        <div className="slate-wordmark" style={{ marginBottom: 0, fontSize: 15 }}>
          New invoice
        </div>
        <input placeholder="Company" value={form.company} onChange={(e) => setForm({ ...form, company: e.target.value })} required />
        <input placeholder="Description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} required />
        <input placeholder="Amount ($)" type="number" min="0" value={form.amount} onChange={(e) => setForm({ ...form, amount: e.target.value })} required />
        <div className="slate-modal-row">
          <button type="button" className="slate-btn slate-btn-ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="slate-btn slate-btn-primary" disabled={saving}>{saving ? 'Creating…' : 'Create invoice'}</button>
        </div>
      </form>
    </div>
  );
}

export default function FinanceView({ userRole }) {
  const [invoices, setInvoices] = useState([]);
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const canWrite = atLeast(userRole, 'standard_user');

  function load() {
    setLoading(true);
    Promise.all([fetchInvoices(), fetchFinanceSummary()])
      .then(([inv, sum]) => {
        setInvoices(inv);
        setSummary(sum);
      })
      .finally(() => setLoading(false));
  }

  useEffect(load, []);

  async function advance(inv) {
    const next = inv.status === 'Draft' ? 'Sent' : inv.status === 'Sent' ? 'Paid' : null;
    if (!next) return;
    await updateInvoice(inv.id, { status: next });
    load();
  }

  if (loading) return <div className="slate-empty-note">Loading invoices…</div>;

  return (
    <div>
      {summary && (
        <div className="slate-summary-row">
          <div className="slate-kpi-card">
            <div className="slate-kpi-label">Outstanding</div>
            <div className="slate-kpi-value num">{money(summary.outstanding)}</div>
          </div>
          <div className="slate-kpi-card">
            <div className="slate-kpi-label">Paid</div>
            <div className="slate-kpi-value num">{money(summary.paid)}</div>
          </div>
          <div className="slate-kpi-card">
            <div className="slate-kpi-label">Overdue</div>
            <div className="slate-kpi-value num">{summary.overdue_count}</div>
          </div>
        </div>
      )}

      {invoices.length === 0 ? (
        <div className="slate-empty-state">
          No invoices yet. Mark a CRM deal "Won" and generate one, or add one manually.
          {canWrite && (
            <div style={{ marginTop: 14 }}>
              <button className="slate-btn slate-btn-primary" style={{ width: 'auto', margin: '0 auto' }} onClick={() => setShowCreate(true)}>
                <Plus size={14} /> New invoice
              </button>
            </div>
          )}
        </div>
      ) : (
        <>
          <table className="slate-invoice-table">
            <thead>
              <tr>
                <th>Invoice</th>
                <th>Company</th>
                <th>Description</th>
                <th>Amount</th>
                <th>Status</th>
                <th>Due</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {invoices.map((inv) => (
                <tr key={inv.id}>
                  <td className="num">{inv.invoice_number}</td>
                  <td>{inv.company}</td>
                  <td>{inv.description}</td>
                  <td className="num">{money(inv.amount)}</td>
                  <td><span className={`slate-status-badge ${badgeClass(inv)}`}>{badgeLabel(inv)}</span></td>
                  <td>{inv.due_date ? new Date(inv.due_date).toLocaleDateString() : '—'}</td>
                  <td>
                    <div className="slate-invoice-actions">
                      {canWrite && inv.status !== 'Paid' && (
                        <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => advance(inv)}>
                          Mark {inv.status === 'Draft' ? 'sent' : 'paid'}
                        </button>
                      )}
                      <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => downloadInvoicePdf(inv.id, `${inv.invoice_number}.pdf`)} aria-label="Download PDF">
                        <Download size={12} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {canWrite && (
            <div style={{ marginTop: 14 }}>
              <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setShowCreate(true)}>
                <Plus size={13} /> New invoice
              </button>
            </div>
          )}
        </>
      )}

      {showCreate && (
        <CreateInvoiceModal
          onClose={() => setShowCreate(false)}
          onCreate={async (payload) => {
            await createInvoice(payload);
            load();
          }}
        />
      )}
    </div>
  );
}
