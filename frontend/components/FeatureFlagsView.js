'use client';

import { useState } from 'react';
import { useFeatureFlags } from './FeatureFlagsProvider';

const ROLE_RANK = { read_only: 0, standard_user: 1, manager: 2, org_admin: 3 };
const atLeast = (role, min) => (ROLE_RANK[role] ?? 0) >= ROLE_RANK[min];

export default function FeatureFlagsView({ userRole }) {
  const { flags, loading, setFlag } = useFeatureFlags();
  const [busyKey, setBusyKey] = useState(null);
  const canWrite = atLeast(userRole, 'org_admin');

  async function toggle(f) {
    setBusyKey(f.key);
    try {
      await setFlag(f.key, !f.enabled);
    } finally {
      setBusyKey(null);
    }
  }

  if (loading) return <div className="slate-empty-note">Loading feature flags…</div>;

  return (
    <div>
      <p className="slate-view-intro">
        These switches gate real server-side behavior — real-time push, the plugin loader, multi-agent
        hand-off, and white-label branding all check their flag on every relevant request, not just in
        this screen. {!canWrite && "You're viewing these as read-only; an org admin can change them."}
      </p>

      <div className="slate-flag-list">
        {flags.map((f) => (
          <div className="slate-flag-row" key={f.key}>
            <div className="slate-flag-info">
              <div className="slate-flag-label">{f.label}</div>
              <div className="slate-flag-desc">{f.description}</div>
            </div>
            <button
              type="button"
              className={`slate-toggle ${f.enabled ? 'on' : ''}`}
              role="switch"
              aria-checked={f.enabled}
              aria-label={f.label}
              disabled={!canWrite || busyKey === f.key}
              onClick={() => toggle(f)}
            >
              <span className="slate-toggle-knob" />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
