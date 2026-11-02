// UI behavior tests for the OAuth / OpenID Connect login option and its Security-panel, and a regression
// check that SAML's existing fragment hand-off still works beside it. Mounts the REAL components in jsdom
// against a mocked fetch — no browser exists in the build environment, so this verifies component behavior
// (what renders, what is sent, where the browser would be navigated), not real browser rendering.
//
//   cd frontend && npm install --no-save jsdom esbuild && node tests/ui/run.js
//
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import LoginScreen from '../../components/LoginScreen';
import SecurityView from '../../components/SecurityView';
import AIStudioView from '../../components/AIStudioView';
import WorkflowsPanel from '../../components/WorkflowsPanel';
import RagPanel from '../../components/RagPanel';
import CompliancePanel from '../../components/CompliancePanel';
import { FeatureFlagsProvider } from '../../components/FeatureFlagsProvider';

let passed = 0; const failures = [];
function check(label, cond, extra = '') {
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${label}${!cond && extra ? ' — ' + extra : ''}`);
  if (cond) passed += 1; else failures.push(label);
}
const loc = global.__fakeLocation;
const tick = () => act(async () => { await new Promise((r) => setTimeout(r, 15)); });
const text = (el) => el.textContent.replace(/\s+/g, ' ');
const q = (c, sel) => c.querySelector(sel);
const byText = (c, sel, re) => [...c.querySelectorAll(sel)].find((e) => re.test(e.textContent));

async function mount(node) {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => { root.render(node); });
  await tick();
  return { container, unmount: async () => { await act(async () => root.unmount()); container.remove(); } };
}
async function choose(el, value) {
  await act(async () => {
    el.value = value;
    el.dispatchEvent(new window.Event('change', { bubbles: true }));
  });
}
async function type(el, value) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(el.constructor.prototype, 'value').set.call(el, value);
    el.dispatchEvent(new window.Event('input', { bubbles: true }));
  });
}
const click = (el) => act(async () => { el.click(); });

let fetchCalls = [];
function mockFetch(routes) {
  fetchCalls = [];
  global.fetch = async (url, opts = {}) => {
    const path = String(url).replace('http://api.test', '');
    fetchCalls.push({ path, method: opts.method || 'GET', headers: opts.headers || {}, body: opts.body });
    const key = `${opts.method || 'GET'} ${path}`;
    const route = routes[key];
    const [status, body] = typeof route === 'function' ? route(opts) : route || [404, { detail: 'not mocked: ' + key }];
    return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  };
}
const USER = { id: 7, name: 'Alice', email: 'alice@example.com', role: 'standard_user', tenant: 'Acme' };

async function loginWithHash(hash, routes = {}) {
  loc.hash = hash;
  mockFetch({ 'GET /api/auth/me': [200, USER], ...routes });
  global.localStorage.clear();
  const authed = [];
  const m = await mount(<LoginScreen onAuth={(u) => authed.push(u)} />);
  await tick();
  loc.hash = '';
  return { m, authed };
}

async function main() {
  // ---------------- LoginScreen: the OAuth option ----------------
  loc.hash = ''; mockFetch({});
  let m = await mount(<LoginScreen onAuth={() => {}} />);
  check('login screen offers "company SSO (OAuth / OpenID Connect)" next to the existing SAML option',
    !!byText(m.container, 'button', /OAuth \/ OpenID Connect/) && !!byText(m.container, 'button', /company SSO \(SAML\)/));
  await click(byText(m.container, 'button', /OAuth \/ OpenID Connect/));
  check('OAuth mode: heading, workspace-slug field and "Continue to provider" button',
    /OpenID Connect provider/.test(text(m.container)) && !!q(m.container, 'input[placeholder^="Workspace slug"]') && !!byText(m.container, 'button[type=submit]', /Continue to provider/));
  const slug = q(m.container, 'input[placeholder^="Workspace slug"]');
  await type(slug, 'acme-co');
  await click(byText(m.container, 'button[type=submit]', /Continue to provider/));
  check('submitting sends the browser to GET /api/auth/oauth/login?tenant_slug=acme-co (full navigation, like SAML)',
    loc.href === 'http://api.test/api/auth/oauth/login?tenant_slug=acme-co', loc.href);
  loc.href = 'unchanged';
  await type(slug, 'a b/c');
  await click(byText(m.container, 'button[type=submit]', /Continue to provider/));
  check('the slug is URL-encoded', loc.href === 'http://api.test/api/auth/oauth/login?tenant_slug=a%20b%2Fc', loc.href);
  await m.unmount();

  // ---------------- LoginScreen: the #oauth_* handoff ----------------
  let r = await loginWithHash('#oauth_token=TOK123');
  check('#oauth_token=…: stored as the session token', global.localStorage.getItem('slate_token') === 'TOK123');
  check('…and /api/auth/me is called with that Bearer token', fetchCalls.some((c) => c.path === '/api/auth/me' && c.headers.Authorization === 'Bearer TOK123'));
  check('…and the signed-in user is handed to the app (onAuth)', r.authed.length === 1 && r.authed[0].email === 'alice@example.com');
  await r.m.unmount();

  r = await loginWithHash('#oauth_token=BAD', { 'GET /api/auth/me': [401, { detail: 'Invalid token' }] });
  check('#oauth_token that /me rejects: no half-signed-in app, an honest error instead',
    r.authed.length === 0 && /could not load your account/.test(text(r.m.container)));
  await r.m.unmount();

  r = await loginWithHash('#oauth_mfa_token=MFATOK');
  check('#oauth_mfa_token=…: switches to the authenticator-code step, no session stored, onAuth not called',
    !global.localStorage.getItem('slate_token') && r.authed.length === 0 && /Two-factor|authenticator|6-digit|code/i.test(text(r.m.container)), text(r.m.container).slice(0, 200));
  await r.m.unmount();

  r = await loginWithHash('#oauth_error=The%20ID%20token%20has%20expired');
  check('#oauth_error=…: the reason is shown on the login screen', /OAuth sign-in failed: The ID token has expired/.test(text(q(r.m.container, '.slate-login-error') || r.m.container)));
  check('…and no session is stored, no /me call made', !global.localStorage.getItem('slate_token') && !fetchCalls.some((c) => c.path === '/api/auth/me'));
  await r.m.unmount();

  r = await loginWithHash('#something=else');
  check('an unrelated fragment is ignored (no fetches, no errors)', fetchCalls.length === 0 && !q(r.m.container, '.slate-login-error'));
  await r.m.unmount();

  // ---------------- SAML regression: the existing handoff must be untouched ----------------
  r = await loginWithHash('#saml_token=SAMLTOK');
  check('SAML regression: #saml_token still signs in', global.localStorage.getItem('slate_token') === 'SAMLTOK' && r.authed.length === 1);
  await r.m.unmount();
  r = await loginWithHash('#saml_error=Bad%20assertion');
  check('SAML regression: #saml_error still shows "SAML sign-in failed: …"', /SAML sign-in failed: Bad assertion/.test(text(r.m.container)));
  await r.m.unmount();
  r = await loginWithHash('#saml_mfa_token=M');
  check('SAML regression: #saml_mfa_token still switches to the code step', r.authed.length === 0 && /code/i.test(text(r.m.container)));
  await r.m.unmount();

  // ---------------- SecurityView: the OAuth configuration panel ----------------
  const FLAGS = (oauthOn) => [200, ['enterprise_oauth', 'enterprise_saml', 'enterprise_ldap', 'passkeys'].map((key) => ({ key, enabled: key === 'enterprise_oauth' ? oauthOn : true, label: key, description: '' }))];
  const CONFIG = {
    provider_name: 'Test IdP', client_id: 'cid-123', has_client_secret: true, authorization_endpoint: 'http://idp/authorize',
    token_endpoint: 'http://idp/token', jwks_uri: 'http://idp/jwks', issuer: 'http://idp', scopes: 'openid email profile', enabled: true,
    is_configured: true, redirect_uri: 'http://api.test/api/auth/oauth/callback?tenant_slug=acme',
  };
  let puts = [];
  const routes = (over = {}) => ({
    'GET /api/feature-flags': FLAGS(true),
    'GET /api/auth/oauth-config': [200, CONFIG],
    'PUT /api/auth/oauth-config': (opts) => { puts.push(JSON.parse(opts.body)); return [200, { ...CONFIG, ...JSON.parse(opts.body), client_secret: undefined, has_client_secret: true }]; },
    'GET /api/auth/ldap-config': [200, { is_configured: false }],
    'GET /api/auth/saml-config': [200, { sp_entity_id: 'sp', acs_url: 'acs', has_cert: false }],
    'GET /api/auth/webauthn/credentials': [200, []],
    ...over,
  });
  const panel = (c) => [...c.querySelectorAll('.slate-chart-card')].find((e) => /Enterprise OAuth/.test(e.textContent));
  const view = (role) => <FeatureFlagsProvider><SecurityView user={{ id: 1, role, email: 'a@b.c', mfa_enabled: false }} onUserUpdate={() => {}} /></FeatureFlagsProvider>;

  mockFetch(routes());
  m = await mount(view('org_admin'));
  let p = panel(m.container);
  check('Security view shows the Enterprise OAuth / OpenID Connect panel after the SAML panel',
    !!p && [...m.container.querySelectorAll('.slate-card-title')].map((e) => e.textContent.trim()).findIndex((t) => /SAML/.test(t)) < [...m.container.querySelectorAll('.slate-card-title')].map((e) => e.textContent.trim()).findIndex((t) => /OAuth/.test(t)));
  check('…shows the exact redirect URI to register at the provider', text(p).includes(CONFIG.redirect_uri));
  check('…loads the saved config into the form', q(p, '#oauth-client-id').value === 'cid-123' && q(p, '#oauth-issuer').value === 'http://idp' && q(p, '#oauth-scopes').value === 'openid email profile');
  const secret = q(p, '#oauth-client-secret');
  check('…client secret is a password field, empty, with a "saved" hint — the secret is never sent to the browser', secret.type === 'password' && secret.value === '' && /saved/.test(secret.placeholder));

  await type(q(p, '#oauth-issuer'), 'http://idp/v2');
  await click(byText(p, 'button[type=submit]', /Save/));
  check('saving WITHOUT typing a secret sends client_secret "" (server keeps the stored one) and the edited fields',
    puts.length === 1 && puts[0].client_secret === '' && puts[0].issuer === 'http://idp/v2' && puts[0].enabled === true && puts[0].scopes === 'openid email profile', JSON.stringify(puts[0]));
  check('…the PUT went to PUT /api/auth/oauth-config', fetchCalls.some((c) => c.method === 'PUT' && c.path === '/api/auth/oauth-config'));
  check('…and the panel confirms "Saved."', /Saved\./.test(text(panel(m.container))));

  await type(q(panel(m.container), '#oauth-client-secret'), 'NEW-SECRET-VALUE');
  await click(byText(panel(m.container), 'button[type=submit]', /Save/));
  check('saving WITH a new secret sends it exactly once', puts.length === 2 && puts[1].client_secret === 'NEW-SECRET-VALUE');
  check('…then the secret field is cleared again (not kept in the DOM)', q(panel(m.container), '#oauth-client-secret').value === '' && !text(m.container).includes('NEW-SECRET-VALUE'));

  mockFetch(routes({ 'PUT /api/auth/oauth-config': [400, { detail: 'Scopes must include \'openid\'' }] }));
  await click(byText(panel(m.container), 'button[type=submit]', /Save/));
  check('a server-side validation error is shown in the panel', /Scopes must include 'openid'/.test(text(q(panel(m.container), '.slate-login-error') || panel(m.container))));
  await m.unmount();

  mockFetch(routes({ 'GET /api/auth/oauth-config': [403, { detail: 'Requires org_admin' }] }));
  m = await mount(view('standard_user'));
  p = panel(m.container);
  check('non-admin (config fetch 403s): panel still renders — no crash — read-only, no Save button',
    !!p && [...p.querySelectorAll('input')].every((i) => i.disabled) && !byText(p, 'button', /Save/));
  await m.unmount();

  mockFetch(routes({ 'GET /api/feature-flags': FLAGS(false) }));
  m = await mount(view('org_admin'));
  p = panel(m.container);
  check('flag OFF: the panel is gated ("turned off for this workspace" + Turn it on), no form rendered',
    /turned off for this workspace/.test(text(p)) && !!byText(p, 'button', /Turn it on/) && !q(p, '#oauth-client-id'));
  await m.unmount();

  // ---------------- AI Studio: custom & local model endpoints ----------------
  let created = []; let deleted = []; let endpointList = [{ id: 5, name: 'Acme vLLM', kind: 'chat', base_url: 'http://vllm:8000/v1', model: 'llama', has_api_key: true, auth_header: 'Authorization', enabled: true }];
  const studioRoutes = (over = {}) => ({
    'GET /api/ai-studio/settings': [200, { provider: 'local', model: 'llama3.1', available_providers: [{ key: 'local', name: 'Local model', default_model: 'llama3.1', is_configured: true }] }],
    'GET /api/ai-studio/endpoints': () => [200, endpointList],
    'POST /api/ai-studio/endpoints': (opts) => { created.push(JSON.parse(opts.body)); endpointList = [...endpointList, { id: 6, ...JSON.parse(opts.body), has_api_key: !!JSON.parse(opts.body).api_key, enabled: true }]; return [200, endpointList[endpointList.length - 1]]; },
    'POST /api/ai-studio/endpoints/5/test': [200, { ok: true, latency_ms: 42, reply: 'pong' }],
    'POST /api/ai-studio/endpoints/6/test': [200, { ok: false, error: 'HTTP 500: boom' }],
    'DELETE /api/ai-studio/endpoints/5': () => { deleted.push(5); endpointList = endpointList.filter((e) => e.id !== 5); return [200, { deleted: true }]; },
    'DELETE /api/ai-studio/endpoints/6': [409, { detail: 'This endpoint is the workspace\'s active AI provider' }],
    ...over,
  });
  mockFetch(studioRoutes());
  m = await mount(<AIStudioView userRole="org_admin" />);
  check('AI Studio (org admin): the Custom & local models panel lists saved endpoints — URL, model, "key saved" — never the key',
    /Custom & local models/.test(text(m.container)) && /Acme vLLM/.test(text(m.container)) && /key saved/.test(text(m.container)));
  await type(q(m.container, '#ep-name'), 'Gateway'); await type(q(m.container, '#ep-url'), 'http://gw:9000');
  await type(q(m.container, '#ep-model'), 'gw-model'); await type(q(m.container, '#ep-key'), 'sk-123');
  await click(byText(m.container, 'button[type=submit]', /Add endpoint/));
  check('adding an endpoint POSTs name, base URL, model and key, then the list refreshes and the provider list is re-fetched',
    created.length === 1 && created[0].name === 'Gateway' && created[0].base_url === 'http://gw:9000' && created[0].api_key === 'sk-123'
    && /Gateway/.test(text(m.container)) && fetchCalls.filter((c) => c.path === '/api/ai-studio/settings').length >= 2);
  check('…and the form (including the key) is cleared afterwards', q(m.container, '#ep-key').value === '' && q(m.container, '#ep-name').value === '');
  const testButtons = [...m.container.querySelectorAll('button')].filter((b) => b.textContent === 'Test');
  await click(testButtons[0]);
  check('Test shows success with latency', /Works — 42 ms/.test(text(m.container)));
  await click(testButtons[1]);
  check('Test shows a failure reason', /Failed — HTTP 500: boom/.test(text(m.container)));
  await click(m.container.querySelector('button[aria-label="Delete Gateway"]'));
  check('a refused delete (409: it is the active provider) shows the server\'s reason', /active AI provider/.test(text(m.container)));
  await click(m.container.querySelector('button[aria-label="Delete Acme vLLM"]'));
  check('a delete that succeeds removes the row', deleted.includes(5) && !/Acme vLLM/.test(text(q(m.container, '.slate-flag-list') || m.container)));
  await m.unmount();

  mockFetch(studioRoutes({ 'GET /api/ai-studio/endpoints': [403, { detail: 'Requires org_admin' }] }));
  m = await mount(<AIStudioView userRole="manager" />);
  check('a non-admin never sees the endpoints panel (the list is org-admin only)', !/Custom & local models/.test(text(m.container)));
  await m.unmount();

  // ---------------- Automation Studio: the workflow engine panel ----------------
  const WF_DEF = { nodes: [{ id: 'trigger', type: 'trigger', params: { kind: 'webhook' } }, { id: 'out', type: 'set', params: { values: { a: 1 } } }], edges: [{ from: 'trigger', to: 'out' }] };
  const wfSummary = { id: 1, name: 'Daily digest', description: '', trigger_type: 'webhook', enabled: false, version: 2, last_run_at: null, last_status: 'failed', next_run_at: null };
  const wfDetail = { ...wfSummary, definition: WF_DEF, trigger_config: {}, webhook_url: 'http://api.test/api/hooks/abc123' };
  let wfCalls = [];
  const wfRoutes = (over = {}) => ({
    'GET /api/feature-flags': [200, [{ key: 'workflow_engine', enabled: true, label: 'Workflow engine', description: '' }]],
    'GET /api/workflows': [200, [wfSummary]],
    'GET /api/workflows/1': [200, wfDetail],
    'GET /api/workflows/1/runs': [200, [{ id: 9, status: 'failed', trigger_type: 'manual', error: 'Node x failed: boom' }]],
    'GET /api/workflow-credentials': [200, [{ id: 3, name: 'crm-api', kind: 'bearer', allowed_hosts: ['api.example.com'] }]],
    'POST /api/workflows': (opts) => { wfCalls.push(['create', JSON.parse(opts.body)]); return [200, { ...wfDetail, id: 2, name: JSON.parse(opts.body).name, webhook_secret: 'SECRET-SHOWN-ONCE' }]; },
    'GET /api/workflows/2': [200, { ...wfDetail, id: 2, name: 'New one' }],
    'GET /api/workflows/2/runs': [200, []],
    'POST /api/workflows/validate': [200, { valid: false, errors: ['The graph contains a cycle — workflows must be acyclic'] }],
    'PUT /api/workflows/1': (opts) => { wfCalls.push(['save', JSON.parse(opts.body)]); return [200, { ...wfDetail, version: 3 }]; },
    'POST /api/workflows/1/run': (opts) => { wfCalls.push(['run', JSON.parse(opts.body)]); return [200, { id: 10, workflow_version: 2, status: 'failed', trigger_type: 'manual', error: 'Node \'b\' failed: HTTP 500', nodes: [
      { node_id: 'a', node_type: 'set', status: 'succeeded', attempts: 1, duration_ms: 3, input: {}, output: { x: 1 } }, { node_id: 'b', node_type: 'http_request', status: 'failed', attempts: 3, duration_ms: 1200, error: 'HTTP 500', input: {}, output: null },
      { node_id: 'c', node_type: 'set', status: 'skipped', attempts: 0, duration_ms: 0, error: 'Not run: an earlier step failed' }] }]; },
    'PATCH /api/workflows/1/enabled': (opts) => { wfCalls.push(['enable', JSON.parse(opts.body)]); return [400, { detail: 'This workflow uses credentials that don\'t exist: ghost' }]; },
    'POST /api/workflow-credentials': (opts) => { wfCalls.push(['cred', JSON.parse(opts.body)]); return [200, { id: 4, name: 'x', kind: 'bearer', allowed_hosts: ['h.example.com'] }]; },
    ...over,
  });
  const wfView = (role = 'manager') => <FeatureFlagsProvider><WorkflowsPanel userRole={role} /></FeatureFlagsProvider>;

  mockFetch(wfRoutes());
  m = await mount(wfView());
  check('Workflows panel lists each workflow with its trigger, enabled state and last run status', /Daily digest/.test(text(m.container)) && /webhook/.test(text(m.container)) && /Disabled/.test(text(m.container)) && /failed/.test(text(m.container)));
  check('credentials are listed with their allowed hosts — never a secret', /crm-api/.test(text(m.container)) && /api\.example\.com/.test(text(m.container)));

  await click(byText(m.container, 'button', /New workflow/));
  await type(q(m.container, '#wf-name'), 'New one');
  await choose(q(m.container, '#wf-template'), 'webhook_ai');
  await click(byText(m.container, 'button[type=submit]', /Create \(starts disabled\)/));
  const madeWf = wfCalls.find((c) => c[0] === 'create');
  check('creating from a template POSTs the name and that template\'s graph (webhook trigger → AI → notify)',
    !!madeWf && madeWf[1].name === 'New one' && madeWf[1].definition.nodes.map((n) => n.type).join() === 'trigger,ai_prompt,notify' && madeWf[1].definition.nodes[0].params.kind === 'webhook');
  check('…and the one-time webhook signing secret is shown to copy, with the webhook URL', /SECRET-SHOWN-ONCE/.test(text(m.container)) && /api\/hooks\/abc123/.test(text(m.container)));
  await m.unmount();

  mockFetch(wfRoutes());
  m = await mount(wfView());
  await click(byText(m.container, 'button', /^Open$/));
  const editor = q(m.container, '#wf-definition');
  check('Open: the editor shows the stored definition as JSON, and the version', !!editor && JSON.parse(editor.value).nodes.length === 2 && /v2/.test(text(m.container)));
  check('recent runs show their status and error', /#9/.test(text(m.container)) && /Node x failed: boom/.test(text(m.container)));
  await click(byText(m.container, 'button', /^Validate$/));
  check('Validate shows the server\'s reasons (here: a cycle)', /graph contains a cycle/.test(text(m.container)));
  await type(editor, '{ not json');
  await click(byText(m.container, 'button', /^Validate$/));
  check('invalid JSON is caught in the browser, before any request', /Not valid JSON/.test(text(m.container)));
  await type(editor, JSON.stringify(WF_DEF));
  await click(byText(m.container, 'button', /^Save$/));
  const saved = wfCalls.find((c) => c[0] === 'save');
  check('Save PUTs the parsed definition object (not a string)', !!saved && saved[1].definition.nodes[1].id === 'out' && /Saved\./.test(text(m.container)));

  await type(q(m.container, '#wf-input'), '{"name": "ada"}');
  await click(byText(m.container, 'button', /Run now/));
  const ran = wfCalls.find((c) => c[0] === 'run');
  check('Run now sends the test input and waits for the result', !!ran && ran[1].input.name === 'ada' && ran[1].wait === true);
  check('…and renders the run: status, error, and a per-node table with attempts and a skipped node explained',
    /Run #10/.test(text(m.container)) && /Node 'b' failed: HTTP 500/.test(text(m.container)) && /skipped/.test(text(m.container)) && /Not run: an earlier step failed/.test(text(m.container)) && /1200/.test(text(m.container)));
  await click(byText(m.container, 'button', /^Enable$/));
  check('a refused Enable (400, missing credential) shows the server\'s reason', /credentials that don't exist: ghost/.test(text(m.container)));

  await type(q(m.container, '#cred-name'), 'x');
  await type(q(m.container, '#cred-secret'), 'sk-live-123');
  await type(q(m.container, '#cred-hosts'), 'h.example.com, *.example.org');
  await click(byText(m.container, 'button[type=submit]', /Add credential/));
  const cred = wfCalls.find((c) => c[0] === 'cred');
  check('adding a credential sends allowed hosts as a list, and the secret field is cleared afterwards',
    !!cred && cred[1].allowed_hosts.join('|') === 'h.example.com|*.example.org' && cred[1].secret === 'sk-live-123' && q(m.container, '#cred-secret').value === '' && !/sk-live-123/.test(text(m.container)));
  await m.unmount();

  mockFetch(wfRoutes({ 'GET /api/feature-flags': [200, [{ key: 'workflow_engine', enabled: false, label: 'Workflow engine', description: '' }]] }));
  m = await mount(wfView('org_admin'));
  check('flag OFF: the panel is gated ("turned off for this workspace" + Turn it on) and shows no workflows', /turned off for this workspace/.test(text(m.container)) && !/Daily digest/.test(text(m.container)));
  await m.unmount();

  // ---------------- Knowledge Base: ask your documents (RAG) ----------------
  let ragDeleted = [];
  const ragStatus = { vector_backend: 'qdrant', embedder: 'hash-384', semantic: false, documents: 2, chunks: 9, stale_documents: 1 };
  const ragRoutes = (over = {}) => ({
    'GET /api/feature-flags': [200, [{ key: 'knowledge_rag', enabled: true, label: 'Knowledge', description: '' }]],
    'GET /api/rag/status': [200, ragStatus],
    'GET /api/rag/documents': [200, [{ id: 1, title: 'Leave policy', source_type: 'text', chunk_count: 3, min_role: 'standard_user', stale: false }, { id: 2, title: 'Handbook.pdf', source_type: 'upload', chunk_count: 6, min_role: 'manager', stale: true }]],
    'POST /api/rag/ask': [200, { answer: 'Submit a request in the HR portal [1].', model_called: true, retrieved: [], citations: [{ n: 1, title: 'Leave policy', page: 2, snippet: 'Submit a leave request in the HR portal', cited: true }] }],
    'POST /api/rag/search': [200, { results: [{ chunk_id: 5, title: 'Leave policy', page: null, text: 'Submit a leave request', score: 0.0328 }] }],
    'POST /api/rag/reindex': [200, { reindexed: 1, embedder: 'hash-384' }],
    'POST /api/rag/index/kb': [200, { indexed: 2, replaced: 0, unchanged: 1 }],
    'POST /api/rag/documents/upload': [200, { document_id: 7, status: 'indexed', chunks: 4 }],
    'DELETE /api/rag/documents/1': () => { ragDeleted.push(1); return [200, { deleted: true }]; },
    ...over,
  });
  const ragView = (role = 'manager') => <FeatureFlagsProvider><RagPanel userRole={role} /></FeatureFlagsProvider>;
  mockFetch(ragRoutes());
  m = await mount(ragView());
  check('RAG panel states plainly what the index is: counts, backend, embedder, and that a hash embedder is lexical (not semantic)',
    /2 documents · 9 passages · qdrant index · hash-384/.test(text(m.container)) && /lexical, typo-tolerant/.test(text(m.container)));
  await type(q(m.container, '#rag-question'), 'How do I request vacation days?');
  await click(byText(m.container, 'button[type=submit]', /^Ask$/));
  check('Ask shows the answer with its numbered sources: title, page, snippet, and which were actually cited',
    /Submit a request in the HR portal \[1\]\./.test(text(m.container)) && /\[1\] Leave policy, page 2/.test(text(m.container)) && /· cited/.test(text(m.container)));
  mockFetch(ragRoutes({ 'POST /api/rag/ask': [200, { answer: null, reason: 'The workspace AI provider (anthropic) is not configured on the server, so only the retrieved passages are shown', model_called: false, citations: [], retrieved: [{ chunk_id: 1, title: 'Expense policy', text: 'Receipts are required' }] }] }));
  await click(byText(m.container, 'button[type=submit]', /^Ask$/));
  check('with no AI provider configured it says so and still shows the retrieved passages (it never invents an answer)', /not configured on the server/.test(text(m.container)) && /Expense policy: Receipts are required/.test(text(m.container)));
  await click(byText(m.container, 'button', /Search passages/));
  check('Search passages lists matches with their score', /Leave policy/.test(text(m.container)) && /score 0\.0328/.test(text(m.container)));
  await click(byText(m.container, 'button', /Re-embed 1 stale/));
  check('a manager sees "Re-embed N stale" and it calls reindex, reporting the result', /Re-embedded 1 document/.test(text(m.container)) && fetchCalls.some((c) => c.path === '/api/rag/reindex' && c.method === 'POST'));
  await click(byText(m.container, 'button', /Index knowledge-base articles/));
  check('Index knowledge-base articles reports new / updated / unchanged', /2 new, 0 updated, 1 unchanged/.test(text(m.container)));
  const fileInput = q(m.container, '#rag-file');
  const file = new File(['hello'], 'policy.pdf', { type: 'application/pdf' }); // the same File/FormData pair the component code uses at runtime
  Object.defineProperty(fileInput, 'files', { value: [file], configurable: true });
  await act(async () => { fileInput.dispatchEvent(new window.Event('change', { bubbles: true })); });
  await tick();
  const uploadCall = fetchCalls.find((c) => c.path === '/api/rag/documents/upload');
  check('uploading sends multipart FormData (file in the body) and does NOT force a JSON Content-Type, so the browser can set the boundary',
    !!uploadCall && uploadCall.body instanceof FormData && uploadCall.body.get('file').name === 'policy.pdf' && !uploadCall.headers['Content-Type']);
  check('…and reports the outcome', /policy\.pdf: indexed \(4 passages\)/.test(text(m.container)));
  await click(m.container.querySelector('button[aria-label="Remove Leave policy"]'));
  check('removing a document calls DELETE', ragDeleted.includes(1));
  await m.unmount();

  mockFetch(ragRoutes());
  m = await mount(ragView('standard_user'));
  check('a standard user can ask, but has no upload, index, re-embed or delete controls',
    !!q(m.container, '#rag-question') && !q(m.container, '#rag-file') && !byText(m.container, 'button', /Index knowledge-base/) && !byText(m.container, 'button', /Re-embed/));
  await m.unmount();
  mockFetch(ragRoutes({ 'GET /api/feature-flags': [200, [{ key: 'knowledge_rag', enabled: false, label: 'Knowledge', description: '' }]] }));
  m = await mount(ragView('org_admin'));
  check('flag OFF: gated, no question box', /turned off for this workspace/.test(text(m.container)) && !q(m.container, '#rag-question'));
  await m.unmount();

  // ---------------- Security: the Compliance Center ----------------
  let compCalls = []; const downloads = [];
  global.URL.createObjectURL = (blob) => { downloads.push(blob); return 'blob:test'; };
  global.URL.revokeObjectURL = () => {};
  window.HTMLAnchorElement.prototype.click = function () { downloads.push(this.download); };
  const compRoutes = (over = {}) => ({
    'GET /api/compliance/controls': [200, { summary: { counts: { pass: 1, fail: 1, manual: 1 }, score_percent: 50 }, controls: [
      { id: 'access.mfa_admins', title: 'Every organization admin has multi-factor authentication', frameworks: ['SOC2 CC6.1', 'ISO27001 A.8.5'], status: 'fail', evidence: {} },
      { id: 'logging.audit_active', title: 'Security-relevant actions are being audit-logged', frameworks: ['SOC2 CC7.2'], status: 'pass', evidence: {} },
      { id: 'ops.backups', title: 'Backups are taken and restores are tested', frameworks: ['SOC2 A1.2'], status: 'manual', evidence: {} }] }],
    'GET /api/compliance/users': [200, [{ id: 4, name: 'Sam', email: 'sam@example.com', role: 'standard_user', suspended: false }, { id: 5, name: 'Pat', email: 'pat@example.com', role: 'manager', suspended: true, suspended_reason: 'left the company' }]],
    'POST /api/compliance/controls/ops.backups/attest': (opts) => { compCalls.push(['attest', JSON.parse(opts.body)]); return [200, { attested: true }]; },
    'POST /api/compliance/dsar/export': (opts) => { compCalls.push(['export', JSON.parse(opts.body)]); return [200, { tables: { users: [{}], crm_deals: [{}] }, other_mentions: [{}, {}, {}] }]; },
    'POST /api/compliance/dsar/erase': (opts) => { compCalls.push(['erase', JSON.parse(opts.body)]); return [200, { clean: true, scrubbed_free_text: [{ table: 'crm_deals', column: 'name', rows: 1 }], needs_review: [{}, {}] }]; },
    'POST /api/compliance/users/4/suspend': (opts) => { compCalls.push(['suspend', JSON.parse(opts.body)]); return [200, { suspended: true }]; },
    'POST /api/compliance/users/5/restore': () => { compCalls.push(['restore']); return [200, { restored: true }]; },
    'GET /api/compliance/evidence-pack': [200, { integrity_sha256: 'abc', controls: [] }],
    ...over,
  });
  mockFetch(compRoutes());
  m = await mount(<CompliancePanel userRole="org_admin" />);
  check('Compliance panel: each control shows its status and SOC 2 / ISO 27001 references, with the overall measured score — and says it is tooling, not certification',
    /multi-factor authentication/.test(text(m.container)) && /SOC2 CC6\.1 · ISO27001 A\.8\.5/.test(text(m.container)) && /50% of measured controls pass/.test(text(m.container)) && /certification is an audit/.test(text(m.container)));
  await type(q(m.container, 'input[aria-label="Attestation note for ops.backups"]'), 'Restore test passed on the 3rd, 14 minutes');
  await click(byText(m.container, 'button', /^Attest$/));
  check('only MANUAL controls offer an attestation, and it sends the note', compCalls.some((c) => c[0] === 'attest' && /Restore test/.test(c[1].note)) && m.container.querySelectorAll('input[aria-label^="Attestation note"]').length === 1);
  await type(q(m.container, '#dsar-email'), 'dana@example.com');
  await click(byText(m.container, 'button', /Export their data/));
  check('Export their data downloads the JSON and reports what it found', compCalls.some((c) => c[0] === 'export' && c[1].email === 'dana@example.com') && downloads.includes('data-export.json') && /Exported 2 tables and 3 free-text mentions/.test(text(m.container)));
  const eraseBtn = byText(m.container, 'button', /^Erase$/);
  check('Erase is DISABLED until the irreversibility box is ticked', eraseBtn.disabled === true);
  await click(q(m.container, 'input[type=checkbox]'));
  await click(byText(m.container, 'button', /^Erase$/));
  check('…then it sends confirm:true and shows the report: clean rescan, where text was rewritten, names needing a human look',
    compCalls.some((c) => c[0] === 'erase' && c[1].confirm === true) && /a rescan found no remaining mentions/.test(text(m.container)) && /crm_deals\.name \(1\)/.test(text(m.container)) && /2 place\(s\) mention their name/.test(text(m.container)));
  check('the checkbox resets after an erasure (no accidental second one)', byText(m.container, 'button', /^Erase$/).disabled === true);
  await type(q(m.container, 'input[aria-label="Reason for suspending sam@example.com"]'), 'contractor ended');
  await click(byText(m.container, 'button', /^Suspend$/));
  check('Suspend sends the reason; an already-suspended member shows why and offers Restore',
    compCalls.some((c) => c[0] === 'suspend' && c[1].reason === 'contractor ended') && /left the company/.test(text(m.container)));
  await click(byText(m.container, 'button', /^Restore$/));
  check('Restore calls the restore endpoint', compCalls.some((c) => c[0] === 'restore'));
  await click(byText(m.container, 'button', /Download evidence pack/));
  check('Download evidence pack saves the tamper-evident JSON', downloads.includes('slate-evidence-pack.json'));
  await m.unmount();
  mockFetch(compRoutes());
  m = await mount(<CompliancePanel userRole="manager" />);
  check('anyone below org admin sees nothing at all (and no compliance request is even made)', text(m.container) === '' && !fetchCalls.some((c) => c.path.startsWith('/api/compliance')));
  await m.unmount();

  console.log(`\n${passed} passed, ${failures.length} failed`);
  process.exit(failures.length ? 1 : 0);
}
main().catch((e) => { console.error(e); process.exit(1); });
