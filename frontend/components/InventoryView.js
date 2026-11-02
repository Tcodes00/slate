'use client';

import { useEffect, useState } from 'react';
import { AlertTriangle, Plus, X } from 'lucide-react';
import { createInventoryItem, fetchInventoryItems, fetchMovements, recordMovement } from '../lib/api';

const ROLE_RANK = { read_only: 0, standard_user: 1, manager: 2, org_admin: 3 };
const atLeast = (role, min) => (ROLE_RANK[role] ?? 0) >= ROLE_RANK[min];

function money(n) {
  return '$' + Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function CreateItemModal({ onClose, onCreate }) {
  const [form, setForm] = useState({ sku: '', name: '', unit_of_measure: 'unit', quantity_on_hand: '', reorder_point: '', unit_cost: '' });
  const [saving, setSaving] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setSaving(true);
    try {
      await onCreate({
        sku: form.sku, name: form.name, unit_of_measure: form.unit_of_measure,
        quantity_on_hand: Number(form.quantity_on_hand) || 0,
        reorder_point: Number(form.reorder_point) || 0,
        unit_cost: Number(form.unit_cost) || 0,
      });
      onClose();
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="slate-modal-overlay" onClick={onClose}>
      <form className="slate-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="slate-wordmark" style={{ marginBottom: 0, fontSize: 15 }}>New item</div>
        <input placeholder="SKU" value={form.sku} onChange={(e) => setForm({ ...form, sku: e.target.value })} required />
        <input placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
        <input placeholder="Unit (e.g. unit, roll, box)" value={form.unit_of_measure} onChange={(e) => setForm({ ...form, unit_of_measure: e.target.value })} />
        <input placeholder="Starting quantity" type="number" min="0" value={form.quantity_on_hand} onChange={(e) => setForm({ ...form, quantity_on_hand: e.target.value })} />
        <input placeholder="Reorder point" type="number" min="0" value={form.reorder_point} onChange={(e) => setForm({ ...form, reorder_point: e.target.value })} />
        <input placeholder="Unit cost ($)" type="number" min="0" step="0.01" value={form.unit_cost} onChange={(e) => setForm({ ...form, unit_cost: e.target.value })} />
        <div className="slate-modal-row">
          <button type="button" className="slate-btn slate-btn-ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="slate-btn slate-btn-primary" disabled={saving}>{saving ? 'Creating…' : 'Create item'}</button>
        </div>
      </form>
    </div>
  );
}

function MovementPanel({ item, userRole, onClose, onChanged }) {
  const [movements, setMovements] = useState([]);
  const [form, setForm] = useState({ movement_type: 'in', quantity: '', note: '' });
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const canWrite = atLeast(userRole, 'standard_user');

  function load() {
    fetchMovements(item.id).then(setMovements);
  }
  useEffect(load, [item]);

  async function submit(e) {
    e.preventDefault();
    setError('');
    setSaving(true);
    try {
      const updated = await recordMovement(item.id, { ...form, quantity: Number(form.quantity) });
      onChanged(updated);
      setForm({ movement_type: 'in', quantity: '', note: '' });
      load();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <div className="slate-panel-overlay" onClick={onClose} />
      <div className="slate-panel">
        <button className="slate-panel-close" onClick={onClose} aria-label="Close"><X size={16} /></button>
        <div className="slate-panel-eyebrow">{item.sku}</div>
        <h3 className="slate-panel-title">{item.name}</h3>
        <div className="slate-panel-value num">{item.quantity_on_hand} {item.unit_of_measure}</div>

        {canWrite ? (
          <form onSubmit={submit} className="slate-panel-section" style={{ borderTop: 'none', paddingTop: 0 }}>
            <div className="slate-panel-label">Record a movement</div>
            <select value={form.movement_type} onChange={(e) => setForm({ ...form, movement_type: e.target.value })} style={{ width: '100%', marginBottom: 6 }}>
              <option value="in">Stock in</option>
              <option value="out">Stock out</option>
              <option value="adjustment">Adjustment (set exact count)</option>
            </select>
            <div className="slate-activity-add-row">
              <input type="number" min="0" placeholder="Quantity" value={form.quantity} onChange={(e) => setForm({ ...form, quantity: e.target.value })} required />
              <button className="slate-btn slate-btn-primary slate-btn-sm" type="submit" disabled={saving}>Save</button>
            </div>
            <input placeholder="Note (optional)" value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} style={{ width: '100%', marginTop: 6, border: '1px solid var(--line)', borderRadius: 6, padding: '7px 9px', fontSize: '12.5px' }} />
            {error && <div className="slate-login-error" style={{ marginTop: 6 }}>{error}</div>}
          </form>
        ) : (
          <div className="slate-empty-note">Read-only access — viewing only.</div>
        )}

        <div className="slate-panel-section">
          <div className="slate-panel-label">History</div>
          {movements.length === 0 && <div className="slate-panel-muted">No movements yet.</div>}
          {movements.map((m) => (
            <div className="slate-activity-log-item" key={m.id}>
              <strong>{m.movement_type}</strong> {m.quantity} {item.unit_of_measure}
              {m.note ? ` — ${m.note}` : ''}
              <span className="slate-panel-muted"> · {new Date(m.occurred_at).toLocaleDateString()}</span>
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

export default function InventoryView({ userRole }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(null);
  const [showCreate, setShowCreate] = useState(false);
  const canWrite = atLeast(userRole, 'standard_user');

  function load() {
    setLoading(true);
    fetchInventoryItems().then(setItems).finally(() => setLoading(false));
  }
  useEffect(load, []);

  if (loading) return <div className="slate-empty-note">Loading inventory…</div>;

  return (
    <div>
      <table className="slate-invoice-table">
        <thead>
          <tr>
            <th>SKU</th><th>Item</th><th>On hand</th><th>Reorder at</th><th>Unit cost</th><th></th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id} onClick={() => setSelected(item)} style={{ cursor: 'pointer' }}>
              <td className="num">{item.sku}</td>
              <td>{item.name}</td>
              <td className="num">
                {item.quantity_on_hand} {item.unit_of_measure}
                {item.low_stock && (
                  <span className="slate-status-badge overdue" style={{ marginLeft: 8 }}>
                    <AlertTriangle size={10} style={{ verticalAlign: -1 }} /> Low
                  </span>
                )}
              </td>
              <td className="num">{item.reorder_point}</td>
              <td className="num">{money(item.unit_cost)}</td>
              <td></td>
            </tr>
          ))}
        </tbody>
      </table>

      {canWrite && (
        <div style={{ marginTop: 14 }}>
          <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setShowCreate(true)}>
            <Plus size={13} /> New item
          </button>
        </div>
      )}

      {selected && (
        <MovementPanel
          item={selected}
          userRole={userRole}
          onClose={() => setSelected(null)}
          onChanged={(updated) => {
            setItems(items.map((i) => (i.id === updated.id ? updated : i)));
            setSelected(updated);
          }}
        />
      )}

      {showCreate && (
        <CreateItemModal
          onClose={() => setShowCreate(false)}
          onCreate={async (payload) => {
            await createInventoryItem(payload);
            load();
          }}
        />
      )}
    </div>
  );
}
