'use client';

import { useEffect, useState } from 'react';
import { ClipboardCheck } from 'lucide-react';
import {
  attestControl, dsarErase, dsarExport, fetchComplianceControls, fetchComplianceMembers, fetchEvidencePack, restoreMember, suspendMember,
} from '../lib/api';

const CHIP = { pass: 'paid', attested: 'paid', warn: 'sent', manual: 'draft', fail: 'overdue' };

function saveJson(filename, data) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

export default function CompliancePanel({ userRole }) {
  const [data, setData] = useState(null);
  const [members, setMembers] = useState([]);
  const [email, setEmail] = useState('');
  const [sure, setSure] = useState(false);
  const [result, setResult] = useState(null);
  const [notes, setNotes] = useState({});
  const [reasons, setReasons] = useState({});
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');

  const load = () => {
    fetchComplianceControls().then(setData).catch(() => setData(null));
    fetchComplianceMembers().then(setMembers).catch(() => setMembers([]));
  };
  useEffect(() => { if (userRole === 'org_admin') load(); }, [userRole]);

  async function act(fn) {
    setError('');
    setMessage('');
    try {
      await fn();
    } catch (err) {
      setError(err.message);
    }
  }

  if (userRole !== 'org_admin' || !data) return null;

  return (
    <div className="slate-chart-card" style={{ maxWidth: 640, marginTop: 16 }}>
      <div className="slate-card-title" style={{ display: 'flex', alignItems: 'center', gap: 6 }}><ClipboardCheck size={13} /> Compliance Center</div>
      <p style={{ fontSize: 12.5, color: 'var(--ink-soft)', marginBottom: 10 }}>
        Controls are measured from live data and mapped to SOC 2, ISO 27001 and GDPR references. This is tooling and evidence — certification is an audit of your
        processes by an external party. {data.summary.score_percent !== null && <strong>{data.summary.score_percent}% of measured controls pass.</strong>}
      </p>

      {data.controls.map((c) => (
        <div className="slate-flag-row" key={c.id}>
          <div className="slate-flag-info">
            <div className="slate-flag-label">{c.title} <span className={`slate-status-badge ${CHIP[c.status] || 'draft'}`}>{c.status}</span></div>
            <div className="slate-flag-desc">{c.frameworks.join(' · ')}</div>
            {c.status === 'manual' && (
              <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                <input aria-label={`Attestation note for ${c.id}`} placeholder="What was checked, and when" value={notes[c.id] || ''} onChange={(e) => setNotes({ ...notes, [c.id]: e.target.value })} style={{ flex: 1 }} />
                <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => act(async () => { await attestControl(c.id, notes[c.id] || ''); load(); })}>Attest</button>
              </div>
            )}
          </div>
        </div>
      ))}
      <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" style={{ marginTop: 8 }} onClick={() => act(async () => saveJson('slate-evidence-pack.json', await fetchEvidencePack()))}>Download evidence pack</button>

      <div className="slate-card-title" style={{ marginTop: 18 }}>Data-subject requests (GDPR)</div>
      <input id="dsar-email" placeholder="Email address of the person" value={email} onChange={(e) => { setEmail(e.target.value); setResult(null); }} style={{ width: '100%' }} />
      <div style={{ display: 'flex', gap: 8, margin: '8px 0', alignItems: 'center', flexWrap: 'wrap' }}>
        <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" disabled={!email}
          onClick={() => act(async () => { const out = await dsarExport(email); saveJson('data-export.json', out); setMessage(`Exported ${Object.keys(out.tables).length} tables and ${out.other_mentions.length} free-text mentions.`); })}>Export their data</button>
        <label style={{ fontSize: 12.5 }}><input type="checkbox" checked={sure} onChange={(e) => setSure(e.target.checked)} /> I understand erasure is irreversible</label>
        <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" disabled={!email || !sure}
          onClick={() => act(async () => { setResult(await dsarErase(email)); setSure(false); load(); })}>Erase</button>
      </div>
      {result && (
        <div style={{ fontSize: 12.5 }}>
          <strong>{result.clean ? 'Erased — a rescan found no remaining mentions of the email.' : 'Erased, but the rescan still found mentions of the email.'}</strong>
          <div>Rewrote free text in: {result.scrubbed_free_text.map((x) => `${x.table}.${x.column} (${x.rows})`).join(', ') || 'nowhere'}.</div>
          {result.needs_review.length > 0 && <div>{result.needs_review.length} place(s) mention their name in prose and need a human look.</div>}
        </div>
      )}

      <div className="slate-card-title" style={{ marginTop: 18 }}>Access — suspend or restore</div>
      {members.map((m) => (
        <div className="slate-flag-row" key={m.id}>
          <div className="slate-flag-info">
            <div className="slate-flag-label">{m.name} <span className="slate-plugin-version">{m.role}</span>{m.suspended ? ' · suspended' : ''}</div>
            <div className="slate-flag-desc">{m.email}{m.suspended_reason ? ` — ${m.suspended_reason}` : ''}</div>
          </div>
          {m.suspended ? (
            <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => act(async () => { await restoreMember(m.id); load(); })}>Restore</button>
          ) : (
            <>
              <input aria-label={`Reason for suspending ${m.email}`} placeholder="Reason" value={reasons[m.id] || ''} onChange={(e) => setReasons({ ...reasons, [m.id]: e.target.value })} style={{ width: 140 }} />
              <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => act(async () => { await suspendMember(m.id, reasons[m.id] || ''); load(); })}>Suspend</button>
            </>
          )}
        </div>
      ))}
      {error && <div className="slate-login-error" style={{ marginTop: 8 }}>{error}</div>}
      {message && !error && <div style={{ fontSize: 12.5, color: 'var(--ink-soft)', marginTop: 8 }}>{message}</div>}
    </div>
  );
}
