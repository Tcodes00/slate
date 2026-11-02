import hashlib
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

import pyotp
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fido2.webauthn import AttestedCredentialData
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import ldap_auth
import models
import oauth_auth
import saml_auth
import schemas
import webauthn_auth
from auth import create_access_token, create_mfa_pending_token, decode_mfa_pending_token, get_current_user, hash_password, require_role, verify_password
from database import get_db
from feature_flags import is_enabled
from mailer import send_email
from rate_limit import limiter
from security import decrypt_field, encrypt_field, log_audit

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

router = APIRouter(prefix="/api/auth", tags=["auth"])

VALID_ROLES = {"read_only", "standard_user", "manager", "org_admin"}
RESET_TOKEN_TTL_MINUTES = 60


def _hash_token(token: str) -> str:
    # Tokens are high-entropy random values, not user-chosen passwords, so
    # a fast hash (unlike bcrypt for passwords) is the correct tool here —
    # it's collision/lookup resistance we need, not brute-force resistance.
    return hashlib.sha256(token.encode()).hexdigest()


def _user_out(user: models.User, tenant_name: str):
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "tenant": tenant_name,
        "email_verified": user.email_verified,
    }


@router.post("/signup", response_model=schemas.TokenResponse)
@limiter.limit("5/minute")
def signup(request: Request, payload: schemas.SignupRequest, db: Session = Depends(get_db)):
    if db.query(models.User).filter(models.User.email == payload.email).first():
        raise HTTPException(400, "That email is already registered")

    slug_base = payload.tenant_name.lower().strip().replace(" ", "-") or "workspace"
    slug = slug_base
    suffix = 1
    while db.query(models.Tenant).filter(models.Tenant.slug == slug).first():
        suffix += 1
        slug = f"{slug_base}-{suffix}"

    tenant = models.Tenant(name=payload.tenant_name, slug=slug)
    db.add(tenant)
    db.flush()

    raw_token = secrets.token_urlsafe(32)
    user = models.User(
        tenant_id=tenant.id,
        email=payload.email,
        hashed_password=hash_password(payload.password),
        name=payload.name,
        role="org_admin",
        email_verified=False,
        verification_token=_hash_token(raw_token),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    send_email(
        to=user.email,
        subject="Verify your Slate account",
        body=f"Hi {user.name}, verify your email with this token: {raw_token}\n"
             f"(In the app: Settings → paste this token into 'Verify email'.)",
    )

    token = create_access_token({"sub": str(user.id)})
    return {"access_token": token, "token_type": "bearer", "user": _user_out(user, tenant.name)}


@router.post("/login", response_model=schemas.TokenResponse)
@limiter.limit("10/minute")
def login(request: Request, payload: schemas.LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == payload.email).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(401, "Incorrect email or password")
    if db.query(models.UserLock.id).filter(models.UserLock.user_id == user.id).first() is not None:
        raise HTTPException(403, "This account has been suspended — contact an organization admin")

    if user.mfa_enabled:
        return {"mfa_required": True, "mfa_token": create_mfa_pending_token(user.id)}

    tenant = db.query(models.Tenant).filter(models.Tenant.id == user.tenant_id).first()
    token = create_access_token({"sub": str(user.id)})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": _user_out(user, tenant.name if tenant else None),
    }


@router.get("/me", response_model=schemas.UserOut)
def me(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == user.tenant_id).first()
    return _user_out(user, tenant.name if tenant else None)


@router.get("/team", response_model=list[schemas.TeammateOut])
def list_team(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    teammates = db.query(models.User).filter(models.User.tenant_id == user.tenant_id).all()
    return teammates


@router.post("/invite", response_model=schemas.TeammateOut)
def invite_teammate(
    payload: schemas.InviteRequest,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("org_admin")),
):
    if payload.role not in VALID_ROLES:
        raise HTTPException(400, f"role must be one of {sorted(VALID_ROLES)}")
    if db.query(models.User).filter(models.User.email == payload.email).first():
        raise HTTPException(400, "That email is already registered")

    teammate = models.User(
        tenant_id=user.tenant_id,
        email=payload.email,
        hashed_password=hash_password(payload.password),
        name=payload.name,
        role=payload.role,
        email_verified=True,  # an Org Admin already vouched for this address
    )
    db.add(teammate)
    db.flush()
    log_audit(
        db, user.tenant_id, user, "user.invited", "User", teammate.id,
        after={"email": teammate.email, "role": teammate.role},
    )
    db.commit()
    db.refresh(teammate)
    return teammate


@router.post("/request-password-reset")
@limiter.limit("5/minute")
def request_password_reset(request: Request, payload: schemas.RequestPasswordReset, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == payload.email).first()
    # Always return the same generic response whether or not the email
    # exists — a distinct "not found" response would let anyone probe
    # which emails are registered.
    generic_response = {"message": "If that email is registered, a reset link has been sent."}

    if not user:
        return generic_response

    raw_token = secrets.token_urlsafe(32)
    user.reset_token = _hash_token(raw_token)
    user.reset_token_expires = datetime.utcnow() + timedelta(minutes=RESET_TOKEN_TTL_MINUTES)
    db.commit()

    send_email(
        to=user.email,
        subject="Reset your Slate password",
        body=f"Hi {user.name}, reset your password with this token (valid {RESET_TOKEN_TTL_MINUTES} min): {raw_token}",
    )
    return generic_response


@router.post("/reset-password")
def reset_password(payload: schemas.ConfirmPasswordReset, db: Session = Depends(get_db)):
    hashed = _hash_token(payload.token)
    user = db.query(models.User).filter(models.User.reset_token == hashed).first()

    if not user or not user.reset_token_expires or user.reset_token_expires < datetime.utcnow():
        raise HTTPException(400, "That reset link is invalid or has expired")

    if len(payload.new_password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

    user.hashed_password = hash_password(payload.new_password)
    user.reset_token = None
    user.reset_token_expires = None
    db.commit()
    return {"message": "Password updated. You can log in with your new password now."}


@router.post("/verify-email")
def verify_email(payload: schemas.VerifyEmail, db: Session = Depends(get_db)):
    hashed = _hash_token(payload.token)
    user = db.query(models.User).filter(models.User.verification_token == hashed).first()
    if not user:
        raise HTTPException(400, "That verification token is invalid")

    user.email_verified = True
    user.verification_token = None
    db.commit()
    return {"message": "Email verified."}


@router.post("/resend-verification")
def resend_verification(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    if user.email_verified:
        return {"message": "Already verified."}

    raw_token = secrets.token_urlsafe(32)
    user.verification_token = _hash_token(raw_token)
    db.commit()

    send_email(
        to=user.email,
        subject="Verify your Slate account",
        body=f"Hi {user.name}, verify your email with this token: {raw_token}",
    )
    return {"message": "Verification email resent."}


@router.post("/mfa/setup", response_model=schemas.MFASetupOut)
def mfa_setup(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Generates a real secret and provisioning URI. mfa_enabled stays
    False until /mfa/confirm proves the user actually scanned it and can
    produce valid codes — otherwise a typo'd setup could lock someone out."""
    secret = pyotp.random_base32()
    user.totp_secret = secret
    db.commit()
    uri = pyotp.totp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="Slate")
    return {"secret": secret, "provisioning_uri": uri}


@router.post("/mfa/confirm")
def mfa_confirm(payload: schemas.MFAConfirm, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    if not user.totp_secret:
        raise HTTPException(400, "Call /mfa/setup first")
    if not pyotp.TOTP(user.totp_secret).verify(payload.code, valid_window=1):
        raise HTTPException(400, "Incorrect code")
    user.mfa_enabled = True
    log_audit(db, user.tenant_id, user, "mfa.enabled", "User", user.id)
    db.commit()
    return {"message": "MFA enabled."}


@router.post("/mfa/disable")
def mfa_disable(payload: schemas.MFAConfirm, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    if not user.mfa_enabled:
        return {"message": "MFA was already disabled."}
    if not pyotp.TOTP(user.totp_secret).verify(payload.code, valid_window=1):
        raise HTTPException(400, "Incorrect code")
    user.mfa_enabled = False
    user.totp_secret = None
    log_audit(db, user.tenant_id, user, "mfa.disabled", "User", user.id)
    db.commit()
    return {"message": "MFA disabled."}


@router.post("/mfa/verify-login", response_model=schemas.TokenResponse)
@limiter.limit("10/minute")
def mfa_verify_login(request: Request, payload: schemas.MFALoginVerify, db: Session = Depends(get_db)):
    user_id = decode_mfa_pending_token(payload.mfa_token)
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user or not user.mfa_enabled or not user.totp_secret:
        raise HTTPException(401, "Invalid MFA session")
    if not pyotp.TOTP(user.totp_secret).verify(payload.code, valid_window=1):
        raise HTTPException(401, "Incorrect code")

    tenant = db.query(models.Tenant).filter(models.Tenant.id == user.tenant_id).first()
    token = create_access_token({"sub": str(user.id)})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": _user_out(user, tenant.name if tenant else None),
    }


# ---------------------------------------------------------------------------
# Enterprise LDAP: an org admin points this at their company directory
# (search-then-bind, see ldap_auth.py) instead of relying only on local
# passwords. Config is per tenant, created lazily on first save — same
# pattern as branding/plugins — and the service account's password gets
# the exact same field-level encryption already used for vendor bank
# details (security.py), not a new mechanism invented for this one field.
# ---------------------------------------------------------------------------

def _ldap_config_out(config: models.LdapConfig) -> dict:
    if config is None:
        return {
            "server_uri": "", "bind_dn": "", "has_bind_password": False, "user_search_base": "",
            "user_search_filter": "(uid={username})", "email_attribute": "mail", "name_attribute": "cn",
            "enabled": False, "is_configured": False,
        }
    return {
        "server_uri": config.server_uri, "bind_dn": config.bind_dn,
        "has_bind_password": bool(config.bind_password_encrypted),
        "user_search_base": config.user_search_base, "user_search_filter": config.user_search_filter,
        "email_attribute": config.email_attribute, "name_attribute": config.name_attribute,
        "enabled": config.enabled, "is_configured": True,
    }


@router.get("/ldap-config", response_model=schemas.LdapConfigOut)
def get_ldap_config(db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    config = db.query(models.LdapConfig).filter(models.LdapConfig.tenant_id == user.tenant_id).first()
    return _ldap_config_out(config)


@router.put("/ldap-config", response_model=schemas.LdapConfigOut)
def update_ldap_config(
    payload: schemas.LdapConfigUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("org_admin")),
):
    config = db.query(models.LdapConfig).filter(models.LdapConfig.tenant_id == user.tenant_id).first()
    if config is None:
        config = models.LdapConfig(tenant_id=user.tenant_id, bind_password_encrypted=None)
        db.add(config)

    config.server_uri = payload.server_uri
    config.bind_dn = payload.bind_dn
    if payload.bind_password:  # omit to keep the existing saved password
        config.bind_password_encrypted = encrypt_field(payload.bind_password)
    config.user_search_base = payload.user_search_base
    config.user_search_filter = payload.user_search_filter
    config.email_attribute = payload.email_attribute
    config.name_attribute = payload.name_attribute
    config.enabled = payload.enabled

    db.flush()
    log_audit(db, user.tenant_id, user, "ldap_config.updated", "ldap_config", config.id,
              after={"server_uri": config.server_uri, "enabled": config.enabled})
    db.commit()
    db.refresh(config)
    return _ldap_config_out(config)


@router.post("/ldap-config/test", response_model=schemas.LdapTestResult)
def test_ldap_config(db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    """A real connectivity check against the saved config — binds as the
    service account and confirms the search base actually resolves. Not
    a syntax check; an actual network round trip to the directory."""
    config = db.query(models.LdapConfig).filter(models.LdapConfig.tenant_id == user.tenant_id).first()
    if config is None or not config.bind_password_encrypted:
        return {"success": False, "message": "No LDAP configuration saved yet"}
    success, message = ldap_auth.test_connection(
        config.server_uri, config.bind_dn, decrypt_field(config.bind_password_encrypted), config.user_search_base,
    )
    return {"success": success, "message": message}


@router.post("/login-ldap", response_model=schemas.TokenResponse)
@limiter.limit("10/minute")
def login_ldap(request: Request, payload: schemas.LdapLoginRequest, db: Session = Depends(get_db)):
    """LDAP replaces only the password check — everything after a
    successful bind (MFA branch, token shape, response shape) is
    identical to normal login, so nothing downstream needs to know or
    care how a session started.

    Requires an explicit tenant_slug because, without subdomain or
    /w/{slug} routing (see the branding notes), there's no other way for
    the server to know which tenant's directory to check before the
    directory has confirmed who's signing in — the same honest ordering
    problem noted for pre-login branding, applied here too rather than
    guessed around."""
    tenant = db.query(models.Tenant).filter(models.Tenant.slug == payload.tenant_slug).first()
    if not tenant:
        raise HTTPException(404, "Workspace not found")
    if not is_enabled(db, tenant.id, "enterprise_ldap"):
        raise HTTPException(403, "LDAP sign-in is turned off for this workspace")

    config = db.query(models.LdapConfig).filter(models.LdapConfig.tenant_id == tenant.id).first()
    if not config or not config.enabled or not config.bind_password_encrypted:
        raise HTTPException(400, "LDAP isn't configured for this workspace")

    ldap_user = ldap_auth.authenticate(
        config.server_uri, config.bind_dn, decrypt_field(config.bind_password_encrypted),
        config.user_search_base, config.user_search_filter, config.email_attribute, config.name_attribute,
        payload.username, payload.password,
    )
    if ldap_user is None:
        raise HTTPException(401, "Incorrect username or password")

    user = db.query(models.User).filter(models.User.tenant_id == tenant.id, models.User.email == ldap_user.email).first()
    if user is None:
        # Just-in-time provisioning: a successful directory bind is real
        # proof of identity, so a first-time LDAP sign-in creates the
        # local account rather than requiring a separate invite step —
        # always as the safe default role, never anything higher.
        user = models.User(
            tenant_id=tenant.id, email=ldap_user.email, name=ldap_user.name,
            hashed_password=hash_password(secrets.token_urlsafe(32)),  # unusable local password — LDAP owns auth for this account
            role="standard_user", email_verified=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        log_audit(db, tenant.id, None, "user.provisioned_via_ldap", "User", user.id, after={"email": user.email})
        db.commit()

    if user.mfa_enabled:
        return {"mfa_required": True, "mfa_token": create_mfa_pending_token(user.id)}

    token = create_access_token({"sub": str(user.id)})
    log_audit(db, tenant.id, user, "auth.ldap_login", "User", user.id)
    db.commit()
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": _user_out(user, tenant.name),
    }


# ---------------------------------------------------------------------------
# Enterprise SAML SSO: real XML-Dsig signature verification via
# python3-saml/xmlsec (saml_auth.py), not a hand-rolled parser. Slate is
# always the Service Provider; the enterprise customer's IdP (Okta, Azure
# AD, OneLogin, ...) is configured per tenant, same lazy-created-config
# pattern as LDAP. Only the IdP's public certificate is ever stored —
# never a private key or shared secret, since the SP side of SAML never
# holds one.
# ---------------------------------------------------------------------------

def _saml_config_out(tenant_slug: str, config: models.SamlConfig) -> dict:
    if config is None:
        return {
            "idp_entity_id": "", "idp_sso_url": "", "has_cert": False, "email_attribute": "email",
            "name_attribute": "displayName", "enabled": False, "is_configured": False,
            "sp_entity_id": saml_auth.sp_entity_id(tenant_slug), "acs_url": saml_auth.acs_url(tenant_slug),
        }
    return {
        "idp_entity_id": config.idp_entity_id, "idp_sso_url": config.idp_sso_url,
        "has_cert": bool(config.idp_x509_cert), "email_attribute": config.email_attribute,
        "name_attribute": config.name_attribute, "enabled": config.enabled, "is_configured": True,
        "sp_entity_id": saml_auth.sp_entity_id(tenant_slug), "acs_url": saml_auth.acs_url(tenant_slug),
    }


@router.get("/saml-config", response_model=schemas.SamlConfigOut)
def get_saml_config(db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == user.tenant_id).first()
    config = db.query(models.SamlConfig).filter(models.SamlConfig.tenant_id == user.tenant_id).first()
    return _saml_config_out(tenant.slug, config)


@router.put("/saml-config", response_model=schemas.SamlConfigOut)
def update_saml_config(
    payload: schemas.SamlConfigUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("org_admin")),
):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == user.tenant_id).first()
    config = db.query(models.SamlConfig).filter(models.SamlConfig.tenant_id == user.tenant_id).first()
    if config is None:
        config = models.SamlConfig(tenant_id=user.tenant_id)
        db.add(config)

    config.idp_entity_id = payload.idp_entity_id
    config.idp_sso_url = payload.idp_sso_url
    config.idp_x509_cert = payload.idp_x509_cert
    config.email_attribute = payload.email_attribute
    config.name_attribute = payload.name_attribute
    config.enabled = payload.enabled

    # Real validation, not just "did the fields get saved". Two checks:
    # the cert is parsed as an actual X.509 certificate (get_sp_metadata
    # below validates the SP's *own* metadata structure, which never
    # touches the IdP cert field at all — checked directly here instead,
    # after this gap showed up as a real failure during the build's own
    # verification, not assumed sufficient), and SP metadata generation
    # against this config succeeds, catching a malformed SSO URL too.
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        cert_bytes = config.idp_x509_cert.encode()
        if "-----BEGIN CERTIFICATE-----" not in config.idp_x509_cert:
            cert_bytes = b"-----BEGIN CERTIFICATE-----\n" + config.idp_x509_cert.encode() + b"\n-----END CERTIFICATE-----\n"
        x509.load_pem_x509_certificate(cert_bytes, default_backend())
    except Exception as e:
        raise HTTPException(400, f"'{config.idp_x509_cert[:40]}...' isn't a valid X.509 certificate: {e}")

    try:
        _metadata, errors = saml_auth.get_sp_metadata(tenant.slug, config)
        if errors:
            raise HTTPException(400, f"Invalid SAML configuration: {errors}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Invalid SAML configuration: {e}")

    db.flush()
    log_audit(db, user.tenant_id, user, "saml_config.updated", "saml_config", config.id,
              after={"idp_entity_id": config.idp_entity_id, "enabled": config.enabled})
    db.commit()
    db.refresh(config)
    return _saml_config_out(tenant.slug, config)


@router.get("/saml/metadata")
def saml_metadata(tenant_slug: str, db: Session = Depends(get_db)):
    """Public and unauthenticated on purpose — this is what an org admin
    pastes into their IdP's 'add application' setup screen, before
    anyone has signed in yet."""
    tenant = db.query(models.Tenant).filter(models.Tenant.slug == tenant_slug).first()
    if not tenant:
        raise HTTPException(404, "Workspace not found")
    config = db.query(models.SamlConfig).filter(models.SamlConfig.tenant_id == tenant.id).first()
    if not config:
        config = models.SamlConfig(idp_entity_id="", idp_sso_url="", idp_x509_cert="")
    metadata, errors = saml_auth.get_sp_metadata(tenant_slug, config)
    if errors:
        raise HTTPException(500, f"Could not generate metadata: {errors}")
    return Response(content=metadata, media_type="application/xml")


@router.get("/saml/login")
def saml_login(tenant_slug: str, request: Request, db: Session = Depends(get_db)):
    tenant = db.query(models.Tenant).filter(models.Tenant.slug == tenant_slug).first()
    if not tenant:
        raise HTTPException(404, "Workspace not found")
    if not is_enabled(db, tenant.id, "enterprise_saml"):
        raise HTTPException(403, "SAML sign-in is turned off for this workspace")
    config = db.query(models.SamlConfig).filter(models.SamlConfig.tenant_id == tenant.id).first()
    if not config or not config.enabled:
        raise HTTPException(400, "SAML isn't configured for this workspace")

    redirect_url = saml_auth.get_login_redirect_url(tenant_slug, config, str(request.url))
    return RedirectResponse(redirect_url)


@router.post("/saml/acs")
async def saml_acs(tenant_slug: str, request: Request, db: Session = Depends(get_db), SAMLResponse: str = Form(...)):
    """The IdP's browser-mediated redirect target after a person
    authenticates there. Real signature verification happens inside
    saml_auth.process_acs; this endpoint's job is just to turn a
    verified assertion into the same kind of session every other login
    method produces, then hand the browser back to the frontend with it.

    Redirects with the token in the URL *fragment* (`#...`), not a query
    param — fragments never reach the server on the next request and
    aren't sent in the Referer header, which is the standard mitigation
    for this exact "hand a token back after a cross-origin redirect"
    problem when there's no shared cookie domain between frontend and
    backend to lean on instead."""
    tenant = db.query(models.Tenant).filter(models.Tenant.slug == tenant_slug).first()
    if not tenant:
        raise HTTPException(404, "Workspace not found")
    if not is_enabled(db, tenant.id, "enterprise_saml"):
        raise HTTPException(403, "SAML sign-in is turned off for this workspace")
    config = db.query(models.SamlConfig).filter(models.SamlConfig.tenant_id == tenant.id).first()
    if not config or not config.enabled:
        raise HTTPException(400, "SAML isn't configured for this workspace")

    result = saml_auth.process_acs(tenant_slug, config, SAMLResponse, str(request.url))
    if not result.success:
        return RedirectResponse(f"{FRONTEND_URL}/?saml_error=" + "+".join(result.errors[:1]) or "unknown_error")

    user = db.query(models.User).filter(models.User.tenant_id == tenant.id, models.User.email == result.email).first()
    if user is None:
        user = models.User(
            tenant_id=tenant.id, email=result.email, name=result.name or result.email,
            hashed_password=hash_password(secrets.token_urlsafe(32)),  # unusable local password — SAML owns auth for this account
            role="standard_user", email_verified=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        log_audit(db, tenant.id, None, "user.provisioned_via_saml", "User", user.id, after={"email": user.email})
        db.commit()

    if user.mfa_enabled:
        return RedirectResponse(f"{FRONTEND_URL}/#saml_mfa_token={create_mfa_pending_token(user.id)}")

    token = create_access_token({"sub": str(user.id)})
    log_audit(db, tenant.id, user, "auth.saml_login", "User", user.id)
    db.commit()
    return RedirectResponse(f"{FRONTEND_URL}/#saml_token={token}")


# ---------------------------------------------------------------------------
# Passkeys (WebAuthn). Real CBOR/COSE attestation and assertion
# verification via Yubico's fido2 library (backend/webauthn_auth.py), not
# a hand-rolled check. Unlike LDAP/SAML there's no external directory or
# IdP to configure — registration and login both work immediately once
# the flag is on, which is why passkeys defaults to enabled rather than
# requiring setup first.
# ---------------------------------------------------------------------------

def _user_credentials(db: Session, user_id: int) -> list:
    rows = db.query(models.WebAuthnCredential).filter(models.WebAuthnCredential.user_id == user_id).all()
    return [AttestedCredentialData(bytes(r.credential_data)) for r in rows]


@router.post("/webauthn/register/begin")
def webauthn_register_begin(
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("standard_user")),
):
    if not is_enabled(db, user.tenant_id, "passkeys"):
        raise HTTPException(403, "Passkeys are turned off for this workspace")
    existing = _user_credentials(db, user.id)
    options, state = webauthn_auth.registration_options(user.id, user.email, user.name, existing)
    challenge_id = webauthn_auth.store_challenge(state)
    return {"options": options, "challenge_id": challenge_id}


@router.post("/webauthn/register/complete", response_model=schemas.WebAuthnCredentialOut)
def webauthn_register_complete(
    payload: schemas.WebAuthnRegisterCompleteRequest,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("standard_user")),
):
    state = webauthn_auth.pop_challenge(payload.challenge_id)
    if state is None:
        raise HTTPException(400, "This registration attempt has expired — start again")

    try:
        auth_data = webauthn_auth.verify_registration(state, payload.credential)
    except Exception as e:
        raise HTTPException(400, f"Could not verify this passkey: {e}")

    row = models.WebAuthnCredential(
        tenant_id=user.tenant_id, user_id=user.id,
        credential_data=bytes(auth_data.credential_data), nickname=payload.nickname,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    log_audit(db, user.tenant_id, user, "webauthn.registered", "User", user.id, after={"nickname": payload.nickname})
    db.commit()
    return row


@router.get("/webauthn/credentials", response_model=list[schemas.WebAuthnCredentialOut])
def list_webauthn_credentials(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return db.query(models.WebAuthnCredential).filter(models.WebAuthnCredential.user_id == user.id).all()


@router.delete("/webauthn/credentials/{credential_id}")
def delete_webauthn_credential(credential_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    row = db.query(models.WebAuthnCredential).filter(
        models.WebAuthnCredential.id == credential_id, models.WebAuthnCredential.user_id == user.id
    ).first()
    if not row:
        raise HTTPException(404, "Passkey not found")
    db.delete(row)
    db.commit()
    return {"message": "Passkey removed"}


@router.post("/webauthn/login/begin")
@limiter.limit("15/minute")
def webauthn_login_begin(request: Request, payload: schemas.WebAuthnLoginBeginRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == payload.email).first()
    # Same posture as request_password_reset above: an unknown email or
    # one with zero registered passkeys both produce a real, well-formed
    # challenge with an empty allowCredentials list — indistinguishable
    # from each other, so this endpoint can't be used to probe which
    # emails have an account.
    existing = _user_credentials(db, user.id) if user else []
    options, state = webauthn_auth.authentication_options(existing)
    challenge_id = webauthn_auth.store_challenge(state)
    return {"options": options, "challenge_id": challenge_id}


@router.post("/webauthn/login/complete", response_model=schemas.TokenResponse)
@limiter.limit("15/minute")
def webauthn_login_complete(request: Request, payload: schemas.WebAuthnLoginCompleteRequest, db: Session = Depends(get_db)):
    state = webauthn_auth.pop_challenge(payload.challenge_id)
    if state is None:
        raise HTTPException(400, "This sign-in attempt has expired — start again")

    user = db.query(models.User).filter(models.User.email == payload.email).first()
    if not user or not is_enabled(db, user.tenant_id, "passkeys"):
        raise HTTPException(401, "Incorrect email or no passkey registered")

    rows = db.query(models.WebAuthnCredential).filter(models.WebAuthnCredential.user_id == user.id).all()
    credentials = [AttestedCredentialData(bytes(r.credential_data)) for r in rows]

    try:
        matched = webauthn_auth.verify_authentication(state, credentials, payload.credential)
    except Exception:
        raise HTTPException(401, "Incorrect email or no passkey registered")

    matched_row = next((r for r in rows if bytes(r.credential_data) == bytes(matched)), None)
    if matched_row:
        matched_row.last_used_at = datetime.utcnow()

    tenant = db.query(models.Tenant).filter(models.Tenant.id == user.tenant_id).first()
    token = create_access_token({"sub": str(user.id)})
    log_audit(db, user.tenant_id, user, "auth.webauthn_login", "User", user.id)
    db.commit()
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": _user_out(user, tenant.name if tenant else None),
    }


# ---------------------------------------------------------------------------
# Enterprise OAuth 2.0 / OpenID Connect: the authorization-code flow with
# Slate as the client (oauth_auth.py). An org admin points a tenant at
# their provider's endpoints and client credentials — config is per
# tenant, created lazily on first save, same pattern as LDAP/SAML — and
# the client secret gets the same field-level encryption as the LDAP
# service account password. Everything after a verified ID token is
# identical to every other login method: same just-in-time provisioning
# (always standard_user), same MFA branch, same token, same fragment
# hand-off back to the frontend as SAML's.
# ---------------------------------------------------------------------------

def _oauth_config_out(tenant_slug: str, config: Optional[models.OAuthConfig]) -> dict:
    redirect_uri = oauth_auth.redirect_uri(tenant_slug)
    if config is None:
        return {
            "provider_name": "", "client_id": "", "has_client_secret": False, "authorization_endpoint": "",
            "token_endpoint": "", "jwks_uri": "", "issuer": "", "scopes": "openid email profile",
            "enabled": False, "is_configured": False, "redirect_uri": redirect_uri,
        }
    return {
        "provider_name": config.provider_name, "client_id": config.client_id,
        "has_client_secret": bool(config.client_secret_encrypted),
        "authorization_endpoint": config.authorization_endpoint, "token_endpoint": config.token_endpoint,
        "jwks_uri": config.jwks_uri, "issuer": config.issuer, "scopes": config.scopes,
        "enabled": config.enabled, "is_configured": True, "redirect_uri": redirect_uri,
    }


@router.get("/oauth-config", response_model=schemas.OAuthConfigOut)
def get_oauth_config(db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == user.tenant_id).first()
    config = db.query(models.OAuthConfig).filter(models.OAuthConfig.tenant_id == user.tenant_id).first()
    return _oauth_config_out(tenant.slug, config)


@router.put("/oauth-config", response_model=schemas.OAuthConfigOut)
def update_oauth_config(
    payload: schemas.OAuthConfigUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("org_admin")),
):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == user.tenant_id).first()
    config = db.query(models.OAuthConfig).filter(models.OAuthConfig.tenant_id == user.tenant_id).first()

    # Real validation at save time, not just "did the fields get saved" —
    # same posture as the SAML cert check. The two endpoints this server
    # itself will call (token_endpoint, jwks_uri) get the SSRF checks.
    try:
        provider_name = payload.provider_name.strip()
        client_id = payload.client_id.strip()
        if not provider_name or not client_id:
            raise ValueError("Provider name and client ID are required")
        authorization_endpoint = oauth_auth.validate_endpoint_url("Authorization endpoint", payload.authorization_endpoint, server_fetched=False)
        token_endpoint = oauth_auth.validate_endpoint_url("Token endpoint", payload.token_endpoint)
        jwks_uri = oauth_auth.validate_endpoint_url("JWKS URI", payload.jwks_uri)
        issuer = oauth_auth.validate_endpoint_url("Issuer", payload.issuer, server_fetched=False)
        scopes = oauth_auth.normalize_scopes(payload.scopes)
    except ValueError as e:
        raise HTTPException(400, str(e))

    has_secret = bool(payload.client_secret) or bool(config and config.client_secret_encrypted)
    if payload.enabled and not has_secret:
        raise HTTPException(400, "A client secret is required before OAuth sign-in can be enabled")

    if config is None:
        config = models.OAuthConfig(tenant_id=user.tenant_id, client_secret_encrypted=None)
        db.add(config)

    config.provider_name = provider_name
    config.client_id = client_id
    if payload.client_secret:  # omit to keep the existing saved secret
        config.client_secret_encrypted = encrypt_field(payload.client_secret)
    config.authorization_endpoint = authorization_endpoint
    config.token_endpoint = token_endpoint
    config.jwks_uri = jwks_uri
    config.issuer = issuer
    config.scopes = scopes
    config.enabled = payload.enabled

    db.flush()
    log_audit(db, user.tenant_id, user, "oauth_config.updated", "oauth_config", config.id,
              after={"provider_name": config.provider_name, "issuer": config.issuer, "enabled": config.enabled})
    db.commit()
    db.refresh(config)
    return _oauth_config_out(tenant.slug, config)


def _oauth_tenant_and_config(db: Session, tenant_slug: str):
    """Same three gates as SAML's login/ACS: workspace exists, flag is on,
    config is present and enabled — plus a saved client secret, the OAuth
    equivalent of LDAP's saved bind password."""
    tenant = db.query(models.Tenant).filter(models.Tenant.slug == tenant_slug).first()
    if not tenant:
        raise HTTPException(404, "Workspace not found")
    if not is_enabled(db, tenant.id, "enterprise_oauth"):
        raise HTTPException(403, "OAuth sign-in is turned off for this workspace")
    config = db.query(models.OAuthConfig).filter(models.OAuthConfig.tenant_id == tenant.id).first()
    if not config or not config.enabled or not config.client_secret_encrypted:
        raise HTTPException(400, "OAuth isn't configured for this workspace")
    return tenant, config


def _oauth_finish(url: str, cookie_name: Optional[str] = None) -> RedirectResponse:
    """Every callback outcome goes through here: the Location header can
    carry a session token (in the fragment), so it's marked no-store, and
    this attempt's one-shot browser-binding cookie is cleared whatever
    happened — only this attempt's, never another tab's."""
    response = RedirectResponse(url, status_code=302)
    response.headers["Cache-Control"] = "no-store"
    if cookie_name:
        response.delete_cookie(cookie_name, path=oauth_auth.BINDING_COOKIE_PATH)
    return response


def _oauth_fail(message: str, cookie_name: Optional[str] = None) -> RedirectResponse:
    # In the fragment, not the query string — that's the one place the
    # login screen looks (see LoginScreen.js), same as the token itself.
    return _oauth_finish(f"{FRONTEND_URL}/#oauth_error={quote(message, safe='')}", cookie_name)


@router.get("/oauth/login")
@limiter.limit("60/minute")
def oauth_login(tenant_slug: str, request: Request, db: Session = Depends(get_db)):
    tenant, config = _oauth_tenant_and_config(db, tenant_slug)

    flow = oauth_auth.new_flow_secrets()
    try:
        state = oauth_auth.store_state({
            "tenant_slug": tenant.slug, "nonce": flow["nonce"],
            "code_verifier": flow["code_verifier"], "binding_hash": flow["binding_hash"],
        })
    except RuntimeError as e:
        # Fail fast and clearly, same contract as the passkey endpoints —
        # but a 503 with the reason, not an unhandled 500.
        raise HTTPException(503, str(e))

    response = RedirectResponse(
        oauth_auth.build_authorization_url(config, tenant.slug, state, flow["nonce"], flow["code_challenge"]),
        status_code=302,
    )
    response.headers["Cache-Control"] = "no-store"
    # Ties this attempt to the browser that started it: without it, anyone
    # could start their own flow, sign in at the provider as themselves,
    # and hand the resulting callback URL to a victim — logging the victim
    # into the attacker's account. SameSite=Lax is deliberate: the
    # provider's redirect back is a cross-site top-level GET, which Lax
    # cookies are sent on and Strict ones are not.
    response.set_cookie(
        oauth_auth.binding_cookie_name(state), flow["binding"], max_age=oauth_auth.STATE_TTL_SECONDS,
        path=oauth_auth.BINDING_COOKIE_PATH, httponly=True, samesite="lax",
        secure=oauth_auth.APP_BASE_URL.startswith("https://"),
    )
    return response


def _oauth_find_or_provision(db: Session, tenant: models.Tenant, identity: oauth_auth.OAuthIdentity):
    """Returns (user, newly_provisioned); user is None only when the email
    already belongs to another workspace. users.email is globally unique,
    and silently signing someone into a different tenant's account — or
    crashing on the unique constraint, which is what a plain insert would
    do — are both worse than saying so. Matching ignores case: providers
    don't agree on how they capitalize an address."""
    email = identity.email.lower()
    user = db.query(models.User).filter(
        models.User.tenant_id == tenant.id, func.lower(models.User.email) == email
    ).first()
    if user is not None:
        return user, False
    if db.query(models.User).filter(func.lower(models.User.email) == email).first() is not None:
        return None, False

    # Just-in-time provisioning, same rule as LDAP and SAML: a verified
    # ID token is real proof of identity, so a first-time sign-in creates
    # the local account — always as the safe default role, never higher.
    user = models.User(
        tenant_id=tenant.id, email=email, name=identity.name,
        hashed_password=hash_password(secrets.token_urlsafe(32)),  # unusable local password — OAuth owns auth for this account
        role="standard_user", email_verified=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Two first-time sign-ins for the same person racing each other:
        # the other one won, so use its row.
        db.rollback()
        user = db.query(models.User).filter(
            models.User.tenant_id == tenant.id, func.lower(models.User.email) == email
        ).first()
        return user, False
    db.refresh(user)
    log_audit(db, tenant.id, None, "user.provisioned_via_oauth", "User", user.id, after={"email": user.email})
    db.commit()
    return user, True


@router.get("/oauth/callback")
@limiter.limit("60/minute")
def oauth_callback(
    tenant_slug: str,
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Where the provider sends the browser back. Order matters:

      1. State is consumed *first* — before the provider's own `error`,
         before the code is looked at — so a state can never be used
         twice, whatever the first attempt did or didn't accomplish.
      2. The state must belong to this workspace and to this browser.
      3. Only then is the code exchanged and the ID token verified.

    Failures after the workspace/flag/config gates redirect back to the
    login screen with #oauth_error=..., because the person is sitting in
    a browser tab that just came back from their provider — a bare JSON
    error page there would be a dead end. Like SAML's ACS, success hands
    the token back in the URL *fragment*, never the query string."""
    tenant, config = _oauth_tenant_and_config(db, tenant_slug)

    cookie_name = oauth_auth.binding_cookie_name(state)
    if not state:
        return _oauth_fail("The sign-in response was missing its state — start again from the login page")
    try:
        flow = oauth_auth.pop_state(state)
    except RuntimeError as e:
        return _oauth_fail(f"{e}. Try again shortly", cookie_name)
    if flow is None:
        return _oauth_fail("This sign-in link has expired or was already used — start again from the login page", cookie_name)
    if flow.get("tenant_slug") != tenant.slug:
        return _oauth_fail("This sign-in attempt was started for a different workspace — start again from the login page", cookie_name)
    if not oauth_auth.binding_matches(request.cookies.get(cookie_name) if cookie_name else None, flow.get("binding_hash", "")):
        return _oauth_fail("This sign-in attempt wasn't started in this browser — start again from the login page", cookie_name)

    if error:
        provider_error = error if oauth_auth._PROVIDER_ERROR_RE.match(error) else "unknown_error"
        return _oauth_fail(f"{config.provider_name} didn't complete the sign-in ({provider_error})", cookie_name)
    if not code:
        return _oauth_fail("The provider's response didn't include a sign-in code — start again from the login page", cookie_name)

    try:
        client_secret = decrypt_field(config.client_secret_encrypted)
    except InvalidToken:
        return _oauth_fail("The saved client secret can't be decrypted — an org admin needs to re-enter it in Security settings", cookie_name)
    try:
        identity = oauth_auth.complete_login(config, client_secret, tenant.slug, code, flow)
    except oauth_auth.OAuthError as e:
        return _oauth_fail(str(e), cookie_name)
    except Exception:
        oauth_auth.logger.exception("unexpected failure completing OAuth sign-in for tenant %s", tenant.slug)
        return _oauth_fail("Sign-in failed unexpectedly — try again, and tell an org admin if it keeps happening", cookie_name)

    user, _created = _oauth_find_or_provision(db, tenant, identity)
    if user is None:
        return _oauth_fail("An account with this email address already exists in a different workspace", cookie_name)

    if user.mfa_enabled:
        return _oauth_finish(f"{FRONTEND_URL}/#oauth_mfa_token={create_mfa_pending_token(user.id)}", cookie_name)

    token = create_access_token({"sub": str(user.id)})
    log_audit(db, tenant.id, user, "auth.oauth_login", "User", user.id)
    db.commit()
    return _oauth_finish(f"{FRONTEND_URL}/#oauth_token={token}", cookie_name)
