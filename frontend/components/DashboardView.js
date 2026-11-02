'use client';

import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { Clock, TrendingUp } from 'lucide-react';

const STAGES = ['Lead', 'Qualified', 'Proposal', 'Won'];

function money(n) {
  return '$' + Number(n).toLocaleString('en-US');
}

function daysSince(iso) {
  return Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 86400000));
}

export default function DashboardView({ deals }) {
  const open = deals.filter((d) => d.stage !== 'Won' && d.stage !== 'Lost');
  const openValue = open.reduce((s, d) => s + d.value, 0);
  const won = deals.filter((d) => d.stage === 'Won').reduce((s, d) => s + d.value, 0);
  const atRisk = deals.filter((d) => d.stage !== 'Won' && d.stage !== 'Lost' && daysSince(d.last_activity_at) >= 5);
  const avg = deals.length ? Math.round(deals.reduce((s, d) => s + d.value, 0) / deals.length) : 0;

  const stageData = STAGES.map((stage) => ({
    stage,
    value: deals.filter((d) => d.stage === stage).reduce((s, d) => s + d.value, 0),
  }));

  const recent = [...deals]
    .sort((a, b) => new Date(b.last_activity_at) - new Date(a.last_activity_at))
    .slice(0, 5);

  return (
    <div>
      <div className="slate-kpi-grid">
        <div className="slate-kpi-card">
          <div className="slate-kpi-label">Open pipeline</div>
          <div className="slate-kpi-value num">{money(openValue)}</div>
          <div className="slate-kpi-trend neutral">{open.length} open deals</div>
        </div>
        <div className="slate-kpi-card">
          <div className="slate-kpi-label">Won total</div>
          <div className="slate-kpi-value num">{money(won)}</div>
          <div className="slate-kpi-trend up">
            <TrendingUp size={12} /> {deals.filter((d) => d.stage === 'Won').length} deals closed
          </div>
        </div>
        <div className="slate-kpi-card">
          <div className="slate-kpi-label">At risk</div>
          <div className="slate-kpi-value num">{atRisk.length}</div>
          <div className="slate-kpi-trend warn">
            <Clock size={12} /> 5+ days quiet
          </div>
        </div>
        <div className="slate-kpi-card">
          <div className="slate-kpi-label">Avg. deal size</div>
          <div className="slate-kpi-value num">{money(avg)}</div>
          <div className="slate-kpi-trend neutral">Across {deals.length} deals</div>
        </div>
      </div>

      <div className="slate-row">
        <div className="slate-chart-card">
          <div className="slate-card-title">Pipeline value by stage</div>
          {deals.length === 0 ? (
            <div className="slate-empty-note">No deals yet — add one from the CRM tab.</div>
          ) : (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={stageData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                <XAxis dataKey="stage" stroke="#9CA0A6" fontSize={11} tickLine={false} axisLine={false} />
                <YAxis hide />
                <Tooltip
                  formatter={(v) => money(v)}
                  contentStyle={{ background: '#14161A', border: 'none', borderRadius: 8, fontSize: 12, color: '#FAFAF8' }}
                  labelStyle={{ color: '#9CA0A6' }}
                  cursor={{ fill: 'rgba(14,124,107,0.06)' }}
                />
                <Bar dataKey="value" fill="#0E7C6B" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </div>

        <div className="slate-activity-card">
          <div className="slate-card-title">Recently touched</div>
          {recent.length === 0 && <div className="slate-empty-note">Nothing yet.</div>}
          {recent.map((d) => {
            const risky = d.stage !== 'Won' && d.stage !== 'Lost' && daysSince(d.last_activity_at) >= 5;
            const days = daysSince(d.last_activity_at);
            return (
              <div className={`slate-activity-item ${risky ? 'risk' : ''}`} key={d.id}>
                <div className="slate-activity-dot" />
                <div>
                  <div className="slate-activity-text">
                    {d.name} — {d.stage}
                  </div>
                  <div className="slate-activity-sub">
                    {d.company} · {days === 0 ? 'today' : `${days}d ago`}
                    {risky ? ' · flagged' : ''}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
