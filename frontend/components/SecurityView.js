'use client';

import { useEffect, useState } from 'react';
import CompliancePanel from './CompliancePanel';
import { Fingerprint, KeyRound, ShieldCheck, Server, Trash2 } from 'lucide-react';
import { fetchLdapConfig, fetchOauthConfig, fetchSamlConfig, mfaConfirm, mfaDisable, mfaSetup, testLdapConfig, updateLdapConfig, updateOauthConfig, updateSamlConfig, webauthnDeleteCredential, webauthnListCredentials, webauthnRegisterBegin, webauthnRegisterComplete } from '../lib/api';
import { createPasskeyCredential, isWebAuthnSupported } from '../lib/webauthn';
import FlagGate from './FlagGate';

const ROLE_RANK = { read_only: 0, standard_user: 1, manager: 2, org_admin: 3 };
const atLeast = (role, min) => (ROLE_RANK[role] ?? 0) >= ROLE_RANK[min];

const DEFAULTS = {
  server_uri: '', bind_dn: '', bind_password: '', user_search_base: '',
  user_search_filter: '(uid={username})', email_attribute: 'mail', name_attribute: 'cn', enabled: false,
};

function LdapPanel({ userRole }) {
  const [form, setForm] = useState(DEFAULTS);
  const [hasPassword, setHasPassword] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [error, setError] = useState('');
  const canWrite = atLeast(userRole, 'org_admin');

  useEffect(() => {
    fetchLdapConfig().then((data) => {
      setForm({ ...DEFAULTS, ...data, bind_password: '' });
      setHasPassword(data.has_bind_password);
    }).finally(() => setLoading(false));
  }, []);

  async function save(e) {
    e.preventDefault();
    setSaving(true);
    setError('');
    setTestResult(null);
    try {
      const data = await updateLdapConfig(form);
      setHasPassword(data.has_bind_password);
      setForm((f) => ({ ...f, bind_password: '' }));
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function test() {
    setTesting(true);
    setTestResult(null);
    try {
      setTestResult(await testLdapConfig());
    } catch (err) {
      setTestResult({ success: false, message: err.message });
    } finally {
      setTesting(false);
    }
  }

  if (loading) return null;

  return (
    <div className="slate-chart-card" style={{ maxWidth: 480, marginTop: 16 }}>
      <div className="slate-card-title" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <Server size={13} /> Enterprise LDAP
      </div>
      <p style={{ fontSize: 12.5, color: 'var(--ink-soft)', marginBottom: 12 }}>
        Point this at a company directory so people can sign in with their existing LDAP credentials
        instead of a separate Slate password. Search-then-bind — the service account below only ever
        looks a user up; the person's own password is checked with their own bind, never seen by this form.
      </p>

      <FlagGate flagKey="enterprise_ldap" label="Enterprise LDAP login" userRole={userRole}>
        <form onSubmit={save} className="slate-branding-form">
          <label className="slate-branding-label" htmlFor="ldap-uri">Server URI</label>
          <input id="ldap-uri" placeholder="ldap://ldap.acme.com:389" value={form.server_uri} onChange={(e) => setForm({ ...form, server_uri: e.target.value })} disabled={!canWrite} required />

          <label className="slate-branding-label" htmlFor="ldap-binddn">Service account DN</label>
          <input id="ldap-binddn" placeholder="cn=svc-slate,dc=acme,dc=com" value={form.bind_dn} onChange={(e) => setForm({ ...form, bind_dn: e.target.value })} disabled={!canWrite} required />

          <label className="slate-branding-label" htmlFor="ldap-pw">Service account password</label>
          <input id="ldap-pw" type="password" placeholder={hasPassword ? '•••••••• (saved — leave blank to keep it)' : 'required'} value={form.bind_password} onChange={(e) => setForm({ ...form, bind_password: e.target.value })} disabled={!canWrite} />

          <label className="slate-branding-label" htmlFor="ldap-base">User search base</label>
          <input id="ldap-base" placeholder="ou=people,dc=acme,dc=com" value={form.user_search_base} onChange={(e) => setForm({ ...form, user_search_base: e.target.value })} disabled={!canWrite} required />

          <label className="slate-branding-label" htmlFor="ldap-filter">User search filter</label>
          <input id="ldap-filter" value={form.user_search_filter} onChange={(e) => setForm({ ...form, user_search_filter: e.target.value })} disabled={!canWrite} />

          <div className="slate-segment-form-row" style={{ marginTop: 4 }}>
            <div style={{ flex: 1 }}>
              <label className="slate-branding-label" htmlFor="ldap-email-attr">Email attribute</label>
              <input id="ldap-email-attr" value={form.email_attribute} onChange={(e) => setForm({ ...form, email_attribute: e.target.value })} disabled={!canWrite} />
            </div>
            <div style={{ flex: 1 }}>
              <label className="slate-branding-label" htmlFor="ldap-name-attr">Name attribute</label>
              <input id="ldap-name-attr" value={form.name_attribute} onChange={(e) => setForm({ ...form, name_attribute: e.target.value })} disabled={!canWrite} />
            </div>
          </div>

          <label className="slate-segment-checkbox" style={{ marginTop: 10 }}>
            <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} disabled={!canWrite} />
            Enabled — people can sign in with LDAP at this workspace
          </label>

          {error && <div className="slate-login-error" style={{ marginTop: 8 }}>{error}</div>}
          {testResult && (
            <div className={testResult.success ? 'slate-generate-invoice-note' : 'slate-login-error'} style={{ marginTop: 8 }}>
              {testResult.message}
            </div>
          )}

          {canWrite && (
            <div className="slate-modal-row" style={{ marginTop: 12 }}>
              <button type="button" className="slate-btn slate-btn-ghost" onClick={test} disabled={testing || !hasPassword}>
                {testing ? 'Testing…' : 'Test connection'}
              </button>
              <button type="submit" className="slate-btn slate-btn-primary" disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
            </div>
          )}
        </form>
      </FlagGate>
    </div>
  );
}

function SamlPanel({ userRole }) {
  const [form, setForm] = useState({ idp_entity_id: '', idp_sso_url: '', idp_x509_cert: '', email_attribute: 'email', name_attribute: 'displayName', enabled: false });
  const [meta, setMeta] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const canWrite = atLeast(userRole, 'org_admin');

  useEffect(() => {
    fetchSamlConfig().then((data) => {
      setForm((f) => ({ ...f, ...data, idp_x509_cert: '' }));
      setMeta({ sp_entity_id: data.sp_entity_id, acs_url: data.acs_url, has_cert: data.has_cert });
    }).finally(() => setLoading(false));
  }, []);

  async function save(e) {
    e.preventDefault();
    setSaving(true);
    setError('');
    try {
      const data = await updateSamlConfig(form);
      setMeta({ sp_entity_id: data.sp_entity_id, acs_url: data.acs_url, has_cert: data.has_cert });
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  if (loading) return null;

  return (
    <div className="slate-chart-card" style={{ maxWidth: 480, marginTop: 16 }}>
      <div className="slate-card-title" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <ShieldCheck size={13} /> Enterprise SAML SSO
      </div>
      <p style={{ fontSize: 12.5, color: 'var(--ink-soft)', marginBottom: 12 }}>
        Sign in through your company's identity provider (Okta, Azure AD, OneLogin, and similar). Give your
        IdP the two values below when setting up the application there.
      </p>

      {meta && (
        <div className="slate-flag-list" style={{ marginBottom: 14 }}>
          <div className="slate-flag-row">
            <div className="slate-flag-info">
              <div className="slate-flag-label">SP Entity ID</div>
              <div className="slate-flag-desc" style={{ wordBreak: 'break-all' }}>{meta.sp_entity_id}</div>
            </div>
          </div>
          <div className="slate-flag-row">
            <div className="slate-flag-info">
              <div className="slate-flag-label">ACS URL</div>
              <div className="slate-flag-desc" style={{ wordBreak: 'break-all' }}>{meta.acs_url}</div>
            </div>
          </div>
        </div>
      )}

      <FlagGate flagKey="enterprise_saml" label="Enterprise SAML SSO" userRole={userRole}>
        <form onSubmit={save} className="slate-branding-form">
          <label className="slate-branding-label" htmlFor="saml-idp-id">IdP Entity ID</label>
          <input id="saml-idp-id" placeholder="https://idp.acme.com/metadata" value={form.idp_entity_id} onChange={(e) => setForm({ ...form, idp_entity_id: e.target.value })} disabled={!canWrite} required />

          <label className="slate-branding-label" htmlFor="saml-sso-url">IdP SSO URL</label>
          <input id="saml-sso-url" placeholder="https://idp.acme.com/sso" value={form.idp_sso_url} onChange={(e) => setForm({ ...form, idp_sso_url: e.target.value })} disabled={!canWrite} required />

          <label className="slate-branding-label" htmlFor="saml-cert">IdP signing certificate (PEM)</label>
          <textarea
            id="saml-cert" rows={4} placeholder={meta?.has_cert ? '•••• saved — paste a new cert to replace it' : '-----BEGIN CERTIFICATE-----...'}
            value={form.idp_x509_cert} onChange={(e) => setForm({ ...form, idp_x509_cert: e.target.value })} disabled={!canWrite}
            style={{ fontFamily: 'var(--font-mono)', fontSize: 11.5, border: '1px solid var(--line)', borderRadius: 8, padding: '9px 12px', resize: 'vertical' }}
          />

          <div className="slate-segment-form-row" style={{ marginTop: 4 }}>
            <div style={{ flex: 1 }}>
              <label className="slate-branding-label" htmlFor="saml-email-attr">Email attribute</label>
              <input id="saml-email-attr" value={form.email_attribute} onChange={(e) => setForm({ ...form, email_attribute: e.target.value })} disabled={!canWrite} />
            </div>
            <div style={{ flex: 1 }}>
              <label className="slate-branding-label" htmlFor="saml-name-attr">Name attribute</label>
              <input id="saml-name-attr" value={form.name_attribute} onChange={(e) => setForm({ ...form, name_attribute: e.target.value })} disabled={!canWrite} />
            </div>
          </div>

          <label className="slate-segment-checkbox" style={{ marginTop: 10 }}>
            <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} disabled={!canWrite} />
            Enabled — people can sign in with SAML at this workspace
          </label>

          {error && <div className="slate-login-error" style={{ marginTop: 8 }}>{error}</div>}

          {canWrite && (
            <div className="slate-modal-row" style={{ marginTop: 12 }}>
              <button type="submit" className="slate-btn slate-btn-primary" disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
            </div>
          )}
        </form>
      </FlagGate>
    </div>
  );
}

const OAUTH_DEFAULTS = {
  provider_name: '', client_id: '', client_secret: '', authorization_endpoint: '', token_endpoint: '',
  jwks_uri: '', issuer: '', scopes: 'openid email profile', enabled: false,
};

function OauthPanel({ userRole }) {
  const [form, setForm] = useState(OAUTH_DEFAULTS);
  const [meta, setMeta] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const canWrite = atLeast(userRole, 'org_admin');

  useEffect(() => {
    fetchOauthConfig().then((data) => {
      // The client secret is write-only: the server never sends it back, only whether one is saved.
      setForm((f) => ({ ...f, ...data, client_secret: '' }));
      setMeta({ redirect_uri: data.redirect_uri, has_client_secret: data.has_client_secret });
    }).catch(() => {}).finally(() => setLoading(false));
  }, []);

  async function save(e) {
    e.preventDefault();
    setSaving(true);
    setError('');
    setSaved(false);
    try {
      const data = await updateOauthConfig(form);
      setForm((f) => ({ ...f, client_secret: '' }));
      setMeta({ redirect_uri: data.redirect_uri, has_client_secret: data.has_client_secret });
      setSaved(true);
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  if (loading) return null;

  const field = (id, label, key, placeholder, extra = {}) => (
    <>
      <label className="slate-branding-label" htmlFor={id}>{label}</label>
      <input id={id} placeholder={placeholder} value={form[key]} onChange={(e) => setForm({ ...form, [key]: e.target.value })} disabled={!canWrite} required {...extra} />
    </>
  );

  return (
    <div className="slate-chart-card" style={{ maxWidth: 480, marginTop: 16 }}>
      <div className="slate-card-title" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <KeyRound size={13} /> Enterprise OAuth / OpenID Connect
      </div>
      <p style={{ fontSize: 12.5, color: 'var(--ink-soft)', marginBottom: 12 }}>
        Sign in through any OpenID Connect provider (Google, Microsoft Entra, Okta, Auth0, Keycloak, and similar).
        Register the redirect URI below as an allowed callback when creating the application at your provider.
      </p>

      {meta && (
        <div className="slate-flag-list" style={{ marginBottom: 14 }}>
          <div className="slate-flag-row">
            <div className="slate-flag-info">
              <div className="slate-flag-label">Redirect URI</div>
              <div className="slate-flag-desc" style={{ wordBreak: 'break-all' }}>{meta.redirect_uri}</div>
            </div>
          </div>
        </div>
      )}

      <FlagGate flagKey="enterprise_oauth" label="Enterprise OAuth / OpenID Connect login" userRole={userRole}>
        <form onSubmit={save} className="slate-branding-form">
          {field('oauth-provider-name', 'Provider name', 'provider_name', 'Google Workspace')}
          {field('oauth-client-id', 'Client ID', 'client_id', '1234567890-abc.apps.googleusercontent.com')}

          <label className="slate-branding-label" htmlFor="oauth-client-secret">Client secret</label>
          <input
            id="oauth-client-secret" type="password" autoComplete="new-password"
            placeholder={meta?.has_client_secret ? '•••• saved — enter a new secret to replace it' : 'Client secret'}
            value={form.client_secret} onChange={(e) => setForm({ ...form, client_secret: e.target.value })} disabled={!canWrite}
          />

          {field('oauth-auth-endpoint', 'Authorization endpoint', 'authorization_endpoint', 'https://accounts.google.com/o/oauth2/v2/auth')}
          {field('oauth-token-endpoint', 'Token endpoint', 'token_endpoint', 'https://oauth2.googleapis.com/token')}
          {field('oauth-jwks-uri', 'JWKS URI', 'jwks_uri', 'https://www.googleapis.com/oauth2/v3/certs')}
          {field('oauth-issuer', 'Issuer', 'issuer', 'https://accounts.google.com')}
          {field('oauth-scopes', 'Scopes', 'scopes', 'openid email profile')}

          <label className="slate-segment-checkbox" style={{ marginTop: 10 }}>
            <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} disabled={!canWrite} />
            Enabled — people can sign in with OAuth at this workspace
          </label>

          {error && <div className="slate-login-error" style={{ marginTop: 8 }}>{error}</div>}
          {saved && !error && <div style={{ marginTop: 8, fontSize: 12.5, color: 'var(--ink-soft)' }}>Saved.</div>}

          {canWrite && (
            <div className="slate-modal-row" style={{ marginTop: 12 }}>
              <button type="submit" className="slate-btn slate-btn-primary" disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
            </div>
          )}
        </form>
      </FlagGate>
    </div>
  );
}

function PasskeysPanel({ userRole }) {
  const [credentials, setCredentials] = useState([]);
  const [loading, setLoading] = useState(true);
  const [registering, setRegistering] = useState(false);
  const [error, setError] = useState('');
  const supported = isWebAuthnSupported();

  function load() {
    setLoading(true);
    webauthnListCredentials().then(setCredentials).finally(() => setLoading(false));
  }
  useEffect(load, []);

  async function register() {
    setRegistering(true);
    setError('');
    try {
      const begin = await webauthnRegisterBegin();
      const credential = await createPasskeyCredential(begin.options);
      const nickname = window.prompt('Name this passkey (e.g. "Work laptop")', '') || undefined;
      await webauthnRegisterComplete(begin.challenge_id, credential, nickname);
      load();
    } catch (err) {
      // A cancelled browser prompt (NotAllowedError) isn't a real error worth showing
      if (err.name !== 'NotAllowedError') setError(err.message || String(err));
    } finally {
      setRegistering(false);
    }
  }

  async function remove(id) {
    await webauthnDeleteCredential(id);
    load();
  }

  return (
    <div className="slate-chart-card" style={{ maxWidth: 480, marginTop: 16 }}>
      <div className="slate-card-title" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <Fingerprint size={13} /> Passkeys
      </div>
      <p style={{ fontSize: 12.5, color: 'var(--ink-soft)', marginBottom: 12 }}>
        Sign in with a fingerprint, face scan, or hardware security key instead of a password. Unlike
        LDAP/SAML, this needs no external setup — it works for your own account immediately.
      </p>

      <FlagGate flagKey="passkeys" label="Passkeys" userRole={userRole}>
        {!supported ? (
          <div className="slate-empty-note">This browser doesn't support passkeys.</div>
        ) : (
          <>
            {loading ? (
              <div className="slate-empty-note">Loading…</div>
            ) : (
              <div className="slate-flag-list" style={{ marginBottom: 12 }}>
                {credentials.map((c) => (
                  <div className="slate-flag-row" key={c.id}>
                    <div className="slate-flag-info">
                      <div className="slate-flag-label">{c.nickname || 'Unnamed passkey'}</div>
                      <div className="slate-flag-desc">
                        Added {new Date(c.created_at).toLocaleDateString()}
                        {c.last_used_at ? ` · last used ${new Date(c.last_used_at).toLocaleDateString()}` : ' · never used'}
                      </div>
                    </div>
                    <button type="button" className="slate-btn slate-btn-ghost slate-btn-sm" onClick={() => remove(c.id)}>
                      <Trash2 size={13} />
                    </button>
                  </div>
                ))}
                {credentials.length === 0 && <div className="slate-empty-note">No passkeys registered yet.</div>}
              </div>
            )}
            {error && <div className="slate-login-error" style={{ marginBottom: 10 }}>{error}</div>}
            <button type="button" className="slate-btn slate-btn-primary slate-btn-sm" onClick={register} disabled={registering}>
              {registering ? 'Follow your browser\u2019s prompt\u2026' : 'Add a passkey'}
            </button>
          </>
        )}
      </FlagGate>
    </div>
  );
}

export default function SecurityView({ user, onUserUpdate }) {
  const [setupData, setSetupData] = useState(null);
  const [code, setCode] = useState('');
  const [error, setError] = useState('');
  const [info, setInfo] = useState('');
  const [busy, setBusy] = useState(false);

  async function startSetup() {
    setError('');
    setBusy(true);
    try {
      const data = await mfaSetup();
      setSetupData(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function confirm(e) {
    e.preventDefault();
    setError('');
    setBusy(true);
    try {
      await mfaConfirm(code);
      setSetupData(null);
      setCode('');
      setInfo('MFA is now enabled on your account.');
      onUserUpdate({ ...user, mfa_enabled: true });
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function disable(e) {
    e.preventDefault();
    setError('');
    setBusy(true);
    try {
      await mfaDisable(code);
      setCode('');
      setInfo('MFA disabled.');
      onUserUpdate({ ...user, mfa_enabled: false });
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
    <div className="slate-chart-card" style={{ maxWidth: 480 }}>
      <div className="slate-card-title" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <ShieldCheck size={13} /> Two-factor authentication
      </div>

      {info && <div className="slate-generate-invoice-note" style={{ marginBottom: 10 }}>{info}</div>}
      {error && <div className="slate-login-error" style={{ marginBottom: 10 }}>{error}</div>}

      {user.mfa_enabled ? (
        <>
          <p style={{ fontSize: 12.5, color: 'var(--ink-soft)' }}>MFA is currently <strong>enabled</strong> on your account.</p>
          <form onSubmit={disable}>
            <input placeholder="Enter a current code to disable" inputMode="numeric" maxLength={6} value={code} onChange={(e) => setCode(e.target.value)}
              style={{ width: '100%', border: '1px solid var(--line)', borderRadius: 8, padding: '10px 12px', fontSize: 13, marginBottom: 8 }} required />
            <button className="slate-btn slate-btn-ghost" type="submit" disabled={busy}>Disable MFA</button>
          </form>
        </>
      ) : setupData ? (
        <>
          <p style={{ fontSize: 12.5, color: 'var(--ink-soft)' }}>
            Add this account to your authenticator app (Google Authenticator, 1Password, Authy…) using the setup key below, then enter the 6-digit code it generates.
          </p>
          <div className="num" style={{ background: 'var(--paper)', border: '1px solid var(--line)', borderRadius: 8, padding: 10, fontSize: 13, marginBottom: 10, wordBreak: 'break-all' }}>
            {setupData.secret}
          </div>
          <form onSubmit={confirm}>
            <input placeholder="123456" inputMode="numeric" maxLength={6} value={code} onChange={(e) => setCode(e.target.value)}
              style={{ width: '100%', border: '1px solid var(--line)', borderRadius: 8, padding: '10px 12px', fontSize: 13, marginBottom: 8 }} required autoFocus />
            <button className="slate-btn slate-btn-primary" type="submit" disabled={busy}>Confirm & enable</button>
          </form>
        </>
      ) : (
        <>
          <p style={{ fontSize: 12.5, color: 'var(--ink-soft)' }}>MFA is currently <strong>disabled</strong>. Enabling it adds a second step at login using a real authenticator app.</p>
          <button className="slate-btn slate-btn-primary" onClick={startSetup} disabled={busy}>Enable MFA</button>
        </>
      )}
      </div>
      <PasskeysPanel userRole={user.role} />
      <LdapPanel userRole={user.role} />
      <SamlPanel userRole={user.role} />
      <OauthPanel userRole={user.role} />
      <CompliancePanel userRole={user.role} />
    </div>
  );
}
