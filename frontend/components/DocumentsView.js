'use client';

import { useEffect, useState } from 'react';
import { Download, Plus, Upload } from 'lucide-react';
import { bumpDocumentVersion, createDocument, downloadDocument, fetchDocuments } from '../lib/api';

const ROLE_RANK = { read_only: 0, standard_user: 1, manager: 2, org_admin: 3 };
const atLeast = (role, min) => (ROLE_RANK[role] ?? 0) >= ROLE_RANK[min];

function bytesToSize(bytes) {
  if (!bytes) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function NewDocModal({ onClose, onCreate }) {
  const [form, setForm] = useState({ title: '', category: 'General', company: '' });
  const [file, setFile] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  async function submit(e) {
    e.preventDefault();
    setError('');
    setSaving(true);
    try {
      await onCreate({ ...form, file });
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
        <div className="slate-wordmark" style={{ marginBottom: 0, fontSize: 15 }}>New document</div>
        <input placeholder="Title" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} required />
        <select value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })}>
          <option>General</option><option>Contracts</option><option>Policies</option><option>Reports</option>
        </select>
        <input placeholder="Related company (optional)" value={form.company} onChange={(e) => setForm({ ...form, company: e.target.value })} />
        <label style={{ border: '1px dashed var(--line)', borderRadius: 8, padding: '14px 12px', fontSize: 12.5, color: 'var(--ink-soft)', textAlign: 'center', cursor: 'pointer' }}>
          <Upload size={14} style={{ verticalAlign: -2, marginRight: 6 }} />
          {file ? file.name : 'Choose a real file to upload (optional)'}
          <input type="file" onChange={(e) => setFile(e.target.files[0])} style={{ display: 'none' }} />
        </label>
        {error && <div className="slate-login-error">{error}</div>}
        <div className="slate-modal-row">
          <button type="button" className="slate-btn slate-btn-ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="slate-btn slate-btn-primary" disabled={saving}>{saving ? 'Uploading…' : 'Add document'}</button>
        </div>
      </form>
    </div>
  );
}

export default function DocumentsView({ userRole }) {
  const [docs, setDocs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const canWrite = atLeast(userRole, 'standard_user');

  function load() {
    setLoading(true);
    fetchDocuments().then(setDocs).finally(() => setLoading(false));
  }
  useEffect(load, []);

  async function newVersion(doc) {
    const input = document.createElement('input');
    input.type = 'file';
    input.onchange = async () => {
      if (input.files[0]) {
        await bumpDocumentVersion(doc.id, input.files[0]);
        load();
      }
    };
    input.click();
  }

  if (loading) return <div className="slate-empty-note">Loading documents…</div>;

  return (
    <div>
      <table className="slate-invoice-table">
        <thead><tr><th>Title</th><th>Category</th><th>Related to</th><th>File</th><th>Version</th><th>Uploaded by</th><th></th></tr></thead>
        <tbody>
          {docs.map((d) => (
            <tr key={d.id}>
              <td>{d.title}</td>
              <td><span className="slate-status-badge sent">{d.category}</span></td>
              <td>{d.company || '—'}</td>
              <td>{d.has_file ? <span className="num" style={{ fontSize: 11.5 }}>{d.filename} ({bytesToSize(d.size_bytes)})</span> : <span className="slate-panel-muted">No file</span>}</td>
              <td className="num">v{d.version}</td>
              <td>{d.uploaded_by}</td>
              <td>
                <div className="slate-invoice-actions">
                  {d.has_file && (
                    <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => downloadDocument(d.id, d.filename)} aria-label="Download">
                      <Download size={12} />
                    </button>
                  )}
                  {canWrite && <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => newVersion(d)}>New version</button>}
                </div>
              </td>
            </tr>
          ))}
          {docs.length === 0 && <tr><td colSpan={7} className="slate-empty-note">No documents yet.</td></tr>}
        </tbody>
      </table>
      {canWrite && (
        <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setShowCreate(true)} style={{ marginTop: 14 }}>
          <Plus size={13} /> New document
        </button>
      )}
      {showCreate && <NewDocModal onClose={() => setShowCreate(false)} onCreate={createDocument} />}
    </div>
  );
}
