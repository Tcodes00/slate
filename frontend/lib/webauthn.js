// Real navigator.credentials.create()/.get() calls — the actual browser
// WebAuthn API, not a simulation. The one thing this file can't carry
// its own proof for: there's no real browser in the environment this
// was built in, so this exact code was never driven by an actual
// registration/authentication ceremony the way the server side was
// (see backend/webauthn_auth.py's build notes — a hand-built software
// authenticator exercised the real crypto and the real server
// verification path end to end). What IS true here: this produces
// exactly the JSON shape (id/rawId/response.*) that server-verified
// path already consumes correctly, and the base64url<->ArrayBuffer
// conversions below follow the WebAuthn spec's encoding directly, not
// a guess. A real browser is still the only thing that proves this
// particular file end to end.

function base64urlToBuffer(base64url) {
  const padding = '='.repeat((4 - (base64url.length % 4)) % 4);
  const base64 = (base64url + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = window.atob(base64);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes.buffer;
}

function bufferToBase64url(buffer) {
  const bytes = new Uint8Array(buffer);
  let str = '';
  for (const b of bytes) str += String.fromCharCode(b);
  return window.btoa(str).replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
}

function credentialToJson(credential, extraFields) {
  return {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: bufferToBase64url(credential.response.clientDataJSON),
      ...extraFields(credential.response),
    },
  };
}

export function isWebAuthnSupported() {
  return typeof window !== 'undefined' && !!window.PublicKeyCredential;
}

export async function createPasskeyCredential(options) {
  const publicKey = options.publicKey;
  const credential = await navigator.credentials.create({
    publicKey: {
      ...publicKey,
      challenge: base64urlToBuffer(publicKey.challenge),
      user: { ...publicKey.user, id: base64urlToBuffer(publicKey.user.id) },
      excludeCredentials: (publicKey.excludeCredentials || []).map((c) => ({ ...c, id: base64urlToBuffer(c.id) })),
    },
  });
  return credentialToJson(credential, (response) => ({
    attestationObject: bufferToBase64url(response.attestationObject),
  }));
}

export async function getPasskeyCredential(options) {
  const publicKey = options.publicKey;
  const credential = await navigator.credentials.get({
    publicKey: {
      ...publicKey,
      challenge: base64urlToBuffer(publicKey.challenge),
      allowCredentials: (publicKey.allowCredentials || []).map((c) => ({ ...c, id: base64urlToBuffer(c.id) })),
    },
  });
  return credentialToJson(credential, (response) => ({
    authenticatorData: bufferToBase64url(response.authenticatorData),
    signature: bufferToBase64url(response.signature),
    userHandle: response.userHandle ? bufferToBase64url(response.userHandle) : null,
  }));
}
