'use client';

import { useEffect, useState } from 'react';
import { Plus, X } from 'lucide-react';
import { createProject, createTask, fetchProjects, fetchTasks, updateTask } from '../lib/api';

const ROLE_RANK = { read_only: 0, standard_user: 1, manager: 2, org_admin: 3 };
const atLeast = (role, min) => (ROLE_RANK[role] ?? 0) >= ROLE_RANK[min];
const STATUSES = ['To Do', 'In Progress', 'Done'];

function money(n) {
  return '$' + Number(n).toLocaleString('en-US');
}

function TaskPanel({ project, userRole, onClose }) {
  const [tasks, setTasks] = useState([]);
  const [title, setTitle] = useState('');
  const canWrite = atLeast(userRole, 'standard_user');

  function load() {
    fetchTasks(project.id).then(setTasks);
  }
  useEffect(load, [project]);

  async function add() {
    if (!title.trim()) return;
    await createTask(project.id, { title: title.trim() });
    setTitle('');
    load();
  }

  async function cycleStatus(task) {
    const next = STATUSES[(STATUSES.indexOf(task.status) + 1) % STATUSES.length];
    await updateTask(task.id, next);
    load();
  }

  return (
    <>
      <div className="slate-panel-overlay" onClick={onClose} />
      <div className="slate-panel">
        <button className="slate-panel-close" onClick={onClose} aria-label="Close"><X size={16} /></button>
        <div className="slate-panel-eyebrow">{project.status}</div>
        <h3 className="slate-panel-title">{project.name}</h3>
        <div className="slate-panel-value num">{money(project.budget)}</div>

        <div className="slate-panel-section">
          <div className="slate-panel-label">Tasks — click to advance status</div>
          {tasks.map((t) => (
            <button
              key={t.id}
              onClick={() => canWrite && cycleStatus(t)}
              className="slate-deal-card"
              style={{ marginBottom: 6, cursor: canWrite ? 'pointer' : 'default' }}
            >
              <div className="slate-deal-name">{t.title}</div>
              <span className={`slate-status-badge ${t.status === 'Done' ? 'paid' : t.status === 'In Progress' ? 'sent' : 'draft'}`}>{t.status}</span>
            </button>
          ))}
          {tasks.length === 0 && <div className="slate-panel-muted">No tasks yet.</div>}

          {canWrite && (
            <div className="slate-activity-add-row">
              <input placeholder="New task…" value={title} onChange={(e) => setTitle(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && add()} />
              <button className="slate-btn slate-btn-primary slate-btn-sm" onClick={add}>Add</button>
            </div>
          )}
        </div>
      </div>
    </>
  );
}

function NewProjectModal({ onClose, onCreate }) {
  const [form, setForm] = useState({ name: '', budget: '' });
  const [saving, setSaving] = useState(false);
  async function submit(e) {
    e.preventDefault();
    setSaving(true);
    try { await onCreate({ name: form.name, budget: Number(form.budget) || 0 }); onClose(); } finally { setSaving(false); }
  }
  return (
    <div className="slate-modal-overlay" onClick={onClose}>
      <form className="slate-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="slate-wordmark" style={{ marginBottom: 0, fontSize: 15 }}>New project</div>
        <input placeholder="Project name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
        <input placeholder="Budget ($)" type="number" min="0" value={form.budget} onChange={(e) => setForm({ ...form, budget: e.target.value })} />
        <div className="slate-modal-row">
          <button type="button" className="slate-btn slate-btn-ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="slate-btn slate-btn-primary" disabled={saving}>{saving ? 'Creating…' : 'Create project'}</button>
        </div>
      </form>
    </div>
  );
}

export default function ProjectsView({ userRole }) {
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(null);
  const [showCreate, setShowCreate] = useState(false);
  const canWrite = atLeast(userRole, 'standard_user');

  function load() {
    setLoading(true);
    fetchProjects().then(setProjects).finally(() => setLoading(false));
  }
  useEffect(load, []);

  if (loading) return <div className="slate-empty-note">Loading projects…</div>;

  return (
    <div>
      <table className="slate-invoice-table">
        <thead><tr><th>Project</th><th>Status</th><th>Budget</th><th>Tasks</th></tr></thead>
        <tbody>
          {projects.map((p) => (
            <tr key={p.id} onClick={() => setSelected(p)} style={{ cursor: 'pointer' }}>
              <td>{p.name}</td>
              <td><span className="slate-status-badge sent">{p.status}</span></td>
              <td className="num">{money(p.budget)}</td>
              <td className="num">{p.done_count}/{p.task_count} done</td>
            </tr>
          ))}
          {projects.length === 0 && <tr><td colSpan={4} className="slate-empty-note">No projects yet.</td></tr>}
        </tbody>
      </table>
      {canWrite && (
        <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setShowCreate(true)} style={{ marginTop: 14 }}>
          <Plus size={13} /> New project
        </button>
      )}
      {selected && <TaskPanel project={selected} userRole={userRole} onClose={() => setSelected(null)} />}
      {showCreate && <NewProjectModal onClose={() => setShowCreate(false)} onCreate={async (p) => { await createProject(p); load(); }} />}
    </div>
  );
}
