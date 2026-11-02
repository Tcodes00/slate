'use client';

import { useEffect, useState } from 'react';
import { Clock, Mail, Plus, Receipt, Sparkles, Trash2, X } from 'lucide-react';
import { addActivity, deleteDeal, fetchActivities, generateInvoiceFromDeal, updateDeal } from '../lib/api';

const ROLE_RANK = { read_only: 0, standard_user: 1, manager: 2, org_admin: 3 };
const atLeast = (role, min) => (ROLE_RANK[role] ?? 0) >= ROLE_RANK[min];

const STAGES = ['Lead', 'Qualified', 'Proposal', 'Won'];

function money(n) {
  return '$' + Number(n).toLocaleString('en-US');
}

function daysSince(iso) {
  return Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 86400000));
}

function DealCard({ deal, onClick }) {
  const risky = daysSince(deal.last_activity_at) >= 5 && deal.stage !== 'Won' && deal.stage !== 'Lost';
  return (
    <button className={`slate-deal-card ${risky ? 'risk' : ''} ${deal.stage === 'Won' ? 'won' : ''}`} onClick={onClick}>
      <div className="slate-deal-company">{deal.company}</div>
      <div className="slate-deal-name">{deal.name}</div>
      <div className="slate-deal-footer">
        <span className="num">{money(deal.value)}</span>
        {risky && (
          <span className="slate-deal-flag">
            <Clock size={11} /> {daysSince(deal.last_activity_at)}d
          </span>
        )}
      </div>
    </button>
  );
}

function DealPanel({ deal, userRole, onClose, onChanged, onDeleted, onAskCopilot }) {
  const [activities, setActivities] = useState([]);
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);
  const [invoiceMsg, setInvoiceMsg] = useState('');
  const [generating, setGenerating] = useState(false);
  const canWrite = atLeast(userRole, 'standard_user');
  const canDelete = atLeast(userRole, 'manager');

  useEffect(() => {
    if (deal) {
      fetchActivities(deal.id).then(setActivities).catch(() => setActivities([]));
      setInvoiceMsg('');
    }
  }, [deal]);

  if (!deal) return null;

  async function changeStage(stage) {
    setSaving(true);
    try {
      const updated = await updateDeal(deal.id, { stage });
      onChanged(updated);
    } finally {
      setSaving(false);
    }
  }

  async function submitNote() {
    if (!note.trim()) return;
    const activity = await addActivity(deal.id, note.trim());
    setActivities([activity, ...activities]);
    setNote('');
    onChanged({ ...deal, last_activity_at: activity.occurred_at });
  }

  async function handleGenerateInvoice() {
    setGenerating(true);
    try {
      const invoice = await generateInvoiceFromDeal(deal.id);
      setInvoiceMsg(`Invoice ${invoice.invoice_number} created in Finance — ${new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(invoice.amount)}.`);
    } catch (e) {
      setInvoiceMsg("Couldn't generate the invoice — try again.");
    } finally {
      setGenerating(false);
    }
  }

  async function handleDelete() {
    if (!confirm(`Delete "${deal.name}"? This can't be undone.`)) return;
    await deleteDeal(deal.id);
    onDeleted(deal.id);
  }

  return (
    <>
      <div className="slate-panel-overlay" onClick={onClose} />
      <div className="slate-panel">
        <button className="slate-panel-close" onClick={onClose} aria-label="Close">
          <X size={16} />
        </button>
        <div className="slate-panel-eyebrow">{deal.stage}</div>
        <h3 className="slate-panel-title">{deal.name}</h3>
        <div className="slate-panel-company">{deal.company}</div>
        <div className="slate-panel-value num">{money(deal.value)}</div>

        <div className="slate-panel-stages">
          {STAGES.map((s) => (
            <button
              key={s}
              className={`slate-stage-btn ${deal.stage === s ? 'active' : ''}`}
              onClick={() => changeStage(s)}
              disabled={saving || !canWrite}
            >
              {s}
            </button>
          ))}
        </div>

        {!canWrite && <div className="slate-empty-note">Read-only access — viewing only.</div>}

        {canWrite && deal.stage === 'Won' && (
          <>
            <button className="slate-btn slate-btn-ghost slate-btn-sm" style={{ width: '100%', marginTop: 10 }} onClick={handleGenerateInvoice} disabled={generating}>
              <Receipt size={13} /> {generating ? 'Generating…' : 'Generate invoice'}
            </button>
            {invoiceMsg && <div className="slate-generate-invoice-note">{invoiceMsg}</div>}
          </>
        )}

        {deal.contact_email && (
          <div className="slate-panel-section">
            <div className="slate-panel-label">Contact</div>
            <div className="slate-panel-contact-email">
              <Mail size={12} /> {deal.contact_email}
            </div>
          </div>
        )}

        <div className="slate-panel-section">
          <div className="slate-panel-label">Activity log</div>
          {activities.length === 0 && <div className="slate-panel-muted">No activity logged yet.</div>}
          {activities.map((a) => (
            <div className="slate-activity-log-item" key={a.id}>
              {a.content} <span className="slate-panel-muted">· {new Date(a.occurred_at).toLocaleDateString()}</span>
            </div>
          ))}
          <div className="slate-activity-add-row">
            <input
              placeholder="Log a note…"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && submitNote()}
              disabled={!canWrite}
            />
            <button className="slate-btn slate-btn-primary slate-btn-sm" onClick={submitNote} disabled={!canWrite}>
              Add
            </button>
          </div>
        </div>

        <button className="slate-btn slate-btn-primary" onClick={() => onAskCopilot(deal)}>
          <Sparkles size={14} /> Ask Copilot about this deal
        </button>

        {canDelete && (
          <button className="slate-btn slate-btn-ghost slate-btn-sm" style={{ width: '100%', marginTop: 8, color: '#B4442E' }} onClick={handleDelete}>
            <Trash2 size={13} /> Delete deal
          </button>
        )}
      </div>
    </>
  );
}

function CreateDealModal({ onClose, onCreate }) {
  const [form, setForm] = useState({ name: '', company: '', value: '' });
  const [saving, setSaving] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setSaving(true);
    try {
      await onCreate({ name: form.name, company: form.company, value: Number(form.value) || 0, stage: 'Lead' });
      onClose();
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="slate-modal-overlay" onClick={onClose}>
      <form className="slate-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="slate-wordmark" style={{ marginBottom: 0, fontSize: 15 }}>
          New deal
        </div>
        <input
          placeholder="Deal name"
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
          required
        />
        <input
          placeholder="Company"
          value={form.company}
          onChange={(e) => setForm({ ...form, company: e.target.value })}
          required
        />
        <input
          placeholder="Value ($)"
          type="number"
          min="0"
          value={form.value}
          onChange={(e) => setForm({ ...form, value: e.target.value })}
        />
        <div className="slate-modal-row">
          <button type="button" className="slate-btn slate-btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="slate-btn slate-btn-primary" disabled={saving}>
            {saving ? 'Creating…' : 'Create deal'}
          </button>
        </div>
      </form>
    </div>
  );
}

export default function CRMView({ deals, userRole, onCreateDeal, onDealChanged, onAskAboutDeal }) {
  const [selected, setSelected] = useState(null);
  const [showCreate, setShowCreate] = useState(false);
  const canWrite = atLeast(userRole, 'standard_user');

  return (
    <div>
      <div className="slate-kanban">
        {STAGES.map((stage) => (
          <div className="slate-kanban-col" key={stage}>
            <div className="slate-kanban-col-header">
              <span>{stage}</span>
              <span className="slate-kanban-count">{deals.filter((d) => d.stage === stage).length}</span>
            </div>
            <div className="slate-kanban-col-body">
              {deals
                .filter((d) => d.stage === stage)
                .map((d) => (
                  <DealCard deal={d} key={d.id} onClick={() => setSelected(d)} />
                ))}
              {stage === 'Lead' && canWrite && (
                <button className="slate-add-deal-btn" onClick={() => setShowCreate(true)}>
                  <Plus size={13} /> Add deal
                </button>
              )}
            </div>
          </div>
        ))}
      </div>

      <DealPanel
        deal={selected}
        userRole={userRole}
        onClose={() => setSelected(null)}
        onChanged={(updated) => {
          onDealChanged(updated);
          setSelected(updated);
        }}
        onDeleted={() => {
          setSelected(null);
          onDealChanged();
        }}
        onAskCopilot={(deal) => {
          setSelected(null);
          onAskAboutDeal(deal);
        }}
      />

      {showCreate && <CreateDealModal onClose={() => setShowCreate(false)} onCreate={onCreateDeal} />}
    </div>
  );
}
