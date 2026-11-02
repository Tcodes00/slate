"""
Real LDAP authentication for enterprise sign-in — search-then-bind, the
standard secure pattern (never guess a user's DN and bind directly; bind
as a service account, search for the user, then attempt a second bind as
*them* with the password they gave us).

Built and verified against a real local OpenLDAP server during
development (see the project's build notes) — a real `ldap3` client
against real `slapd`, not a mocked directory.
"""

from dataclasses import dataclass
from typing import Optional

import ldap3
from ldap3.core.exceptions import LDAPException


@dataclass
class LdapUser:
    email: str
    name: str
    dn: str


def test_connection(server_uri: str, bind_dn: str, bind_password: str, user_search_base: str) -> tuple:
    """Real connectivity + credential check for the 'Test connection'
    button — binds as the service account and confirms the search base
    actually resolves, without authenticating any particular user."""
    try:
        server = ldap3.Server(server_uri, connect_timeout=5)
        conn = ldap3.Connection(server, user=bind_dn, password=bind_password, auto_bind=True, receive_timeout=5)
        found = conn.search(user_search_base, "(objectClass=*)", search_scope=ldap3.BASE, attributes=[])
        conn.unbind()
        if not found:
            return False, f"Bound successfully, but '{user_search_base}' doesn't exist or isn't visible to this account"
        return True, "Connected and bound successfully"
    except LDAPException as e:
        return False, f"Could not connect or bind: {e}"
    except Exception as e:
        return False, f"Unexpected error: {e}"


def authenticate(
    server_uri: str,
    bind_dn: str,
    bind_password: str,
    user_search_base: str,
    user_search_filter: str,
    email_attribute: str,
    name_attribute: str,
    username: str,
    password: str,
) -> Optional[LdapUser]:
    """Returns the matched user's email/name/DN on success, None on any
    failure (wrong password, user not found, directory unreachable) —
    deliberately one outcome type for every failure mode, so a caller
    can't accidentally branch on *why* it failed and leak whether a
    given username exists in the directory."""
    if not password:
        return None  # ldap3 (like most LDAP servers) treats an empty password as an anonymous bind, which "succeeds"

    try:
        server = ldap3.Server(server_uri, connect_timeout=5)
        service_conn = ldap3.Connection(server, user=bind_dn, password=bind_password, auto_bind=True, receive_timeout=5)

        safe_username = ldap3.utils.conv.escape_filter_chars(username)
        search_filter = user_search_filter.replace("{username}", safe_username)
        service_conn.search(user_search_base, search_filter, attributes=[email_attribute, name_attribute])

        if len(service_conn.entries) != 1:
            service_conn.unbind()
            return None  # zero or ambiguous multiple matches both fail closed

        entry = service_conn.entries[0]
        user_dn = entry.entry_dn
        email = str(entry[email_attribute]) if email_attribute in entry else None
        name = str(entry[name_attribute]) if name_attribute in entry else username
        service_conn.unbind()

        if not email:
            return None

        user_conn = ldap3.Connection(server, user=user_dn, password=password, auto_bind=True, receive_timeout=5)
        user_conn.unbind()
        return LdapUser(email=email, name=name, dn=user_dn)
    except LDAPException:
        return None
    except Exception:
        return None
