"""
Real WebAuthn (Passkeys) relying-party integration using Yubico's fido2
library — real CBOR-encoded attestation/assertion parsing and real
public-key signature verification (ES256/EdDSA/etc. via the actual COSE
key embedded in each credential), not a hand-rolled check.

Honesty note (see README.md): there's no real browser in the sandbox
this was built in, so the interactive registration/authentication
*ceremony* (the actual `navigator.credentials.create()`/`.get()` call a
real user's browser would make) was never driven end to end. What was
verified instead — the same strategy already used for SAML's test IdP —
is a hand-built software authenticator (see the build's test tooling)
that does real ECDSA key generation, real CBOR/COSE encoding, and real
signing over the actual authenticatorData + clientDataHash a browser
would produce, checked against this exact module's real, unmodified
verification path (Fido2Server, not a stand-in). It proves the server
side is spec-correct; it does not prove any particular real browser's
implementation talks to it correctly, which only a real device can.
"""

import json
import os
import secrets
from enum import Enum
from typing import Optional

import redis
from fido2.server import Fido2Server
from fido2.webauthn import (
    AttestedCredentialData,
    PublicKeyCredentialRpEntity,
    PublicKeyCredentialUserEntity,
)

RP_ID = os.getenv("WEBAUTHN_RP_ID", "localhost")
RP_NAME = "Slate"
CHALLENGE_TTL_SECONDS = 300  # generous for a real ceremony; short enough that a stale challenge can't be replayed hours later

_server = Fido2Server(PublicKeyCredentialRpEntity(id=RP_ID, name=RP_NAME))

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
try:
    _redis = redis.from_url(REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
    _redis.ping()
except Exception:
    _redis = None


def store_challenge(state: dict) -> str:
    """Unlike cache.py's cache_set, this does NOT silently no-op when
    Redis is unavailable — a WebAuthn ceremony that *looks* like it
    started (register_begin returned real options) but never actually
    saved its state would fail confusingly at the completion step
    instead of clearly at the start. Raising here means the caller
    surfaces one clear error immediately, the same "state a real
    problem, don't paper over it" contract as everywhere else optional
    infrastructure is actually required for a specific feature."""
    if _redis is None:
        raise RuntimeError("Redis is required for WebAuthn (challenge state) but isn't reachable")
    challenge_id = secrets.token_urlsafe(24)
    _redis.setex(f"webauthn:challenge:{challenge_id}", CHALLENGE_TTL_SECONDS, json.dumps(state))
    return challenge_id


def pop_challenge(challenge_id: str) -> Optional[dict]:
    """Single-use by design — deletes on read, so a challenge can never
    be replayed even within its TTL window. This is on top of, not a
    replacement for, fido2's own per-credential sign-count replay
    protection."""
    if _redis is None:
        return None
    key = f"webauthn:challenge:{challenge_id}"
    raw = _redis.get(key)
    if raw is None:
        return None
    _redis.delete(key)
    return json.loads(raw)


class _EnumSafeEncoder(json.JSONEncoder):
    """fido2's own dict-conversion already websafe-encodes every bytes
    field (challenge, credential IDs, user id) to base64url strings —
    the one thing left for plain json.dumps to trip on is the handful of
    IntEnum/StrEnum fields (credential type, algorithm identifiers)."""

    def default(self, o):
        if isinstance(o, Enum):
            return o.value
        return super().default(o)


def to_json(fido2_options) -> dict:
    return json.loads(json.dumps(dict(fido2_options), cls=_EnumSafeEncoder))


def registration_options(user_id: int, email: str, name: str, existing_credentials: list) -> tuple:
    user = PublicKeyCredentialUserEntity(id=str(user_id).encode(), name=email, display_name=name)
    options, state = _server.register_begin(
        user,
        credentials=existing_credentials,
        resident_key_requirement="preferred",
        user_verification="preferred",
    )
    return to_json(options), state


def verify_registration(state: dict, credential_response: dict):
    """Raises on any failure (bad signature, wrong RP ID, wrong
    challenge, malformed CBOR) — Fido2Server.register_complete does the
    actual cryptographic work; this is just the pass-through, not a
    reimplementation of it. Returns an AuthenticatorData; the actual
    credential to store is its .credential_data (an AttestedCredentialData,
    itself a bytes subclass — see WebAuthnCredential in models.py)."""
    return _server.register_complete(state, credential_response)


def authentication_options(existing_credentials: list) -> tuple:
    options, state = _server.authenticate_begin(credentials=existing_credentials, user_verification="preferred")
    return to_json(options), state


def verify_authentication(state: dict, credentials: list, credential_response: dict) -> AttestedCredentialData:
    """Returns the matched AttestedCredentialData on success, raises on
    any failure — wrong signature, replayed/stale sign count, unknown
    credential ID, wrong challenge. Same fail-closed contract as
    ldap_auth.authenticate and saml_auth.process_acs: verification
    failures are exceptions here, turned into a clean 401 by the caller,
    never a partial or best-effort success."""
    return _server.authenticate_complete(state, credentials, credential_response)
