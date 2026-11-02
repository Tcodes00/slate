export const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
const TOKEN_KEY = 'slate_token';

export function getToken() {
  if (typeof window === 'undefined') return null;
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

export async function apiFetch(path, options = {}) {
  const token = getToken();
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (token) headers['Authorization'] = `Bearer ${token}`;

  const res = await fetch(`${API_URL}${path}`, { ...options, headers });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: `Request failed (${res.status})` }));
    throw new Error(body.detail || `Request failed (${res.status})`);
  }
  if (res.status === 204) return null;
  return res.json();
}

export const login = (email, password) =>
  apiFetch('/api/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) });

export const mfaVerifyLogin = (mfa_token, code) =>
  apiFetch('/api/auth/mfa/verify-login', { method: 'POST', body: JSON.stringify({ mfa_token, code }) });

export const mfaSetup = () => apiFetch('/api/auth/mfa/setup', { method: 'POST' });
export const mfaConfirm = (code) => apiFetch('/api/auth/mfa/confirm', { method: 'POST', body: JSON.stringify({ code }) });
export const mfaDisable = (code) => apiFetch('/api/auth/mfa/disable', { method: 'POST', body: JSON.stringify({ code }) });

export const fetchAuditLog = () => apiFetch('/api/audit-log');

export const linkEmployeeToMe = (employeeId) => apiFetch(`/api/hrm/employees/${employeeId}/link-me`, { method: 'POST' });

export const signup = (tenantName, name, email, password) =>
  apiFetch('/api/auth/signup', {
    method: 'POST',
    body: JSON.stringify({ tenant_name: tenantName, name, email, password }),
  });

export const fetchMe = () => apiFetch('/api/auth/me');

export const fetchDeals = () => apiFetch('/api/deals');

export const createDeal = (deal) =>
  apiFetch('/api/deals', { method: 'POST', body: JSON.stringify(deal) });

export const updateDeal = (id, patch) =>
  apiFetch(`/api/deals/${id}`, { method: 'PATCH', body: JSON.stringify(patch) });

export const deleteDeal = (id) => apiFetch(`/api/deals/${id}`, { method: 'DELETE' });

export const fetchActivities = (dealId) => apiFetch(`/api/deals/${dealId}/activities`);

export const addActivity = (dealId, content) =>
  apiFetch(`/api/deals/${dealId}/activities`, { method: 'POST', body: JSON.stringify({ content }) });

export const fetchInvoices = () => apiFetch('/api/finance/invoices');

export const fetchFinanceSummary = () => apiFetch('/api/finance/summary');

export const generateInvoiceFromDeal = (dealId) =>
  apiFetch(`/api/finance/generate-from-deal/${dealId}`, { method: 'POST' });

export const createInvoice = (invoice) =>
  apiFetch('/api/finance/invoices', { method: 'POST', body: JSON.stringify(invoice) });

export const updateInvoice = (id, patch) =>
  apiFetch(`/api/finance/invoices/${id}`, { method: 'PATCH', body: JSON.stringify(patch) });

export async function downloadInvoicePdf(id, filename) {
  const token = getToken();
  const res = await fetch(`${API_URL}/api/finance/invoices/${id}/pdf`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) throw new Error('Could not generate PDF');
  const blob = await res.blob();
  const url = window.URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename || 'invoice.pdf';
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.URL.revokeObjectURL(url);
}

export const fetchTeam = () => apiFetch('/api/auth/team');

export const inviteTeammate = (payload) =>
  apiFetch('/api/auth/invite', { method: 'POST', body: JSON.stringify(payload) });

export const requestPasswordReset = (email) =>
  apiFetch('/api/auth/request-password-reset', { method: 'POST', body: JSON.stringify({ email }) });

export const confirmPasswordReset = (token, newPassword) =>
  apiFetch('/api/auth/reset-password', { method: 'POST', body: JSON.stringify({ token, new_password: newPassword }) });

export const verifyEmail = (token) =>
  apiFetch('/api/auth/verify-email', { method: 'POST', body: JSON.stringify({ token }) });

export const resendVerification = () => apiFetch('/api/auth/resend-verification', { method: 'POST' });

export const fetchAnalyticsFunnel = () => apiFetch('/api/analytics/funnel');
export const fetchWinRate = () => apiFetch('/api/analytics/win-rate');
export const fetchCycleTime = () => apiFetch('/api/analytics/cycle-time');
export const fetchAnalyticsRevenue = () => apiFetch('/api/analytics/revenue');

export const fetchInventoryItems = () => apiFetch('/api/inventory/items');
export const createInventoryItem = (payload) =>
  apiFetch('/api/inventory/items', { method: 'POST', body: JSON.stringify(payload) });
export const fetchMovements = (itemId) => apiFetch(`/api/inventory/items/${itemId}/movements`);
export const recordMovement = (itemId, payload) =>
  apiFetch(`/api/inventory/items/${itemId}/movements`, { method: 'POST', body: JSON.stringify(payload) });

export const fetchQuotes = () => apiFetch('/api/sales/quotes');
export const createQuote = (payload) =>
  apiFetch('/api/sales/quotes', { method: 'POST', body: JSON.stringify(payload) });
export const updateQuote = (id, status) =>
  apiFetch(`/api/sales/quotes/${id}`, { method: 'PATCH', body: JSON.stringify({ status }) });
export const fetchOrders = () => apiFetch('/api/sales/orders');
export const updateOrder = (id, status) =>
  apiFetch(`/api/sales/orders/${id}`, { method: 'PATCH', body: JSON.stringify({ status }) });

export const fetchEmployees = () => apiFetch('/api/hrm/employees');
export const createEmployee = (payload) =>
  apiFetch('/api/hrm/employees', { method: 'POST', body: JSON.stringify(payload) });
export const fetchLeaveRequests = () => apiFetch('/api/hrm/leave-requests');
export const createLeaveRequest = (payload) =>
  apiFetch('/api/hrm/leave-requests', { method: 'POST', body: JSON.stringify(payload) });
export const updateLeaveRequest = (id, status) =>
  apiFetch(`/api/hrm/leave-requests/${id}`, { method: 'PATCH', body: JSON.stringify({ status }) });

export const fetchTickets = () => apiFetch('/api/support/tickets');
export const createTicket = (payload) =>
  apiFetch('/api/support/tickets', { method: 'POST', body: JSON.stringify(payload) });
export const updateTicket = (id, payload) =>
  apiFetch(`/api/support/tickets/${id}`, { method: 'PATCH', body: JSON.stringify(payload) });
export const fetchTicketMessages = (ticketId) => apiFetch(`/api/support/tickets/${ticketId}/messages`);
export const addTicketMessage = (ticketId, sender, content) =>
  apiFetch(`/api/support/tickets/${ticketId}/messages`, { method: 'POST', body: JSON.stringify({ sender, content }) });

export const fetchVendors = () => apiFetch('/api/erp/vendors');
export const createVendor = (payload) => apiFetch('/api/erp/vendors', { method: 'POST', body: JSON.stringify(payload) });
export const fetchPOs = () => apiFetch('/api/erp/purchase-orders');
export const createPO = (payload) => apiFetch('/api/erp/purchase-orders', { method: 'POST', body: JSON.stringify(payload) });
export const approvePO = (id) => apiFetch(`/api/erp/purchase-orders/${id}/approve`, { method: 'POST' });
export const rejectPO = (id) => apiFetch(`/api/erp/purchase-orders/${id}/reject`, { method: 'POST' });

export const fetchDocuments = () => apiFetch('/api/documents');

export async function createDocument({ title, category, company, file }) {
  const token = getToken();
  const params = new URLSearchParams({ title, category: category || 'General' });
  if (company) params.set('company', company);
  const formData = new FormData();
  if (file) formData.append('file', file);
  const res = await fetch(`${API_URL}/api/documents?${params.toString()}`, {
    method: 'POST',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: formData,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: 'Upload failed' }));
    throw new Error(body.detail || 'Upload failed');
  }
  return res.json();
}

export async function bumpDocumentVersion(docId, file) {
  const token = getToken();
  const formData = new FormData();
  if (file) formData.append('file', file);
  const res = await fetch(`${API_URL}/api/documents/${docId}/new-version`, {
    method: 'POST',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: formData,
  });
  if (!res.ok) throw new Error('Could not upload new version');
  return res.json();
}

export async function downloadDocument(docId, filename) {
  const token = getToken();
  const res = await fetch(`${API_URL}/api/documents/${docId}/download`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) throw new Error('No file attached to this document');
  const blob = await res.blob();
  const url = window.URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename || 'document';
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.URL.revokeObjectURL(url);
}

export const fetchProjects = () => apiFetch('/api/projects');
export const createProject = (payload) => apiFetch('/api/projects', { method: 'POST', body: JSON.stringify(payload) });
export const fetchTasks = (projectId) => apiFetch(`/api/projects/${projectId}/tasks`);
export const createTask = (projectId, payload) => apiFetch(`/api/projects/${projectId}/tasks`, { method: 'POST', body: JSON.stringify(payload) });
export const updateTask = (taskId, status) => apiFetch(`/api/projects/tasks/${taskId}`, { method: 'PATCH', body: JSON.stringify({ status }) });

export const fetchCampaigns = () => apiFetch('/api/marketing/campaigns');
export const createCampaign = (payload) => apiFetch('/api/marketing/campaigns', { method: 'POST', body: JSON.stringify(payload) });
export const updateCampaign = (id, status) => apiFetch(`/api/marketing/campaigns/${id}`, { method: 'PATCH', body: JSON.stringify({ status }) });
export const fetchAudienceSize = (filters = {}) => {
  const params = new URLSearchParams(Object.fromEntries(Object.entries(filters).filter(([, v]) => v !== undefined && v !== '')));
  const qs = params.toString();
  return apiFetch(`/api/marketing/audience-size${qs ? `?${qs}` : ''}`);
};
export const fetchParties = () => apiFetch('/api/marketing/parties');
export const updatePartyProfile = (id, payload) => apiFetch(`/api/marketing/parties/${id}/profile`, { method: 'PATCH', body: JSON.stringify(payload) });
export const fetchSegments = () => apiFetch('/api/marketing/segments');
export const createSegment = (payload) => apiFetch('/api/marketing/segments', { method: 'POST', body: JSON.stringify(payload) });
export const deleteSegment = (id) => apiFetch(`/api/marketing/segments/${id}`, { method: 'DELETE' });
export const fetchSegmentMembers = (id) => apiFetch(`/api/marketing/segments/${id}/members`);

export const fetchArticles = () => apiFetch('/api/kb/articles');
export const createArticle = (payload) => apiFetch('/api/kb/articles', { method: 'POST', body: JSON.stringify(payload) });
export const searchArticles = (q) => apiFetch(`/api/kb/search?q=${encodeURIComponent(q)}`);

export const fetchApiKeys = () => apiFetch('/api/keys');
export const createApiKey = (name) => apiFetch('/api/keys', { method: 'POST', body: JSON.stringify({ name }) });
export const createSelfServeKey = (name) => apiFetch('/api/keys/self-serve', { method: 'POST', body: JSON.stringify({ name }) });
export const revokeApiKey = (id) => apiFetch(`/api/keys/${id}/revoke`, { method: 'POST' });

export const fetchAIStudioSettings = () => apiFetch('/api/ai-studio/settings');
export const updateAIStudioSettings = (provider, model) =>
  apiFetch('/api/ai-studio/settings', { method: 'PATCH', body: JSON.stringify({ provider, model: model || null }) });

export const fetchRules = () => apiFetch('/api/automation/rules');
export const createRule = (payload) => apiFetch('/api/automation/rules', { method: 'POST', body: JSON.stringify(payload) });
export const toggleRule = (id, enabled) => apiFetch(`/api/automation/rules/${id}`, { method: 'PATCH', body: JSON.stringify({ enabled }) });

export const fetchAgents = () => apiFetch('/api/agents');
export const createAgent = (payload) => apiFetch('/api/agents', { method: 'POST', body: JSON.stringify(payload) });
export const toggleAgent = (id, enabled) => apiFetch(`/api/agents/${id}`, { method: 'PATCH', body: JSON.stringify({ enabled }) });
export const invokeAgent = (id, message) => apiFetch(`/api/agents/${id}/invoke`, { method: 'POST', body: JSON.stringify({ message }) });

export const fetchFeatureFlags = () => apiFetch('/api/feature-flags');
export const updateFeatureFlag = (key, enabled) =>
  apiFetch(`/api/feature-flags/${key}`, { method: 'PATCH', body: JSON.stringify({ enabled }) });

export const fetchBranding = () => apiFetch('/api/branding');
export const updateBranding = (payload) => apiFetch('/api/branding', { method: 'PUT', body: JSON.stringify(payload) });

export async function uploadLogo(file) {
  const token = getToken();
  const formData = new FormData();
  formData.append('file', file);
  const res = await fetch(`${API_URL}/api/branding/logo`, {
    method: 'POST',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: formData,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: 'Upload failed' }));
    throw new Error(body.detail || 'Upload failed');
  }
  return res.json();
}

export async function fetchLogoBlobUrl() {
  const token = getToken();
  const res = await fetch(`${API_URL}/api/branding/logo`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) return null;
  const blob = await res.blob();
  return window.URL.createObjectURL(blob);
}

export const fetchPlugins = () => apiFetch('/api/plugins');
export const togglePlugin = (key, enabled) =>
  apiFetch(`/api/plugins/${key}`, { method: 'PATCH', body: JSON.stringify({ enabled }) });

export async function downloadPluginFile(path, filename) {
  const token = getToken();
  const res = await fetch(`${API_URL}${path}`, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
  if (!res.ok) throw new Error('Export failed');
  const blob = await res.blob();
  const url = window.URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename || 'export.csv';
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.URL.revokeObjectURL(url);
}

export const startHandoff = (goal) => apiFetch('/api/agents/handoff', { method: 'POST', body: JSON.stringify({ goal }) });
export const fetchHandoffs = () => apiFetch('/api/agents/handoff');
export const fetchHandoff = (id) => apiFetch(`/api/agents/handoff/${id}`);

export function wsUrl() {
  const token = getToken();
  const httpUrl = new URL(API_URL);
  const proto = httpUrl.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${httpUrl.host}/api/ws?token=${encodeURIComponent(token || '')}`;
}

export const fetchLdapConfig = () => apiFetch('/api/auth/ldap-config');
export const updateLdapConfig = (payload) => apiFetch('/api/auth/ldap-config', { method: 'PUT', body: JSON.stringify(payload) });
export const testLdapConfig = () => apiFetch('/api/auth/ldap-config/test', { method: 'POST' });
export const loginLdap = (tenantSlug, username, password) =>
  apiFetch('/api/auth/login-ldap', { method: 'POST', body: JSON.stringify({ tenant_slug: tenantSlug, username, password }) });

export const fetchSamlConfig = () => apiFetch('/api/auth/saml-config');
export const updateSamlConfig = (payload) => apiFetch('/api/auth/saml-config', { method: 'PUT', body: JSON.stringify(payload) });
export const samlLoginUrl = (tenantSlug) => `${API_URL}/api/auth/saml/login?tenant_slug=${encodeURIComponent(tenantSlug)}`;

export const fetchOauthConfig = () => apiFetch('/api/auth/oauth-config');
export const updateOauthConfig = (payload) => apiFetch('/api/auth/oauth-config', { method: 'PUT', body: JSON.stringify(payload) });
export const oauthLoginUrl = (tenantSlug) => `${API_URL}/api/auth/oauth/login?tenant_slug=${encodeURIComponent(tenantSlug)}`;

export const webauthnRegisterBegin = () => apiFetch('/api/auth/webauthn/register/begin', { method: 'POST' });
export const webauthnRegisterComplete = (challengeId, credential, nickname) =>
  apiFetch('/api/auth/webauthn/register/complete', { method: 'POST', body: JSON.stringify({ challenge_id: challengeId, credential, nickname }) });
export const webauthnListCredentials = () => apiFetch('/api/auth/webauthn/credentials');
export const webauthnDeleteCredential = (id) => apiFetch(`/api/auth/webauthn/credentials/${id}`, { method: 'DELETE' });
export const webauthnLoginBegin = (email) => apiFetch('/api/auth/webauthn/login/begin', { method: 'POST', body: JSON.stringify({ email }) });
export const webauthnLoginComplete = (challengeId, email, credential) =>
  apiFetch('/api/auth/webauthn/login/complete', { method: 'POST', body: JSON.stringify({ challenge_id: challengeId, email, credential }) });

export const fetchModelEndpoints = () => apiFetch('/api/ai-studio/endpoints');
export const createModelEndpoint = (payload) => apiFetch('/api/ai-studio/endpoints', { method: 'POST', body: JSON.stringify(payload) });
export const deleteModelEndpoint = (id) => apiFetch(`/api/ai-studio/endpoints/${id}`, { method: 'DELETE' });
export const testModelEndpoint = (id) => apiFetch(`/api/ai-studio/endpoints/${id}/test`, { method: 'POST' });

export const fetchWorkflows = () => apiFetch('/api/workflows');
export const fetchWorkflow = (id) => apiFetch(`/api/workflows/${id}`);
export const createWorkflow = (payload) => apiFetch('/api/workflows', { method: 'POST', body: JSON.stringify(payload) });
export const updateWorkflow = (id, payload) => apiFetch(`/api/workflows/${id}`, { method: 'PUT', body: JSON.stringify(payload) });
export const setWorkflowEnabled = (id, enabled) => apiFetch(`/api/workflows/${id}/enabled`, { method: 'PATCH', body: JSON.stringify({ enabled }) });
export const deleteWorkflow = (id) => apiFetch(`/api/workflows/${id}`, { method: 'DELETE' });
export const runWorkflow = (id, input) => apiFetch(`/api/workflows/${id}/run`, { method: 'POST', body: JSON.stringify({ input, wait: true }) });
export const fetchWorkflowRuns = (id) => apiFetch(`/api/workflows/${id}/runs`);
export const fetchWorkflowRun = (runId) => apiFetch(`/api/workflows/runs/${runId}`);
export const validateWorkflow = (definition) => apiFetch('/api/workflows/validate', { method: 'POST', body: JSON.stringify({ definition }) });
export const rotateWebhookSecret = (id) => apiFetch(`/api/workflows/${id}/rotate-webhook-secret`, { method: 'POST' });
export const fetchWorkflowCredentials = () => apiFetch('/api/workflow-credentials');
export const createWorkflowCredential = (payload) => apiFetch('/api/workflow-credentials', { method: 'POST', body: JSON.stringify(payload) });
export const deleteWorkflowCredential = (id) => apiFetch(`/api/workflow-credentials/${id}`, { method: 'DELETE' });

export const fetchRagStatus = () => apiFetch('/api/rag/status');
export const fetchRagDocuments = () => apiFetch('/api/rag/documents');
export const deleteRagDocument = (id) => apiFetch(`/api/rag/documents/${id}`, { method: 'DELETE' });
export const ragSearch = (query, topK = 5) => apiFetch('/api/rag/search', { method: 'POST', body: JSON.stringify({ query, top_k: topK }) });
export const ragAsk = (question, topK = 5) => apiFetch('/api/rag/ask', { method: 'POST', body: JSON.stringify({ question, top_k: topK }) });
export const ragIndexKb = () => apiFetch('/api/rag/index/kb', { method: 'POST' });
export const ragReindex = () => apiFetch('/api/rag/reindex', { method: 'POST' });

// Multipart: must NOT go through apiFetch, which forces a JSON Content-Type (the browser has to set the multipart boundary itself).
export async function uploadRagDocument(file, { title, minRole } = {}) {
  const form = new FormData();
  form.append('file', file);
  if (title) form.append('title', title);
  if (minRole) form.append('min_role', minRole);
  const token = getToken();
  const res = await fetch(`${API_URL}/api/rag/documents/upload`, { method: 'POST', headers: token ? { Authorization: `Bearer ${token}` } : {}, body: form });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Upload failed (${res.status})`);
  }
  return res.json();
}

export const fetchComplianceControls = () => apiFetch('/api/compliance/controls');
export const attestControl = (id, note) => apiFetch(`/api/compliance/controls/${encodeURIComponent(id)}/attest`, { method: 'POST', body: JSON.stringify({ note }) });
export const fetchComplianceMembers = () => apiFetch('/api/compliance/users');
export const suspendMember = (id, reason) => apiFetch(`/api/compliance/users/${id}/suspend`, { method: 'POST', body: JSON.stringify({ reason }) });
export const restoreMember = (id) => apiFetch(`/api/compliance/users/${id}/restore`, { method: 'POST' });
export const dsarExport = (email) => apiFetch('/api/compliance/dsar/export', { method: 'POST', body: JSON.stringify({ email }) });
export const dsarErase = (email) => apiFetch('/api/compliance/dsar/erase', { method: 'POST', body: JSON.stringify({ email, confirm: true }) });
export const fetchEvidencePack = () => apiFetch('/api/compliance/evidence-pack');

