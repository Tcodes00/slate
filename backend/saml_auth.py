"""
Real SAML 2.0 service-provider integration, built on python3-saml
(OneLogin's library — real XML-Dsig signature verification via xmlsec,
not a hand-rolled XML parser). Slate is always the Service Provider
here; the enterprise customer's own IdP (Okta, Azure AD, OneLogin, ...)
is configured per tenant, same lazy-created-config pattern as LDAP.

Verified during the build against a hand-built, independently-signed
test IdP response (see the project's build notes) — including a
tampered-assertion case that this correctly rejects, not just the happy
path.
"""

import os
from typing import Optional
from urllib.parse import urlparse

from onelogin.saml2.auth import OneLogin_Saml2_Auth

APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")


def sp_entity_id(tenant_slug: str) -> str:
    return f"{APP_BASE_URL}/api/auth/saml/metadata?tenant_slug={tenant_slug}"


def acs_url(tenant_slug: str) -> str:
    return f"{APP_BASE_URL}/api/auth/saml/acs?tenant_slug={tenant_slug}"


def build_settings(tenant_slug: str, config) -> dict:
    return {
        "strict": True,
        "sp": {
            "entityId": sp_entity_id(tenant_slug),
            "assertionConsumerService": {
                "url": acs_url(tenant_slug),
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
        },
        "idp": {
            "entityId": config.idp_entity_id,
            "singleSignOnService": {
                "url": config.idp_sso_url,
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "x509cert": config.idp_x509_cert,
        },
        "security": {
            "wantAssertionsSigned": True,
            "wantMessagesSigned": False,
            "requestedAuthnContext": False,
        },
    }


def _request_data(request_url: str, post_data: Optional[dict] = None) -> dict:
    """python3-saml expects a Flask/Django-shaped request dict; this
    adapts a plain URL (+ optional POST body) into that shape."""
    parsed = urlparse(request_url)
    return {
        "https": "on" if parsed.scheme == "https" else "off",
        "http_host": parsed.netloc,
        "server_port": str(parsed.port or (443 if parsed.scheme == "https" else 80)),
        "script_name": parsed.path,
        "get_data": {},
        "post_data": post_data or {},
    }


def get_login_redirect_url(tenant_slug: str, config, request_url: str) -> str:
    """Builds a real signed-request-capable AuthnRequest (unsigned here,
    since most IdPs don't require signed AuthnRequests over the redirect
    binding — the response signature is what actually matters) and
    returns the URL to send the browser to."""
    auth = OneLogin_Saml2_Auth(_request_data(request_url), build_settings(tenant_slug, config))
    return auth.login(return_to=None)


def get_sp_metadata(tenant_slug: str, config) -> tuple:
    """Returns (metadata_xml_bytes, errors) — an org admin pastes this
    into their IdP's 'add application' flow."""
    settings = build_settings(tenant_slug, config)
    auth = OneLogin_Saml2_Auth(_request_data(sp_entity_id(tenant_slug)), settings)
    saml_settings = auth.get_settings()
    metadata = saml_settings.get_sp_metadata()
    errors = saml_settings.validate_metadata(metadata)
    return metadata, errors


class SamlAcsResult:
    def __init__(self, success: bool, email: Optional[str] = None, name: Optional[str] = None, errors: Optional[list] = None):
        self.success = success
        self.email = email
        self.name = name
        self.errors = errors or []


def process_acs(tenant_slug: str, config, saml_response_b64: str, request_url: str) -> SamlAcsResult:
    """Validates the IdP's response — real signature verification against
    the tenant's configured idp_x509_cert, real expiry/audience/
    recipient checks, all via python3-saml/xmlsec, not reimplemented
    here. Returns success=False (never raises) on anything wrong, same
    fail-closed contract as ldap_auth.authenticate."""
    request_data = _request_data(request_url, post_data={"SAMLResponse": saml_response_b64})
    auth = OneLogin_Saml2_Auth(request_data, build_settings(tenant_slug, config))
    try:
        auth.process_response()
    except Exception as e:
        return SamlAcsResult(False, errors=[str(e)])

    errors = auth.get_errors()
    if errors or not auth.is_authenticated():
        return SamlAcsResult(False, errors=errors or ["not authenticated"])

    attributes = auth.get_attributes()
    email_values = attributes.get(config.email_attribute) or [auth.get_nameid()]
    name_values = attributes.get(config.name_attribute) or [None]
    email = email_values[0] if email_values else auth.get_nameid()
    name = name_values[0] if name_values and name_values[0] else email

    if not email:
        return SamlAcsResult(False, errors=["no email in assertion"])
    return SamlAcsResult(True, email=email, name=name)
