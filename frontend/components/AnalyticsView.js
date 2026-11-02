'use client';

import { useEffect, useState } from 'react';
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { fetchAnalyticsFunnel, fetchAnalyticsRevenue, fetchCycleTime, fetchWinRate } from '../lib/api';

function money(n) {
  return '$' + Number(n).toLocaleString('en-US');
}

export default function AnalyticsView() {
  const [funnel, setFunnel] = useState([]);
  const [winRate, setWinRate] = useState(null);
  const [cycleTime, setCycleTime] = useState(null);
  const [revenue, setRevenue] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([fetchAnalyticsFunnel(), fetchWinRate(), fetchCycleTime(), fetchAnalyticsRevenue()])
      .then(([f, w, c, r]) => {
        setFunnel(f);
        setWinRate(w);
        setCycleTime(c);
        setRevenue(r);
      })
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="slate-empty-note">Crunching the numbers…</div>;

  return (
    <div>
      <div className="slate-kpi-grid">
        <div className="slate-kpi-card">
          <div className="slate-kpi-label">Win rate</div>
          <div className="slate-kpi-value num">{winRate.win_rate_pct != null ? `${winRate.win_rate_pct}%` : '—'}</div>
          <div className="slate-kpi-trend neutral">{winRate.won} won, {winRate.lost} lost</div>
        </div>
        <div className="slate-kpi-card">
          <div className="slate-kpi-label">Avg. sales cycle</div>
          <div className="slate-kpi-value num">{cycleTime.avg_days_to_win != null ? `${cycleTime.avg_days_to_win}d` : '—'}</div>
          <div className="slate-kpi-trend neutral">Lead to Won, {cycleTime.sample_size} deal{cycleTime.sample_size === 1 ? '' : 's'}</div>
        </div>
        <div className="slate-kpi-card">
          <div className="slate-kpi-label">Revenue collected</div>
          <div className="slate-kpi-value num">{money(revenue.reduce((s, r) => s + r.revenue, 0))}</div>
          <div className="slate-kpi-trend neutral">From paid invoices</div>
        </div>
      </div>

      <div className="slate-row">
        <div className="slate-chart-card">
          <div className="slate-card-title">Conversion funnel — deals that ever reached each stage</div>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={funnel} layout="vertical" margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
              <XAxis type="number" hide />
              <YAxis type="category" dataKey="stage" width={80} stroke="#9CA0A6" fontSize={12} tickLine={false} axisLine={false} />
              <Tooltip
                contentStyle={{ background: '#14161A', border: 'none', borderRadius: 8, fontSize: 12, color: '#FAFAF8' }}
                labelStyle={{ color: '#9CA0A6' }}
              />
              <Bar dataKey="deals_reached" name="Deals" fill="#0E7C6B" radius={[0, 4, 4, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="slate-chart-card">
          <div className="slate-card-title">Revenue by month (paid invoices)</div>
          {revenue.length === 0 ? (
            <div className="slate-empty-note">No paid invoices yet.</div>
          ) : (
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={revenue} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                <XAxis dataKey="month" stroke="#9CA0A6" fontSize={11} tickLine={false} axisLine={false} />
                <YAxis hide />
                <Tooltip
                  formatter={(v) => money(v)}
                  contentStyle={{ background: '#14161A', border: 'none', borderRadius: 8, fontSize: 12, color: '#FAFAF8' }}
                  labelStyle={{ color: '#9CA0A6' }}
                />
                <Bar dataKey="revenue" fill="#0E7C6B" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>
    </div>
  );
}
