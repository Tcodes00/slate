'use client';

import { Lock } from 'lucide-react';
import { useFeatureFlag, useFeatureFlags } from './FeatureFlagsProvider';

const ROLE_RANK = { read_only: 0, standard_user: 1, manager: 2, org_admin: 3 };
const atLeast = (role, min) => (ROLE_RANK[role] ?? 0) >= ROLE_RANK[min];

export default function FlagGate({ flagKey, label, userRole, children }) {
  const enabled = useFeatureFlag(flagKey);
  const { setFlag } = useFeatureFlags();

  if (enabled) return children;

  const canEnable = atLeast(userRole, 'org_admin');

  return (
    <div className="slate-flag-gate">
      <Lock size={15} />
      <div>
        <div className="slate-flag-gate-title">{label} is turned off for this workspace</div>
        {canEnable ? (
          <button className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => setFlag(flagKey, true)}>
            Turn it on
          </button>
        ) : (
          <div className="slate-flag-gate-sub">Ask an org admin to enable it in Feature Flags.</div>
        )}
      </div>
    </div>
  );
}
