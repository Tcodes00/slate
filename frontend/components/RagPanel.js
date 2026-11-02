'use client';

import { useEffect, useState } from 'react';
import { FileText, RefreshCw, Sparkles, Trash2, Upload } from 'lucide-react';
import { deleteRagDocument, fetchRagDocuments, fetchRagStatus, ragAsk, ragIndexKb, ragReindex, ragSearch, uploadRagDocument } from '../lib/api';
import FlagGate from './FlagGate';

const ROLE_RANK = { read_only: 0, standard_user: 1, manager: 2, org_admin: 3 };
const atLeast = (role, min) => (ROLE_RANK[role] ?? 0) >= ROLE_RANK[min];

export default function RagPanel({ userRole }) {
  const [status, setStatus] = useState(null);
  const [docs, setDocs] = useState([]);
  const [question, setQuestion] = useState('');
  const [answer, setAnswer] = useState(null);
  const [passages, setPassages] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const canManage = atLeast(userRole, 'manager');

  const load = () => {
    fetchRagStatus().then(setStatus).catch(() => setStatus(null));
    fetchRagDocuments().then(setDocs).catch(() => setDocs([]));
  };
  useEffect(() => { load(); }, []);

  async function run(fn) {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await fn();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const ask = (e) => {
    e.preventDefault();
    return run(async () => {
      setPassages(null);
      setAnswer(await ragAsk(question));
    });
  };
  const search = () => run(async () => {
    setAnswer(null);
    setPassages((await ragSearch(question)).results);
  });
  const upload = (file) => run(async () => {
    const result = await uploadRagDocument(file);
    setNotice(`${file.name}: ${result.status} (${result.chunks} passages)`);
    load();
  });
  const indexKb = () => run(async () => {
    const result = await ragIndexKb();
    setNotice(`Knowledge base indexed: ${result.indexed} new, ${result.replaced} updated, ${result.unchanged} unchanged`);
    load();
  });
  const reindex = () => run(async () => {
    const result = await ragReindex();
    setNotice(`Re-embedded ${result.reindexed} document(s)`);
    load();
  });

  return (
    <div style={{ marginTop: 28 }}>
      <div className="slate-card-title" style={{ display: 'flex', alignItems: 'center', gap: 6 }}><Sparkles size={14} /> Ask your documents</div>
      <FlagGate flagKey="knowledge_rag" label="Knowledge search & answers" userRole={userRole}>
        {status && (
          <p style={{ fontSize: 12.5, color: 'var(--ink-faint)', margin: '6px 0 10px' }}>
            {status.documents} documents · {status.chunks} passages · {status.vector_backend} index · {status.embedder} ({status.semantic ? 'semantic' : 'lexical, typo-tolerant — connect an embedding model for meaning-based search'})
            {status.stale_documents > 0 && canManage && (
              <> · <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" onClick={reindex} disabled={busy}><RefreshCw size={12} /> Re-embed {status.stale_documents} stale</button></>
            )}
          </p>
        )}
        <form onSubmit={ask} style={{ display: 'flex', gap: 8 }}>
          <input id="rag-question" placeholder="Ask a question about your documents" value={question} onChange={(e) => setQuestion(e.target.value)} required style={{ flex: 1 }} />
          <button type="submit" className="slate-btn slate-btn-primary slate-btn-sm" disabled={busy}>Ask</button>
          <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" disabled={busy || !question.trim()} onClick={search}>Search passages</button>
        </form>
        {error && <div className="slate-login-error" style={{ marginTop: 8 }}>{error}</div>}
        {notice && !error && <div style={{ fontSize: 12.5, color: 'var(--ink-soft)', marginTop: 8 }}>{notice}</div>}

        {answer && (
          <div className="slate-chart-card" style={{ marginTop: 12 }}>
            {answer.answer ? <div id="rag-answer" style={{ whiteSpace: 'pre-wrap' }}>{answer.answer}</div> : <div className="slate-empty-note">{answer.reason}</div>}
            {answer.citations.length > 0 && <div className="slate-card-title" style={{ marginTop: 10 }}>Sources</div>}
            {answer.citations.map((c) => (
              <div key={c.n} style={{ fontSize: 12.5, margin: '6px 0' }}>
                <strong>[{c.n}] {c.title}{c.page ? `, page ${c.page}` : ''}</strong>{c.cited ? ' · cited' : ''}
                <div style={{ color: 'var(--ink-faint)' }}>{c.snippet}</div>
              </div>
            ))}
            {!answer.answer && answer.retrieved.map((r) => <div key={r.chunk_id} style={{ fontSize: 12.5, margin: '6px 0' }}><strong>{r.title}</strong>: {r.text.slice(0, 240)}</div>)}
          </div>
        )}
        {passages && (
          <div className="slate-chart-card" style={{ marginTop: 12 }}>
            {passages.length === 0 && <div className="slate-empty-note">No matching passages.</div>}
            {passages.map((r) => (
              <div key={r.chunk_id} style={{ fontSize: 12.5, margin: '6px 0' }}>
                <strong>{r.title}{r.page ? `, page ${r.page}` : ''}</strong> <span className="slate-plugin-version">score {r.score}</span>
                <div style={{ color: 'var(--ink-faint)' }}>{r.text.slice(0, 240)}</div>
              </div>
            ))}
          </div>
        )}

        {canManage && (
          <div style={{ marginTop: 18 }}>
            <div className="slate-card-title">Indexed documents</div>
            {docs.length === 0 && <div className="slate-empty-note">Nothing indexed yet.</div>}
            {docs.map((d) => (
              <div className="slate-flag-row" key={d.id}>
                <div className="slate-flag-info">
                  <div className="slate-flag-label"><FileText size={12} /> {d.title} <span className="slate-plugin-version">{d.source_type}</span>{d.stale ? ' · stale' : ''}</div>
                  <div className="slate-flag-desc">{d.chunk_count} passages · visible to {d.min_role} and above</div>
                </div>
                <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" aria-label={`Remove ${d.title}`} onClick={() => run(async () => { await deleteRagDocument(d.id); load(); })}><Trash2 size={13} /></button>
              </div>
            ))}
            <div style={{ display: 'flex', gap: 8, marginTop: 10, alignItems: 'center' }}>
              <label className="slate-btn slate-btn-ghost slate-btn-sm" style={{ cursor: 'pointer' }}>
                <Upload size={13} /> Upload a document
                <input id="rag-file" type="file" accept=".pdf,.docx,.txt,.md,.csv,.json,.html,.htm" style={{ display: 'none' }} onChange={(e) => e.target.files[0] && upload(e.target.files[0])} />
              </label>
              <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" onClick={indexKb} disabled={busy}>Index knowledge-base articles</button>
            </div>
            <p style={{ fontSize: 12, color: 'var(--ink-faint)', marginTop: 6 }}>PDF, Word, text, Markdown, CSV, JSON or HTML up to 20 MB. Scanned PDFs without a text layer can&apos;t be read (no OCR).</p>
          </div>
        )}
      </FlagGate>
    </div>
  );
}
