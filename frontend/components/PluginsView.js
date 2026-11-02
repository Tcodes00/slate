'use client';

import { useEffect, useState } from 'react';
import { Download, Puzzle } from 'lucide-react';
import { downloadPluginFile, fetchPlugins, togglePlugin } from '../lib/api';
import FlagGate from './FlagGate';

const ROLE_RANK = { read_only: 0, standard_user: 1, manager: 2, org_admin: 3 };
const atLeast = (role, min) => (ROLE_RANK[role] ?? 0) >= ROLE_RANK[min];

function PluginList({ userRole }) {
  const [plugins, setPlugins] = useState([]);
  const [loading, setLoading] = useState(true);
  const canWrite = atLeast(userRole, 'org_admin');

  function load() {
    setLoading(true);
    fetchPlugins().then(setPlugins).finally(() => setLoading(false));
  }
  useEffect(load, []);

  async function toggle(p) {
    await togglePlugin(p.key, !p.enabled);
    load();
  }

  if (loading) return <div className="slate-empty-note">Loading plugins…</div>;
  if (plugins.length === 0) return <div className="slate-empty-note">No plugins discovered in backend/plugins/.</div>;

  return (
    <div className="slate-plugin-list">
      {plugins.map((p) => (
        <div className="slate-plugin-card" key={p.key}>
          <div className="slate-plugin-icon"><Puzzle size={16} /></div>
          <div className="slate-plugin-body">
            <div className="slate-plugin-name">{p.name} <span className="slate-plugin-version">v{p.version}</span></div>
            <div className="slate-flag-desc">{p.description}</div>
            {p.load_error && <div className="slate-login-error" style={{ marginTop: 6 }}>Failed to load: {p.load_error}</div>}
            {p.key === 'deal_exporter' && p.enabled && (
              <button
                type="button"
                className="slate-btn slate-btn-ghost slate-btn-sm"
                style={{ marginTop: 8 }}
                onClick={() => downloadPluginFile('/api/plugins/deal_exporter/export.csv', 'deals.csv')}
              >
                <Download size={13} /> Export deals as CSV
              </button>
            )}
          </div>
          <button
            type="button"
            className={`slate-toggle ${p.enabled ? 'on' : ''}`}
            role="switch"
            aria-checked={p.enabled}
            aria-label={p.name}
            disabled={!canWrite}
            onClick={() => toggle(p)}
          >
            <span className="slate-toggle-knob" />
          </button>
        </div>
      ))}
    </div>
  );
}

export default function PluginsView({ userRole }) {
  return (
    <div>
      <p className="slate-view-intro">
        Each plugin below is a folder under <code>backend/plugins/</code>, discovered and mounted
        automatically on server start — nothing to wire up by hand. Toggling one off here blocks its
        routes for this workspace immediately, no restart needed.
      </p>
      <FlagGate flagKey="plugin_system" label="The plugin system" userRole={userRole}>
        <PluginList userRole={userRole} />
      </FlagGate>
    </div>
  );
}
