'use client';

import { useEffect, useState } from 'react';
import { Plus } from 'lucide-react';
import { approvePO, createPO, createVendor, fetchPOs, fetchVendors, rejectPO } from '../lib/api';

const ROLE_RANK = { read_only: 0, standard_user: 1, manager: 2, org_admin: 3 };
const atLeast = (role, min) => (ROLE_RANK[role] ?? 0) >= ROLE_RANK[min];

function money(n) {
  return '$' + Number(n).toLocaleString('en-US');
}

function NewPOModal({ vendors, onClose, onCreate }) {
  const [form, setForm] = useState({ vendor_id: vendors[0]?.id || '', description: '', total: '' });
  const [saving, setSaving] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setSaving(true);
    try {
      await onCreate({ vendor_id: Number(form.vendor_id), description: form.description, total: Number(form.total) || 0 });
      onClose();
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="slate-modal-overlay" onClick={onClose}>
      <form className="slate-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="slate-wordmark" style={{ marginBottom: 0, fontSize: 15 }}>New purchase order</div>
        <select value={form.vendor_id} onChange={(e) => setForm({ ...form, vendor_id: e.target.value })} required>
          {vendors.map((v) => <option key={v.id} value={v.id}>{v.company}</option>)}
        </select>
        <input placeholder="Description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} required />
        <input placeholder="Total ($)" type="number" min="0" value={form.total} onChange={(e) => setForm({ ...form, total: e.target.value })} required />
        <div className="slate-modal-row">
          <button type="button" className="slate-btn slate-btn-ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="slate-btn slate-btn-primary" disabled={saving}>{saving ? 'Creating…' : 'Create PO'}</button>
        </div>
      </form>
    </div>
  );
}

function NewVendorModal({ onClose, onCreate }) {
  const [company, setCompany] = useState('');
  const [bankDetails, setBankDetails] = useState('');
  const [saving, setSaving] = useState(false);
  async function submit(e) {
    e.preventDefault();
    setSaving(true);
    try { await onCreate({ company, bank_details: bankDetails || undefined }); onClose(); } finally { setSaving(false); }
  }
  return (
    <div className="slate-modal-overlay" onClick={onClose}>
      <form className="slate-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="slate-wordmark" style={{ marginBottom: 0, fontSize: 15 }}>New vendor</div>
        <input placeholder="Company name" value={company} onChange={(e) => setCompany(e.target.value)} required />
        <input placeholder="Bank details (optional)" value={bankDetails} onChange={(e) => setBankDetails(e.target.value)} />
        <div style={{ fontSize: 11, color: 'var(--ink-faint)' }}>Encrypted at rest — only Org Admins can ever view it.</div>
        <div className="slate-modal-row">
          <button type="button" className="slate-btn slate-btn-ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="slate-btn slate-btn-primary" disabled={saving}>{saving ? 'Adding…' : 'Add vendor'}</button>
        </div>
      </form>
    </div>
  );
}

export default function ERPView({ userRole, currentUserName }) {
  const [vendors, setVendors] = useState([]);
  const [pos, setPOs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showPO, setShowPO] = useState(false);
  const [showVendor, setShowVendor] = useState(false);
  const [err, setErr] = useState('');
  const canWrite = atLeast(userRole, 'standard_user');
  const canApprove = atLeast(userRole, 'manager');

  function load() {
    setLoading(true);
    Promise.all([fetchVendors(), fetchPOs()]).then(([v, p]) => { setVendors(v); setPOs(p); }).finally(() => setLoading(false));
  }
  useEffect(load, []);

  async function approve(po) {
    setErr('');
    try { await approvePO(po.id); load(); } catch (e) { setErr(e.message); }
  }
  async function reject(po) {
    await rejectPO(po.id); load();
  }

  if (loading) return <div className="slate-empty-note">Loading procurement…</div>;

  return (
    <div>
      <div className="slate-card-title" style={{ marginBottom: 8 }}>Purchase orders</div>
      {err && <div className="slate-login-error" style={{ marginBottom: 10 }}>{err}</div>}
      <table className="slate-invoice-table" style={{ marginBottom: 12 }}>
        <thead><tr><th>Vendor</th><th>Description</th><th>Total</th><th>Requested by</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {pos.map((po) => (
            <tr key={po.id}>
              <td>{po.vendor}</td>
              <td>{po.description}</td>
              <td className="num">{money(po.total)}</td>
              <td>{po.requested_by}{po.approved_by ? ` · approved by ${po.approved_by}` : ''}</td>
              <td><span className={`slate-status-badge ${po.status === 'Approved' ? 'paid' : po.status === 'Rejected' ? 'overdue' : 'draft'}`}>{po.status}</span></td>
              <td>
                {canApprove && po.status === 'Pending' && (
                  <div className="slate-invoice-actions">
                    <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => approve(po)}>Approve</button>
                    <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => reject(po)}>Reject</button>
                  </div>
                )}
              </td>
            </tr>
          ))}
          {pos.length === 0 && <tr><td colSpan={6} className="slate-empty-note">No purchase orders yet.</td></tr>}
        </tbody>
      </table>
      {canWrite && vendors.length > 0 && (
        <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setShowPO(true)} style={{ marginBottom: 24 }}>
          <Plus size={13} /> New PO
        </button>
      )}

      <div className="slate-card-title" style={{ marginBottom: 8 }}>Vendors</div>
      <table className="slate-invoice-table">
        <thead><tr><th>Company</th><th>Payment terms</th><th>Bank details</th></tr></thead>
        <tbody>
          {vendors.map((v) => (
            <tr key={v.id}>
              <td>{v.company}</td>
              <td>{v.payment_terms}</td>
              <td>{v.bank_details ? <span className="num">{v.bank_details}</span> : v.has_bank_details ? <span className="slate-panel-muted">On file (Org Admin only)</span> : '—'}</td>
            </tr>
          ))}
          {vendors.length === 0 && <tr><td colSpan={3} className="slate-empty-note">No vendors yet.</td></tr>}
        </tbody>
      </table>
      {canWrite && (
        <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setShowVendor(true)} style={{ marginTop: 14 }}>
          <Plus size={13} /> New vendor
        </button>
      )}

      {showPO && <NewPOModal vendors={vendors} onClose={() => setShowPO(false)} onCreate={async (p) => { await createPO(p); load(); }} />}
      {showVendor && <NewVendorModal onClose={() => setShowVendor(false)} onCreate={async (p) => { await createVendor(p); load(); }} />}
    </div>
  );
}
