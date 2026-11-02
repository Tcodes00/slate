'use client';

import { useEffect, useState } from 'react';
import { API_URL, confirmPasswordReset, login, loginLdap, mfaVerifyLogin, oauthLoginUrl, requestPasswordReset, samlLoginUrl, setToken, signup, webauthnLoginBegin, webauthnLoginComplete } from '../lib/api';
import { getPasskeyCredential, isWebAuthnSupported } from '../lib/webauthn';

export default function LoginScreen({ onAuth }) {
  const [mode, setMode] = useState('login'); // login | signup | forgot | reset | mfa | ldap | saml | oauth | passkey
  const [form, setForm] = useState({ tenantName: '', name: '', email: 'demo@slate.dev', password: 'demo1234' });
  const [ldapForm, setLdapForm] = useState({ tenantSlug: '', username: '', password: '' });
  const [passkeyEmail, setPasskeyEmail] = useState('');
  const [samlSlug, setSamlSlug] = useState('');
  const [oauthSlug, setOauthSlug] = useState('');
  const [resetForm, setResetForm] = useState({ token: '', newPassword: '' });
  const [mfaToken, setMfaToken] = useState('');
  const [mfaCode, setMfaCode] = useState('');
  const [error, setError] = useState('');
  const [info, setInfo] = useState('');
  const [loading, setLoading] = useState(false);

  // Picks up the handoff after a SAML redirect: the backend's /saml/acs
  // sends the browser back here with the result in the URL *fragment*
  // (never sent to any server, unlike a query param) rather than a
  // regular API response, since the browser — not our own JS — was the
  // one that just navigated back from the IdP.
  useEffect(() => {
    const hash = window.location.hash;
    if (!hash) return;
    const params = new URLSearchParams(hash.slice(1));
    const samlToken = params.get('saml_token');
    const samlMfaToken = params.get('saml_mfa_token');
    const samlError = params.get('saml_error');
    if (!samlToken && !samlMfaToken && !samlError) return;

    window.history.replaceState(null, '', window.location.pathname);
    if (samlError) {
      setError(`SAML sign-in failed: ${samlError}`);
      return;
    }
    if (samlMfaToken) {
      setMfaToken(samlMfaToken);
      setMode('mfa');
      return;
    }
    setToken(samlToken);
    fetch(`${API_URL}/api/auth/me`, { headers: { Authorization: `Bearer ${samlToken}` } })
      .then((r) => r.json())
      .then((user) => onAuth(user))
      .catch(() => setError('Signed in, but could not load your account. Try refreshing.'));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The same handoff for OAuth / OpenID Connect: /api/auth/oauth/callback sends the browser back
  // here with the outcome in the URL fragment — #oauth_token=… (signed in), #oauth_mfa_token=…
  // (identity confirmed, authenticator code still needed), or #oauth_error=… (the reason, in words).
  useEffect(() => {
    const hash = window.location.hash;
    if (!hash) return;
    const params = new URLSearchParams(hash.slice(1));
    const oauthToken = params.get('oauth_token');
    const oauthMfaToken = params.get('oauth_mfa_token');
    const oauthError = params.get('oauth_error');
    if (!oauthToken && !oauthMfaToken && !oauthError) return;

    window.history.replaceState(null, '', window.location.pathname);
    if (oauthError) {
      setError(`OAuth sign-in failed: ${oauthError}`);
      return;
    }
    if (oauthMfaToken) {
      setMfaToken(oauthMfaToken);
      setMode('mfa');
      return;
    }
    setToken(oauthToken);
    fetch(`${API_URL}/api/auth/me`, { headers: { Authorization: `Bearer ${oauthToken}` } })
      .then((r) => {
        if (!r.ok) throw new Error('not signed in');
        return r.json();
      })
      .then((user) => onAuth(user))
      .catch(() => setError('Signed in, but could not load your account. Try refreshing.'));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleSubmit(e) {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      const data =
        mode === 'login'
          ? await login(form.email, form.password)
          : await signup(form.tenantName, form.name, form.email, form.password);

      if (data.mfa_required) {
        setMfaToken(data.mfa_token);
        setMode('mfa');
        return;
      }
      setToken(data.access_token);
      onAuth(data.user);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function handleMfaSubmit(e) {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      const data = await mfaVerifyLogin(mfaToken, mfaCode);
      setToken(data.access_token);
      onAuth(data.user);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function handleLdapSubmit(e) {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      const data = await loginLdap(ldapForm.tenantSlug.trim(), ldapForm.username.trim(), ldapForm.password);
      if (data.mfa_required) {
        setMfaToken(data.mfa_token);
        setMode('mfa');
        return;
      }
      setToken(data.access_token);
      onAuth(data.user);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  function handleSamlSubmit(e) {
    e.preventDefault();
    if (!samlSlug.trim()) return;
    // A full page navigation, deliberately not a fetch: the whole point
    // is the browser needs to physically leave for the IdP and come
    // back, which no XHR/fetch call can do.
    window.location.href = samlLoginUrl(samlSlug.trim());
  }

  function handleOauthSubmit(e) {
    e.preventDefault();
    if (!oauthSlug.trim()) return;
    // A full page navigation, like SAML's: the browser itself has to go to the provider and come back.
    window.location.href = oauthLoginUrl(oauthSlug.trim());
  }

  async function handlePasskeySubmit(e) {
    e.preventDefault();
    if (!passkeyEmail.trim()) return;
    setError('');
    setLoading(true);
    try {
      const begin = await webauthnLoginBegin(passkeyEmail.trim());
      const credential = await getPasskeyCredential(begin.options);
      const data = await webauthnLoginComplete(begin.challenge_id, passkeyEmail.trim(), credential);
      setToken(data.access_token);
      onAuth(data.user);
    } catch (err) {
      // A cancelled browser prompt (NotAllowedError) isn't a real error worth showing
      if (err.name !== 'NotAllowedError') setError(err.message || 'Could not sign in with that passkey');
    } finally {
      setLoading(false);
    }
  }

  async function handleRequestReset(e) {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      await requestPasswordReset(form.email);
      setInfo("If that email is registered, a reset link was sent. Dev mode: no email provider is configured, so check the backend terminal output for the token, then paste it below.");
      setMode('reset');
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function handleConfirmReset(e) {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      await confirmPasswordReset(resetForm.token, resetForm.newPassword);
      setInfo('Password updated — log in with your new password.');
      setMode('login');
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  const submitHandler = {
    login: handleSubmit, signup: handleSubmit, forgot: handleRequestReset, reset: handleConfirmReset, mfa: handleMfaSubmit, ldap: handleLdapSubmit, saml: handleSamlSubmit, oauth: handleOauthSubmit, passkey: handlePasskeySubmit,
  }[mode];

  return (
    <div className="slate-login-screen">
      <form className="slate-login-card" onSubmit={submitHandler}>
        <div className="slate-wordmark">Slate</div>
        <p className="slate-login-sub">
          {mode === 'login' && 'Log in to your workspace'}
          {mode === 'signup' && 'Create a new workspace'}
          {mode === 'forgot' && 'Reset your password'}
          {mode === 'reset' && 'Enter your reset token'}
          {mode === 'mfa' && 'Enter your 6-digit authenticator code'}
          {mode === 'ldap' && 'Sign in with your company directory'}
          {mode === 'saml' && 'Sign in with your company SSO'}
          {mode === 'oauth' && 'Sign in with your company OpenID Connect provider'}
          {mode === 'passkey' && 'Sign in with a passkey'}
        </p>

        {mode === 'signup' && (
          <>
            <input placeholder="Company name" value={form.tenantName} onChange={(e) => setForm({ ...form, tenantName: e.target.value })} required />
            <input placeholder="Your name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
          </>
        )}

        {(mode === 'login' || mode === 'signup' || mode === 'forgot') && (
          <input type="email" placeholder="Email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} required />
        )}

        {(mode === 'login' || mode === 'signup') && (
          <input type="password" placeholder="Password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required minLength={6} />
        )}

        {mode === 'reset' && (
          <>
            <input placeholder="Reset token" value={resetForm.token} onChange={(e) => setResetForm({ ...resetForm, token: e.target.value })} required />
            <input type="password" placeholder="New password" minLength={6} value={resetForm.newPassword} onChange={(e) => setResetForm({ ...resetForm, newPassword: e.target.value })} required />
          </>
        )}

        {mode === 'mfa' && (
          <input placeholder="123456" inputMode="numeric" maxLength={6} value={mfaCode} onChange={(e) => setMfaCode(e.target.value)} required autoFocus />
        )}

        {mode === 'ldap' && (
          <>
            <input placeholder="Workspace slug (e.g. acme-co)" value={ldapForm.tenantSlug} onChange={(e) => setLdapForm({ ...ldapForm, tenantSlug: e.target.value })} required />
            <input placeholder="LDAP username" value={ldapForm.username} onChange={(e) => setLdapForm({ ...ldapForm, username: e.target.value })} required autoFocus />
            <input type="password" placeholder="Password" value={ldapForm.password} onChange={(e) => setLdapForm({ ...ldapForm, password: e.target.value })} required />
          </>
        )}

        {mode === 'saml' && (
          <input placeholder="Workspace slug (e.g. acme-co)" value={samlSlug} onChange={(e) => setSamlSlug(e.target.value)} required autoFocus />
        )}

        {mode === 'oauth' && (
          <input placeholder="Workspace slug (e.g. acme-co)" value={oauthSlug} onChange={(e) => setOauthSlug(e.target.value)} required autoFocus />
        )}

        {mode === 'passkey' && (
          <input type="email" placeholder="Email" value={passkeyEmail} onChange={(e) => setPasskeyEmail(e.target.value)} required autoFocus />
        )}

        {info && <div className="slate-login-info">{info}</div>}
        {error && <div className="slate-login-error">{error}</div>}

        <button className="slate-btn slate-btn-primary" type="submit" disabled={loading}>
          {loading
            ? 'Please wait…'
            : { login: 'Log in', signup: 'Create workspace', forgot: 'Send reset link', reset: 'Update password', mfa: 'Verify', ldap: 'Sign in', saml: 'Continue to SSO', oauth: 'Continue to provider', passkey: 'Continue with passkey' }[mode]}
        </button>

        {mode === 'login' && (
          <>
            <button type="button" className="slate-login-switch" onClick={() => { setMode('signup'); setError(''); setInfo(''); }}>
              Don't have a workspace? Sign up
            </button>
            <button type="button" className="slate-login-switch" onClick={() => { setMode('forgot'); setError(''); setInfo(''); }}>
              Forgot password?
            </button>
            <button type="button" className="slate-login-switch" onClick={() => { setMode('ldap'); setError(''); setInfo(''); }}>
              Sign in with company LDAP instead
            </button>
            <button type="button" className="slate-login-switch" onClick={() => { setMode('saml'); setError(''); setInfo(''); }}>
              Sign in with company SSO (SAML) instead
            </button>
            <button type="button" className="slate-login-switch" onClick={() => { setMode('oauth'); setError(''); setInfo(''); }}>
              Sign in with company SSO (OAuth / OpenID Connect) instead
            </button>
            {isWebAuthnSupported() && (
              <button type="button" className="slate-login-switch" onClick={() => { setMode('passkey'); setError(''); setInfo(''); }}>
                Sign in with a passkey instead
              </button>
            )}
            <p className="slate-login-hint">Seeded accounts (all password demo1234): demo@slate.dev · manager@slate.dev · viewer@slate.dev</p>
          </>
        )}
        {mode !== 'login' && (
          <button type="button" className="slate-login-switch" onClick={() => { setMode('login'); setError(''); setInfo(''); }}>
            Back to login
          </button>
        )}
      </form>
    </div>
  );
}
