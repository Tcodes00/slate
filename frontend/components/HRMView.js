'use client';

import { useEffect, useState } from 'react';
import { Plus, ShieldCheck } from 'lucide-react';
import { createEmployee, createLeaveRequest, fetchEmployees, fetchLeaveRequests, linkEmployeeToMe, updateLeaveRequest } from '../lib/api';

const ROLE_RANK = { read_only: 0, standard_user: 1, manager: 2, org_admin: 3 };
const atLeast = (role, min) => (ROLE_RANK[role] ?? 0) >= ROLE_RANK[min];

function CreateEmployeeModal({ employees, onClose, onCreate }) {
  const [form, setForm] = useState({ name: '', email: '', employee_number: '', department: '', job_title: '', manager_id: '' });
  const [saving, setSaving] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setSaving(true);
    try {
      await onCreate({ ...form, manager_id: form.manager_id ? Number(form.manager_id) : null });
      onClose();
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="slate-modal-overlay" onClick={onClose}>
      <form className="slate-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="slate-wordmark" style={{ marginBottom: 0, fontSize: 15 }}>New employee</div>
        <input placeholder="Full name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
        <input placeholder="Email" type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
        <input placeholder="Employee number" value={form.employee_number} onChange={(e) => setForm({ ...form, employee_number: e.target.value })} required />
        <input placeholder="Department" value={form.department} onChange={(e) => setForm({ ...form, department: e.target.value })} />
        <input placeholder="Job title" value={form.job_title} onChange={(e) => setForm({ ...form, job_title: e.target.value })} />
        <select value={form.manager_id} onChange={(e) => setForm({ ...form, manager_id: e.target.value })}>
          <option value="">No manager</option>
          {employees.map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
        </select>
        <div className="slate-modal-row">
          <button type="button" className="slate-btn slate-btn-ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="slate-btn slate-btn-primary" disabled={saving}>{saving ? 'Adding…' : 'Add employee'}</button>
        </div>
      </form>
    </div>
  );
}

function RequestLeaveModal({ employees, onClose, onCreate }) {
  const [form, setForm] = useState({ employee_id: employees[0]?.id || '', leave_type: 'PTO', start_date: '', end_date: '', reason: '' });
  const [saving, setSaving] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setSaving(true);
    try {
      await onCreate({ ...form, employee_id: Number(form.employee_id), start_date: `${form.start_date}T00:00:00`, end_date: `${form.end_date}T00:00:00` });
      onClose();
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="slate-modal-overlay" onClick={onClose}>
      <form className="slate-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="slate-wordmark" style={{ marginBottom: 0, fontSize: 15 }}>Request leave</div>
        <select value={form.employee_id} onChange={(e) => setForm({ ...form, employee_id: e.target.value })} required>
          {employees.map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
        </select>
        <select value={form.leave_type} onChange={(e) => setForm({ ...form, leave_type: e.target.value })}>
          <option value="PTO">PTO</option>
          <option value="Sick">Sick</option>
          <option value="Other">Other</option>
        </select>
        <input type="date" value={form.start_date} onChange={(e) => setForm({ ...form, start_date: e.target.value })} required />
        <input type="date" value={form.end_date} onChange={(e) => setForm({ ...form, end_date: e.target.value })} required />
        <input placeholder="Reason (optional)" value={form.reason} onChange={(e) => setForm({ ...form, reason: e.target.value })} />
        <div className="slate-modal-row">
          <button type="button" className="slate-btn slate-btn-ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="slate-btn slate-btn-primary" disabled={saving}>{saving ? 'Submitting…' : 'Submit request'}</button>
        </div>
      </form>
    </div>
  );
}

export default function HRMView({ userRole, currentUser, onUserUpdate }) {
  const [employees, setEmployees] = useState([]);
  const [leaveRequests, setLeaveRequests] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showCreateEmployee, setShowCreateEmployee] = useState(false);
  const [showRequestLeave, setShowRequestLeave] = useState(false);
  const [linkChoice, setLinkChoice] = useState('');
  const canManageHR = atLeast(userRole, 'manager');
  const canRequest = atLeast(userRole, 'standard_user');

  function load() {
    setLoading(true);
    Promise.all([fetchEmployees(), fetchLeaveRequests()]).then(([e, l]) => { setEmployees(e); setLeaveRequests(l); }).finally(() => setLoading(false));
  }
  useEffect(load, []);

  async function decide(request, status) {
    await updateLeaveRequest(request.id, status);
    load();
  }

  async function linkSelf() {
    if (!linkChoice) return;
    await linkEmployeeToMe(Number(linkChoice));
    onUserUpdate?.({ ...currentUser, employee_id: Number(linkChoice) });
    load();
  }

  if (loading) return <div className="slate-empty-note">Loading team records…</div>;

  return (
    <div>
      {currentUser && !currentUser.employee_id && (
        <div className="slate-chart-card" style={{ marginBottom: 18, display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <ShieldCheck size={14} />
          <span style={{ fontSize: 12.5 }}>Link your login to your HRM record — this is what department-scoped visibility (ABAC) uses.</span>
          <select value={linkChoice} onChange={(e) => setLinkChoice(e.target.value)} style={{ fontSize: 12 }}>
            <option value="">Choose your record…</option>
            {employees.map((e) => <option key={e.id} value={e.id}>{e.name} — {e.department}</option>)}
          </select>
          <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={linkSelf} disabled={!linkChoice}>Link</button>
        </div>
      )}

      <div className="slate-card-title" style={{ marginBottom: 8 }}>Employees</div>
      <table className="slate-invoice-table" style={{ marginBottom: 12 }}>
        <thead><tr><th>Name</th><th>Title</th><th>Department</th><th>Reports to</th><th>Status</th></tr></thead>
        <tbody>
          {employees.map((e) => (
            <tr key={e.id}>
              <td>{e.name}</td>
              <td>{e.job_title || '—'}</td>
              <td>{e.department || '—'}</td>
              <td>{e.manager_name || '—'}</td>
              <td><span className="slate-status-badge sent">{e.status}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
      {canManageHR && (
        <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setShowCreateEmployee(true)} style={{ marginBottom: 24 }}>
          <Plus size={13} /> Add employee
        </button>
      )}

      <div className="slate-card-title" style={{ marginBottom: 8 }}>Leave requests</div>
      <table className="slate-invoice-table">
        <thead><tr><th>Employee</th><th>Type</th><th>Dates</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {leaveRequests.map((r) => (
            <tr key={r.id}>
              <td>{r.employee_name}</td>
              <td>{r.leave_type}</td>
              <td>{new Date(r.start_date).toLocaleDateString()} – {new Date(r.end_date).toLocaleDateString()}</td>
              <td><span className={`slate-status-badge ${r.status === 'Approved' ? 'paid' : r.status === 'Denied' ? 'overdue' : 'draft'}`}>{r.status}</span></td>
              <td>
                {canManageHR && r.status === 'Pending' && (
                  <div className="slate-invoice-actions">
                    <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => decide(r, 'Approved')}>Approve</button>
                    <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => decide(r, 'Denied')}>Deny</button>
                  </div>
                )}
              </td>
            </tr>
          ))}
          {leaveRequests.length === 0 && <tr><td colSpan={5} className="slate-empty-note">No leave requests yet.</td></tr>}
        </tbody>
      </table>
      {canRequest && employees.length > 0 && (
        <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setShowRequestLeave(true)} style={{ marginTop: 14 }}>
          <Plus size={13} /> Request leave
        </button>
      )}

      {showCreateEmployee && (
        <CreateEmployeeModal employees={employees} onClose={() => setShowCreateEmployee(false)} onCreate={async (p) => { await createEmployee(p); load(); }} />
      )}
      {showRequestLeave && (
        <RequestLeaveModal employees={employees} onClose={() => setShowRequestLeave(false)} onCreate={async (p) => { await createLeaveRequest(p); load(); }} />
      )}
    </div>
  );
}
