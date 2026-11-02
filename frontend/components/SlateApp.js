'use client';

import { useCallback, useEffect, useState } from 'react';
import { BarChart3, BookOpen, Boxes, Bot, Briefcase, ClipboardList, FileStack, FileText, Headphones, KeyRound, LayoutDashboard, MailCheck, Megaphone, Palette, Puzzle, Receipt, RefreshCw, ScrollText, Shield, ShieldCheck, Sparkles, ToggleLeft, UserCog, Users, Workflow } from 'lucide-react';
import { createDeal, fetchDeals, resendVerification, verifyEmail } from '../lib/api';
import { callCopilot } from '../lib/copilot';
import { BrandingProvider, useBranding } from './BrandingProvider';
import { FeatureFlagsProvider, useFeatureFlag } from './FeatureFlagsProvider';
import { LiveProvider, useLive } from './LiveProvider';
import DashboardView from './DashboardView';
import CRMView from './CRMView';
import FinanceView from './FinanceView';
import AnalyticsView from './AnalyticsView';
import InventoryView from './InventoryView';
import SalesView from './SalesView';
import HRMView from './HRMView';
import SupportView from './SupportView';
import ERPView from './ERPView';
import DocumentsView from './DocumentsView';
import ProjectsView from './ProjectsView';
import MarketingView from './MarketingView';
import KnowledgeBaseView from './KnowledgeBaseView';
import ApiManagerView from './ApiManagerView';
import AIStudioView from './AIStudioView';
import AutomationView from './AutomationView';
import AgentBuilderView from './AgentBuilderView';
import FeatureFlagsView from './FeatureFlagsView';
import BrandingView from './BrandingView';
import PluginsView from './PluginsView';
import SecurityView from './SecurityView';
import AuditLogView from './AuditLogView';
import TeamView from './TeamView';
import CopilotView from './CopilotView';

function BrandMark() {
  const branding = useBranding();
  return (
    <div className="slate-wordmark">
      {branding.logoSrc ? (
        <img src={branding.logoSrc} alt={branding.display_name} className="slate-wordmark-logo" />
      ) : (
        branding.display_name || 'Slate'
      )}
    </div>
  );
}

function LiveIndicator() {
  const { connected } = useLive();
  const on = useFeatureFlag('realtime_updates');
  if (!on) return null;
  return (
    <span className={`slate-live-dot ${connected ? 'on' : ''}`} title={connected ? 'Live updates connected' : 'Reconnecting…'}>
      <span className="slate-live-dot-core" /> {connected ? 'Live' : 'Connecting…'}
    </span>
  );
}

function LiveGate({ children }) {
  const enabled = useFeatureFlag('realtime_updates');
  return <LiveProvider enabled={enabled}>{children}</LiveProvider>;
}

function Signal({ text, loading, onRefresh }) {
  return (
    <div className="slate-signal">
      <Sparkles size={15} strokeWidth={2} />
      <span className="slate-signal-text">{loading ? 'Reading the pipeline…' : text}</span>
      <button className="slate-signal-refresh" onClick={onRefresh} aria-label="Refresh insight" disabled={loading}>
        <RefreshCw size={13} strokeWidth={2} className={loading ? 'spin' : ''} />
      </button>
    </div>
  );
}

function VerifyBanner({ onVerified }) {
  const [token, setToken] = useState('');
  const [status, setStatus] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setStatus('');
    try {
      await verifyEmail(token);
      onVerified();
    } catch (err) {
      setStatus(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function resend() {
    setBusy(true);
    setStatus('');
    try {
      await resendVerification();
      setStatus('Verification email resent — check the backend terminal for the token (dev mode, no email provider configured).');
    } catch (err) {
      setStatus(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="slate-verify-banner">
      <MailCheck size={14} />
      <span>Email not verified.</span>
      <form className="slate-verify-form" onSubmit={submit}>
        <input placeholder="Paste verification token…" value={token} onChange={(e) => setToken(e.target.value)} />
        <button className="slate-btn slate-btn-primary slate-btn-sm" type="submit" disabled={busy || !token}>Verify</button>
      </form>
      <button className="slate-verify-resend" onClick={resend} disabled={busy}>Resend</button>
      {status && <span className="slate-verify-status">{status}</span>}
    </div>
  );
}

export default function SlateApp(props) {
  return (
    <FeatureFlagsProvider>
      <BrandingProvider>
        <LiveGate>
          <SlateAppInner {...props} />
        </LiveGate>
      </BrandingProvider>
    </FeatureFlagsProvider>
  );
}

function SlateAppInner({ user, onLogout, onUserUpdate }) {
  const [view, setView] = useState('dashboard');
  const [deals, setDeals] = useState([]);
  const [dealsLoading, setDealsLoading] = useState(true);
  const [signal, setSignal] = useState('');
  const [signalLoading, setSignalLoading] = useState(false);
  const [messages, setMessages] = useState([
    { role: 'assistant', content: "Hi — I'm the Slate Copilot. I can see your real pipeline. Ask me anything." },
  ]);
  const [input, setInput] = useState('');
  const [chatLoading, setChatLoading] = useState(false);

  const loadDeals = useCallback(() => {
    setDealsLoading(true);
    fetchDeals()
      .then(setDeals)
      .catch(() => setDeals([]))
      .finally(() => setDealsLoading(false));
  }, []);

  useEffect(() => {
    loadDeals();
  }, [loadDeals]);

  const refreshSignal = useCallback(async () => {
    setSignalLoading(true);
    try {
      const text = await callCopilot([
        {
          role: 'user',
          content: 'In one sentence, under 20 words, give the single most useful observation about this pipeline right now. No preamble, no quotes.',
        },
      ]);
      setSignal(text.replace(/^"|"$/g, ''));
    } catch (e) {
      setSignal('Copilot is unavailable right now — check that ANTHROPIC_API_KEY is set on the backend.');
    } finally {
      setSignalLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!dealsLoading) refreshSignal();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dealsLoading]);

  const sendMessage = useCallback(
    async (text) => {
      const next = [...messages, { role: 'user', content: text }];
      setMessages(next);
      setInput('');
      setChatLoading(true);
      try {
        const reply = await callCopilot(next);
        setMessages([...next, { role: 'assistant', content: reply }]);
      } catch (e) {
        setMessages([...next, { role: 'assistant', content: "I couldn't reach the Copilot just now — try again in a moment." }]);
      } finally {
        setChatLoading(false);
      }
    },
    [messages]
  );

  const askAboutDeal = useCallback(
    (deal) => {
      setView('copilot');
      sendMessage(`What's the status on the ${deal.company} deal (${deal.name}), and what should I do next?`);
    },
    [sendMessage]
  );

  const handleCreateDeal = useCallback(
    async (payload) => {
      await createDeal(payload);
      loadDeals();
    },
    [loadDeals]
  );

  return (
    <div className="slate-app">
      <div className="slate-shell">
        <aside className="slate-sidebar">
          <BrandMark />
          <nav className="slate-nav">
            <button className={`slate-nav-item ${view === 'dashboard' ? 'active' : ''}`} onClick={() => setView('dashboard')}>
              <LayoutDashboard size={16} /> <span>Overview</span>
            </button>
            <button className={`slate-nav-item ${view === 'crm' ? 'active' : ''}`} onClick={() => setView('crm')}>
              <Users size={16} /> <span>CRM</span>
            </button>
            <button className={`slate-nav-item ${view === 'finance' ? 'active' : ''}`} onClick={() => setView('finance')}>
              <Receipt size={16} /> <span>Finance</span>
            </button>
            <button className={`slate-nav-item ${view === 'analytics' ? 'active' : ''}`} onClick={() => setView('analytics')}>
              <BarChart3 size={16} /> <span>Analytics</span>
            </button>
            <button className={`slate-nav-item ${view === 'inventory' ? 'active' : ''}`} onClick={() => setView('inventory')}>
              <Boxes size={16} /> <span>Inventory</span>
            </button>
            <button className={`slate-nav-item ${view === 'sales' ? 'active' : ''}`} onClick={() => setView('sales')}>
              <FileText size={16} /> <span>Sales</span>
            </button>
            <button className={`slate-nav-item ${view === 'hrm' ? 'active' : ''}`} onClick={() => setView('hrm')}>
              <UserCog size={16} /> <span>HRM</span>
            </button>
            <button className={`slate-nav-item ${view === 'support' ? 'active' : ''}`} onClick={() => setView('support')}>
              <Headphones size={16} /> <span>Support</span>
            </button>
            <button className={`slate-nav-item ${view === 'erp' ? 'active' : ''}`} onClick={() => setView('erp')}>
              <Briefcase size={16} /> <span>ERP</span>
            </button>
            <button className={`slate-nav-item ${view === 'documents' ? 'active' : ''}`} onClick={() => setView('documents')}>
              <FileStack size={16} /> <span>Documents</span>
            </button>
            <button className={`slate-nav-item ${view === 'projects' ? 'active' : ''}`} onClick={() => setView('projects')}>
              <ClipboardList size={16} /> <span>Projects</span>
            </button>
            <button className={`slate-nav-item ${view === 'marketing' ? 'active' : ''}`} onClick={() => setView('marketing')}>
              <Megaphone size={16} /> <span>Marketing</span>
            </button>
            <button className={`slate-nav-item ${view === 'kb' ? 'active' : ''}`} onClick={() => setView('kb')}>
              <BookOpen size={16} /> <span>Knowledge Base</span>
            </button>
            <button className={`slate-nav-item ${view === 'ai-studio' ? 'active' : ''}`} onClick={() => setView('ai-studio')}>
              <Sparkles size={16} /> <span>AI Studio</span>
            </button>
            <button className={`slate-nav-item ${view === 'automation' ? 'active' : ''}`} onClick={() => setView('automation')}>
              <Workflow size={16} /> <span>Automation</span>
            </button>
            <button className={`slate-nav-item ${view === 'agent-builder' ? 'active' : ''}`} onClick={() => setView('agent-builder')}>
              <Bot size={16} /> <span>Agent Builder</span>
            </button>
            <button className={`slate-nav-item ${view === 'api-manager' ? 'active' : ''}`} onClick={() => setView('api-manager')}>
              <KeyRound size={16} /> <span>API & Dev Portal</span>
            </button>
            <button className={`slate-nav-item ${view === 'plugins' ? 'active' : ''}`} onClick={() => setView('plugins')}>
              <Puzzle size={16} /> <span>Plugins</span>
            </button>
            <button className={`slate-nav-item ${view === 'feature-flags' ? 'active' : ''}`} onClick={() => setView('feature-flags')}>
              <ToggleLeft size={16} /> <span>Feature Flags</span>
            </button>
            <button className={`slate-nav-item ${view === 'branding' ? 'active' : ''}`} onClick={() => setView('branding')}>
              <Palette size={16} /> <span>Branding</span>
            </button>
            <button className={`slate-nav-item ${view === 'security' ? 'active' : ''}`} onClick={() => setView('security')}>
              <ShieldCheck size={16} /> <span>Security</span>
            </button>
            <button className={`slate-nav-item ${view === 'audit-log' ? 'active' : ''}`} onClick={() => setView('audit-log')}>
              <ScrollText size={16} /> <span>Audit Log</span>
            </button>
            <button className={`slate-nav-item ${view === 'team' ? 'active' : ''}`} onClick={() => setView('team')}>
              <Shield size={16} /> <span>Team</span>
            </button>
            <button className={`slate-nav-item ${view === 'copilot' ? 'active' : ''}`} onClick={() => setView('copilot')}>
              <Sparkles size={16} /> <span>AI Copilot</span>
            </button>
          </nav>

          <div className="slate-user-footer">
            <div>
              <div className="slate-user-name">{user.name}</div>
              <div className="slate-user-tenant">{user.tenant}</div>
            </div>
            <button className="slate-logout" onClick={onLogout}>
              Log out
            </button>
          </div>
        </aside>

        <main className="slate-main">
          {!user.email_verified && <VerifyBanner onVerified={() => onUserUpdate({ ...user, email_verified: true })} />}
          <Signal text={signal} loading={signalLoading} onRefresh={refreshSignal} />
          <div className="slate-content">
            <div className="slate-view-header">
              <h2>
                {{
                  dashboard: 'Overview', crm: 'Pipeline', finance: 'Invoices', analytics: 'Analytics',
                  inventory: 'Inventory', sales: 'Quotes & Orders', hrm: 'HRM', support: 'Support',
                  erp: 'Procurement', documents: 'Documents', projects: 'Projects', marketing: 'Marketing',
                  kb: 'Knowledge Base', 'ai-studio': 'AI Studio', automation: 'Automation Studio',
                  'agent-builder': 'Agent Builder', 'api-manager': 'API & Developer Portal',
                  plugins: 'Plugins', 'feature-flags': 'Feature Flags', branding: 'Branding',
                  security: 'Security', 'audit-log': 'Audit Log',
                  team: 'Team', copilot: 'AI Copilot',
                }[view]}
              </h2>
              {view === 'crm' && <span className="slate-view-sub">{deals.length} deals</span>}
              <LiveIndicator />
            </div>

            {(view === 'dashboard' || view === 'crm') && dealsLoading && (
              <div className="slate-empty-note">Loading your pipeline…</div>
            )}
            {view === 'dashboard' && !dealsLoading && <DashboardView deals={deals} />}
            {view === 'crm' && !dealsLoading && (
              <CRMView deals={deals} userRole={user.role} onCreateDeal={handleCreateDeal} onDealChanged={loadDeals} onAskAboutDeal={askAboutDeal} />
            )}
            {view === 'finance' && <FinanceView userRole={user.role} />}
            {view === 'analytics' && <AnalyticsView />}
            {view === 'inventory' && <InventoryView userRole={user.role} />}
            {view === 'sales' && <SalesView userRole={user.role} />}
            {view === 'hrm' && <HRMView userRole={user.role} currentUser={user} onUserUpdate={onUserUpdate} />}
            {view === 'support' && <SupportView userRole={user.role} />}
            {view === 'erp' && <ERPView userRole={user.role} />}
            {view === 'documents' && <DocumentsView userRole={user.role} />}
            {view === 'projects' && <ProjectsView userRole={user.role} />}
            {view === 'marketing' && <MarketingView userRole={user.role} />}
            {view === 'kb' && <KnowledgeBaseView userRole={user.role} />}
            {view === 'ai-studio' && <AIStudioView userRole={user.role} />}
            {view === 'automation' && <AutomationView userRole={user.role} />}
            {view === 'agent-builder' && <AgentBuilderView userRole={user.role} />}
            {view === 'api-manager' && <ApiManagerView userRole={user.role} />}
            {view === 'plugins' && <PluginsView userRole={user.role} />}
            {view === 'feature-flags' && <FeatureFlagsView userRole={user.role} />}
            {view === 'branding' && <BrandingView userRole={user.role} />}
            {view === 'security' && <SecurityView user={user} onUserUpdate={onUserUpdate} />}
            {view === 'audit-log' && <AuditLogView userRole={user.role} />}
            {view === 'team' && <TeamView currentUser={user} />}
            {view === 'copilot' && (
              <CopilotView messages={messages} input={input} setInput={setInput} onSend={sendMessage} loading={chatLoading} />
            )}
          </div>
        </main>
      </div>
    </div>
  );
}
