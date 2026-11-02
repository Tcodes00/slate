'use client';

import { useEffect, useState } from 'react';
import { Plus, Search } from 'lucide-react';
import { createArticle, fetchArticles, searchArticles } from '../lib/api';
import RagPanel from './RagPanel';

const ROLE_RANK = { read_only: 0, standard_user: 1, manager: 2, org_admin: 3 };
const atLeast = (role, min) => (ROLE_RANK[role] ?? 0) >= ROLE_RANK[min];

function NewArticleModal({ onClose, onCreate }) {
  const [form, setForm] = useState({ title: '', content: '', visibility: 'internal' });
  const [saving, setSaving] = useState(false);
  async function submit(e) {
    e.preventDefault();
    setSaving(true);
    try { await onCreate(form); onClose(); } finally { setSaving(false); }
  }
  return (
    <div className="slate-modal-overlay" onClick={onClose}>
      <form className="slate-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="slate-wordmark" style={{ marginBottom: 0, fontSize: 15 }}>New article</div>
        <input placeholder="Title" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} required />
        <input placeholder="Content" value={form.content} onChange={(e) => setForm({ ...form, content: e.target.value })} required />
        <select value={form.visibility} onChange={(e) => setForm({ ...form, visibility: e.target.value })}>
          <option value="internal">Internal only</option>
          <option value="public">Public help center</option>
        </select>
        <div className="slate-modal-row">
          <button type="button" className="slate-btn slate-btn-ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="slate-btn slate-btn-primary" disabled={saving}>{saving ? 'Publishing…' : 'Publish'}</button>
        </div>
      </form>
    </div>
  );
}

export default function KnowledgeBaseView({ userRole }) {
  const [articles, setArticles] = useState([]);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const canWrite = atLeast(userRole, 'standard_user');

  function load() {
    setLoading(true);
    fetchArticles().then(setArticles).finally(() => setLoading(false));
  }
  useEffect(load, []);

  async function runSearch(e) {
    e.preventDefault();
    if (!query.trim()) return load();
    setLoading(true);
    searchArticles(query.trim()).then(setArticles).finally(() => setLoading(false));
  }

  return (
    <div>
      <form onSubmit={runSearch} className="slate-chat-input-row" style={{ marginBottom: 16, borderTop: 'none', paddingTop: 0 }}>
        <input className="slate-chat-input" placeholder="Search articles…" value={query} onChange={(e) => setQuery(e.target.value)} />
        <button className="slate-btn slate-btn-primary" type="submit" aria-label="Search"><Search size={15} /></button>
      </form>

      {loading ? (
        <div className="slate-empty-note">Loading…</div>
      ) : (
        <>
          {articles.map((a) => (
            <div className="slate-chart-card" key={a.id} style={{ marginBottom: 10 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
                <div className="slate-card-title" style={{ marginBottom: 4 }}>{a.title}</div>
                <span className={`slate-status-badge ${a.visibility === 'public' ? 'paid' : 'draft'}`}>{a.visibility}</span>
              </div>
              <div style={{ fontSize: 13 }}>{a.content}</div>
              <div className="slate-panel-muted" style={{ marginTop: 6, fontSize: 11.5 }}>By {a.author} · {new Date(a.created_at).toLocaleDateString()}</div>
            </div>
          ))}
          {articles.length === 0 && <div className="slate-empty-note">No articles found.</div>}
        </>
      )}

      {canWrite && (
        <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setShowCreate(true)} style={{ marginTop: 8 }}>
          <Plus size={13} /> New article
        </button>
      )}
      {showCreate && <NewArticleModal onClose={() => setShowCreate(false)} onCreate={async (p) => { await createArticle(p); load(); }} />}
      <RagPanel userRole={userRole} />
    </div>
  );
}
