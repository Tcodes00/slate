'use client';

import { useEffect, useState } from 'react';
import { ChevronDown, ChevronUp, Pencil, Plus, Trash2, Users } from 'lucide-react';
import {
  createCampaign, createSegment, deleteSegment, fetchAudienceSize, fetchCampaigns, fetchParties,
  fetchSegmentMembers, fetchSegments, updateCampaign, updatePartyProfile,
} from '../lib/api';

const ROLE_RANK = { read_only: 0, standard_user: 1, manager: 2, org_admin: 3 };
const atLeast = (role, min) => (ROLE_RANK[role] ?? 0) >= ROLE_RANK[min];

const INDUSTRIES = ['software', 'retail', 'healthcare', 'finance', 'manufacturing', 'education', 'other'];
const SIZES = ['1-10', '11-50', '51-200', '201-1000', '1000+'];

function NewCampaignModal({ onClose, onCreate }) {
  const [form, setForm] = useState({ name: '', channel: 'Email', budget: '' });
  const [saving, setSaving] = useState(false);
  async function submit(e) {
    e.preventDefault();
    setSaving(true);
    try { await onCreate({ ...form, budget: Number(form.budget) || 0 }); onClose(); } finally { setSaving(false); }
  }
  return (
    <div className="slate-modal-overlay" onClick={onClose}>
      <form className="slate-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="slate-wordmark" style={{ marginBottom: 0, fontSize: 15 }}>New campaign</div>
        <input placeholder="Campaign name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
        <select value={form.channel} onChange={(e) => setForm({ ...form, channel: e.target.value })}>
          <option>Email</option><option>Social</option><option>SMS</option>
        </select>
        <input placeholder="Budget ($)" type="number" min="0" value={form.budget} onChange={(e) => setForm({ ...form, budget: e.target.value })} />
        <div className="slate-modal-row">
          <button type="button" className="slate-btn slate-btn-ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="slate-btn slate-btn-primary" disabled={saving}>{saving ? 'Creating…' : 'Create campaign'}</button>
        </div>
      </form>
    </div>
  );
}

function CampaignsTable({ campaigns, canWrite, onAdvance }) {
  return (
    <table className="slate-invoice-table">
      <thead><tr><th>Campaign</th><th>Channel</th><th>Budget</th><th>Status</th><th></th></tr></thead>
      <tbody>
        {campaigns.map((c) => (
          <tr key={c.id}>
            <td>{c.name}</td>
            <td>{c.channel}</td>
            <td className="num">${Number(c.budget).toLocaleString('en-US')}</td>
            <td><span className={`slate-status-badge ${c.status === 'Active' ? 'sent' : c.status === 'Completed' ? 'paid' : 'draft'}`}>{c.status}</span></td>
            <td>{canWrite && c.status !== 'Completed' && (
              <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => onAdvance(c)}>
                {c.status === 'Draft' ? 'Activate' : 'Complete'}
              </button>
            )}</td>
          </tr>
        ))}
        {campaigns.length === 0 && <tr><td colSpan={5} className="slate-empty-note">No campaigns yet.</td></tr>}
      </tbody>
    </table>
  );
}

function NewSegmentForm({ onCreated }) {
  const [form, setForm] = useState({ name: '', filter_industry: '', filter_company_size: '', filter_tag: '', require_email: true });
  const [preview, setPreview] = useState(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const t = setTimeout(() => {
      fetchAudienceSize({
        industry: form.filter_industry, company_size: form.filter_company_size,
        tag: form.filter_tag, require_email: form.require_email,
      }).then((r) => setPreview(r.addressable_parties));
    }, 250);
    return () => clearTimeout(t);
  }, [form]);

  async function submit(e) {
    e.preventDefault();
    if (!form.name.trim()) return;
    setSaving(true);
    try {
      await createSegment(form);
      setForm({ name: '', filter_industry: '', filter_company_size: '', filter_tag: '', require_email: true });
      onCreated();
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="slate-segment-form" onSubmit={submit}>
      <input placeholder="Segment name — e.g. Enterprise SaaS renewals" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
      <div className="slate-segment-form-row">
        <select value={form.filter_industry} onChange={(e) => setForm({ ...form, filter_industry: e.target.value })}>
          <option value="">Any industry</option>
          {INDUSTRIES.map((i) => <option key={i} value={i}>{i}</option>)}
        </select>
        <select value={form.filter_company_size} onChange={(e) => setForm({ ...form, filter_company_size: e.target.value })}>
          <option value="">Any size</option>
          {SIZES.map((s) => <option key={s} value={s}>{s} employees</option>)}
        </select>
        <input placeholder="Tag (optional)" value={form.filter_tag} onChange={(e) => setForm({ ...form, filter_tag: e.target.value })} />
      </div>
      <div className="slate-segment-form-footer">
        <label className="slate-segment-checkbox">
          <input type="checkbox" checked={form.require_email} onChange={(e) => setForm({ ...form, require_email: e.target.checked })} />
          Must have an email on file
        </label>
        <div className="slate-segment-preview">{preview === null ? '…' : preview} match{preview === 1 ? '' : 'es'} right now</div>
        <button className="slate-btn slate-btn-primary slate-btn-sm" type="submit" disabled={saving || !form.name.trim()}>
          {saving ? 'Saving…' : 'Save segment'}
        </button>
      </div>
    </form>
  );
}

function SegmentRow({ segment, canWrite, onDeleted }) {
  const [expanded, setExpanded] = useState(false);
  const [members, setMembers] = useState(null);

  async function toggle() {
    if (!expanded && members === null) setMembers(await fetchSegmentMembers(segment.id));
    setExpanded((v) => !v);
  }

  const chips = [segment.filter_industry, segment.filter_company_size, segment.filter_tag && `#${segment.filter_tag}`].filter(Boolean);

  return (
    <div className="slate-segment-card">
      <button type="button" className="slate-segment-card-top" onClick={toggle}>
        <div>
          <div className="slate-segment-name">{segment.name}</div>
          <div className="slate-segment-chips">
            {chips.length === 0 ? <span className="slate-flag-desc">No filters — everyone addressable</span> : chips.map((c) => <span key={c} className="slate-segment-chip">{c}</span>)}
          </div>
        </div>
        <div className="slate-segment-card-right">
          <span className="slate-segment-count">{segment.member_count}</span>
          {expanded ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
        </div>
      </button>
      {expanded && (
        <div className="slate-segment-members">
          {members === null ? (
            <div className="slate-empty-note">Loading…</div>
          ) : members.length === 0 ? (
            <div className="slate-empty-note">No matches.</div>
          ) : (
            members.map((m) => <div key={m.id} className="slate-segment-member">{m.display_name} <span className="slate-flag-desc">{m.primary_email}</span></div>)
          )}
          {canWrite && (
            <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" style={{ marginTop: 10 }} onClick={async () => { await deleteSegment(segment.id); onDeleted(); }}>
              <Trash2 size={13} /> Delete segment
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function EditProfileRow({ party, canWrite, onSaved }) {
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState({ industry: party.industry || '', company_size: party.company_size || '', tags: party.tags.join(', ') });
  const [saving, setSaving] = useState(false);

  async function save(e) {
    e.preventDefault();
    setSaving(true);
    try {
      await updatePartyProfile(party.id, {
        industry: form.industry, company_size: form.company_size,
        tags: form.tags.split(',').map((t) => t.trim()).filter(Boolean),
      });
      setEditing(false);
      onSaved();
    } finally {
      setSaving(false);
    }
  }

  if (editing) {
    return (
      <tr>
        <td colSpan={5}>
          <form className="slate-profile-edit-row" onSubmit={save}>
            <span style={{ fontWeight: 600 }}>{party.display_name}</span>
            <select value={form.industry} onChange={(e) => setForm({ ...form, industry: e.target.value })}>
              <option value="">Industry…</option>
              {INDUSTRIES.map((i) => <option key={i} value={i}>{i}</option>)}
            </select>
            <select value={form.company_size} onChange={(e) => setForm({ ...form, company_size: e.target.value })}>
              <option value="">Size…</option>
              {SIZES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
            <input placeholder="tags, comma, separated" value={form.tags} onChange={(e) => setForm({ ...form, tags: e.target.value })} />
            <button className="slate-btn slate-btn-primary slate-btn-sm" type="submit" disabled={saving}>{saving ? '…' : 'Save'}</button>
            <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setEditing(false)}>Cancel</button>
          </form>
        </td>
      </tr>
    );
  }

  return (
    <tr>
      <td>{party.display_name}</td>
      <td className="slate-flag-desc">{party.primary_email || '—'}</td>
      <td>{party.industry || '—'}</td>
      <td>{party.company_size || '—'}</td>
      <td>
        <div className="slate-tag-list">{party.tags.map((t) => <span key={t} className="slate-segment-chip">{t}</span>)}</div>
      </td>
      <td>{canWrite && <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setEditing(true)}><Pencil size={12} /></button>}</td>
    </tr>
  );
}

export default function MarketingView({ userRole }) {
  const [campaigns, setCampaigns] = useState([]);
  const [audience, setAudience] = useState(null);
  const [segments, setSegments] = useState([]);
  const [parties, setParties] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const canWrite = atLeast(userRole, 'standard_user');

  function load() {
    setLoading(true);
    Promise.all([fetchCampaigns(), fetchAudienceSize(), fetchSegments(), fetchParties()])
      .then(([c, a, s, p]) => { setCampaigns(c); setAudience(a); setSegments(s); setParties(p); })
      .finally(() => setLoading(false));
  }
  useEffect(load, []);

  function reloadSegments() {
    fetchSegments().then(setSegments);
  }
  function reloadParties() {
    fetchParties().then(setParties);
  }

  async function advance(c) {
    const next = c.status === 'Draft' ? 'Active' : 'Completed';
    await updateCampaign(c.id, next);
    load();
  }

  if (loading) return <div className="slate-empty-note">Loading campaigns…</div>;

  return (
    <div>
      {audience && (
        <div className="slate-kpi-card" style={{ marginBottom: 18, display: 'inline-flex', alignItems: 'center', gap: 10, padding: '10px 16px' }}>
          <Users size={16} />
          <div>
            <div className="slate-kpi-value num" style={{ fontSize: 16 }}>{audience.addressable_parties}</div>
            <div className="slate-kpi-label" style={{ marginBottom: 0 }}>addressable contacts</div>
          </div>
        </div>
      )}

      <CampaignsTable campaigns={campaigns} canWrite={canWrite} onAdvance={advance} />
      {canWrite && (
        <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setShowCreate(true)} style={{ marginTop: 14 }}>
          <Plus size={13} /> New campaign
        </button>
      )}
      {showCreate && <NewCampaignModal onClose={() => setShowCreate(false)} onCreate={async (p) => { await createCampaign(p); load(); }} />}

      <div className="slate-section-divider" />
      <div className="slate-card-title">Segments</div>
      <p className="slate-view-intro">
        Saved queries against industry, company size, and tags — membership is computed live every time
        a segment is opened, not stored as a snapshot that can drift from the real data.
      </p>
      {canWrite && <NewSegmentForm onCreated={reloadSegments} />}
      <div className="slate-segment-list">
        {segments.map((s) => <SegmentRow key={s.id} segment={s} canWrite={canWrite} onDeleted={reloadSegments} />)}
        {segments.length === 0 && <div className="slate-empty-note">No saved segments yet.</div>}
      </div>

      <div className="slate-section-divider" />
      <div className="slate-card-title">Contacts</div>
      <p className="slate-view-intro">
        Every party in the system (from CRM, Finance, Support — Party is the one shared record every
        module reads and writes) with the industry/size/tags this module adds on top, for segmenting on.
      </p>
      <table className="slate-invoice-table">
        <thead><tr><th>Name</th><th>Email</th><th>Industry</th><th>Size</th><th>Tags</th><th></th></tr></thead>
        <tbody>
          {parties.map((p) => <EditProfileRow key={p.id} party={p} canWrite={canWrite} onSaved={reloadParties} />)}
          {parties.length === 0 && <tr><td colSpan={6} className="slate-empty-note">No contacts yet.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}
