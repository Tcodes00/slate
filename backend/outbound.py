"""
The one way Slate's server calls out to an address a *tenant* controls:
workflow HTTP nodes, custom model endpoints, embedding endpoints. Anything
an org admin can type into a URL box is a way to aim this server at a
place the admin shouldn't be able to reach — a cloud metadata service,
Redis, Postgres, an internal admin panel — so every such call goes
through here instead of straight to httpx.

What it does, in order:

  1. Parses the URL: http(s) only, a host, no embedded credentials.
  2. Resolves the host *now* and checks every address it resolves to.
     Checking resolved addresses rather than the URL's text is what
     catches the encodings string checks miss — `http://2130706433/`,
     `http://0x7f.1/`, `http://[::ffff:127.0.0.1]/`, and a public-looking
     hostname whose DNS record points at 10.0.0.5.
  3. Connects to the address it just checked, not to the hostname
     (the URL is rewritten to the IP, the Host header and TLS server name
     stay the original), so DNS can't return a different answer between
     the check and the connection (DNS rebinding).
  4. Never follows redirects (a public URL that 302s to an internal one
     is the other classic bypass), caps the response size while
     streaming, and enforces a timeout.

Private/loopback/link-local addresses are refused in production by
default — ENVIRONMENT=production — because in a multi-tenant deployment
a tenant admin pointing the platform at its own network is an attack.
Self-hosted single-tenant installs that legitimately run vLLM or Ollama
on an internal address set ALLOW_PRIVATE_OUTBOUND=true; development
allows them so localhost works out of the box.

Not covered, and worth knowing: this guards the *server's* requests. It
doesn't make a malicious upstream trustworthy — whatever it answers is
still untrusted data.
"""

import asyncio
import ipaddress
import json as jsonlib
import os
import socket
from typing import Optional
from urllib.parse import urlparse, urlunparse

import httpx

DEFAULT_MAX_BYTES = 2_000_000


class OutboundError(Exception):
    """Any refusal or failure. str(e) is safe to show the admin who configured the URL.
    retryable marks the transient ones (timeout, couldn't connect) — a refusal or an
    oversized response will fail the same way every time, so retrying it is pointless."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class OutboundResponse:
    def __init__(self, status_code: int, headers: dict, content: bytes):
        self.status_code = status_code
        self.headers = headers
        self.content = content

    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        return jsonlib.loads(self.content)


def allow_private() -> bool:
    explicit = os.getenv("ALLOW_PRIVATE_OUTBOUND")
    if explicit is not None:
        return explicit.strip().lower() in ("1", "true", "yes", "on")
    return os.getenv("ENVIRONMENT", "development").lower() != "production"


def _is_internal(ip) -> bool:
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # not is_global covers private, loopback, link-local, reserved, unspecified AND
    # shared address space (100.64.0.0/10) that is_private alone misses
    return (not ip.is_global) or ip.is_multicast


def parse_url(url: str, label: str = "URL"):
    value = (url or "").strip()
    try:
        parsed = urlparse(value)
        parsed.port  # raises ValueError for an out-of-range or non-numeric port
    except ValueError:
        raise OutboundError(f"{label} isn't a valid URL")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise OutboundError(f"{label} must be a full http(s) URL")
    if parsed.username or parsed.password:
        raise OutboundError(f"{label} must not contain credentials")
    if parsed.fragment:
        raise OutboundError(f"{label} must not contain a fragment")
    return parsed


def _default_port(parsed) -> int:
    return parsed.port or (443 if parsed.scheme == "https" else 80)


async def resolve(host: str, port: int, label: str = "URL") -> list:
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror:
        raise OutboundError(f"{label}: couldn't resolve host '{host}'")
    addrs = []
    for info in infos:
        address = info[4][0].split("%")[0]  # drop an IPv6 zone id
        if address not in addrs:
            addrs.append(address)
    if not addrs:
        raise OutboundError(f"{label}: couldn't resolve host '{host}'")
    if not allow_private():
        for address in addrs:
            if _is_internal(ipaddress.ip_address(address)):
                raise OutboundError(f"{label} points at a private or internal address, which isn't allowed in this deployment")
    return addrs


async def check_url(url: str, label: str = "URL") -> str:
    """Save-time validation: same rules as a real request, without making one."""
    parsed = parse_url(url, label)
    await resolve(parsed.hostname, _default_port(parsed), label)
    return url.strip()


async def safe_request(
    method: str, url: str, *, headers: Optional[dict] = None, json=None, content=None, data=None, params=None,
    timeout: float = 15.0, max_bytes: int = DEFAULT_MAX_BYTES, label: str = "URL",
) -> OutboundResponse:
    parsed = parse_url(url, label)
    port = _default_port(parsed)
    addresses = await resolve(parsed.hostname, port, label)

    host_header = parsed.hostname if port == (443 if parsed.scheme == "https" else 80) else f"{parsed.hostname}:{port}"
    request_headers = {**(headers or {}), "Host": host_header}
    extensions = {"sni_hostname": parsed.hostname} if parsed.scheme == "https" else {}

    last_error: Optional[Exception] = None
    for address in addresses:  # the first reachable of the addresses we validated
        netloc_host = f"[{address}]" if ":" in address else address
        pinned = urlunparse(parsed._replace(netloc=f"{netloc_host}:{port}"))
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)), follow_redirects=False) as client:
                async with client.stream(
                    method.upper(), pinned, headers=request_headers, json=json, content=content, data=data,
                    params=params, extensions=extensions,
                ) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise OutboundError(f"{label}: the response was larger than {max_bytes // 1000} KB")
                    return OutboundResponse(response.status_code, {k.lower(): v for k, v in response.headers.items()}, bytes(body))
        except httpx.ConnectError as e:
            last_error = e
            continue
        except httpx.TimeoutException:
            raise OutboundError(f"{label}: the request timed out after {timeout:g}s", retryable=True)
        except httpx.HTTPError as e:
            raise OutboundError(f"{label}: the request failed ({type(e).__name__})", retryable=True)
    raise OutboundError(f"{label}: couldn't connect ({type(last_error).__name__ if last_error else 'no address'})", retryable=True)
