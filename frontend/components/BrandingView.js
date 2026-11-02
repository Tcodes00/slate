'use client';

import { useRef, useState } from 'react';
import { Image as ImageIcon, Upload } from 'lucide-react';
import { updateBranding, uploadLogo } from '../lib/api';
import { useBranding } from './BrandingProvider';
import FlagGate from './FlagGate';

const ROLE_RANK = { read_only: 0, standard_user: 1, manager: 2, org_admin: 3 };
const atLeast = (role, min) => (ROLE_RANK[role] ?? 0) >= ROLE_RANK[min];

function BrandingForm({ userRole }) {
  const branding = useBranding();
  const [form, setForm] = useState({
    display_name: branding.display_name, primary_color: branding.primary_color, secondary_color: branding.secondary_color,
  });
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const fileRef = useRef(null);
  const canWrite = atLeast(userRole, 'org_admin');

  async function save(e) {
    e.preventDefault();
    setSaving(true);
    setError('');
    try {
      await updateBranding(form);
      await branding.reload();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function onFile(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    setError('');
    try {
      await uploadLogo(file);
      await branding.reload();
    } catch (err) {
      setError(err.message);
    } finally {
      setUploading(false);
      e.target.value = '';
    }
  }

  return (
    <form className="slate-branding-form" onSubmit={save}>
      <div className="slate-branding-row">
        <div className="slate-branding-logo-preview">
          {branding.logoSrc ? <img src={branding.logoSrc} alt="Workspace logo" /> : <ImageIcon size={22} color="var(--ink-faint)" />}
        </div>
        <div>
          <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" disabled={!canWrite || uploading} onClick={() => fileRef.current?.click()}>
            <Upload size={13} /> {uploading ? 'Uploading…' : 'Upload logo'}
          </button>
          <input ref={fileRef} type="file" accept="image/*" hidden onChange={onFile} disabled={!canWrite} />
          <div className="slate-flag-desc" style={{ marginTop: 6 }}>PNG, JPEG, WEBP, GIF, or SVG — up to 2MB.</div>
        </div>
      </div>

      <label className="slate-branding-label" htmlFor="branding-name">Workspace name</label>
      <input id="branding-name" value={form.display_name || ''} onChange={(e) => setForm({ ...form, display_name: e.target.value })} placeholder="Slate" disabled={!canWrite} />

      <div className="slate-branding-colors">
        <div>
          <label className="slate-branding-label" htmlFor="branding-primary">Primary color</label>
          <div className="slate-color-input">
            <input id="branding-primary" type="color" value={form.primary_color} onChange={(e) => setForm({ ...form, primary_color: e.target.value })} disabled={!canWrite} />
            <span>{form.primary_color}</span>
          </div>
        </div>
        <div>
          <label className="slate-branding-label" htmlFor="branding-secondary">Secondary color</label>
          <div className="slate-color-input">
            <input id="branding-secondary" type="color" value={form.secondary_color} onChange={(e) => setForm({ ...form, secondary_color: e.target.value })} disabled={!canWrite} />
            <span>{form.secondary_color}</span>
          </div>
        </div>
      </div>

      {error && <div className="slate-login-error">{error}</div>}
      {canWrite && (
        <button className="slate-btn slate-btn-primary slate-btn-sm" type="submit" disabled={saving} style={{ width: 'auto' }}>
          {saving ? 'Saving…' : 'Save branding'}
        </button>
      )}
    </form>
  );
}

export default function BrandingView({ userRole }) {
  return (
    <div>
      <p className="slate-view-intro">
        Applies live across the whole app the moment you save — the sidebar, buttons, and every accent
        color read from these two values instead of the default Slate palette.
      </p>
      <FlagGate flagKey="white_label_branding" label="White-label branding" userRole={userRole}>
        <BrandingForm userRole={userRole} />
      </FlagGate>
    </div>
  );
}
