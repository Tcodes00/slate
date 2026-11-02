"""
Cache-aside helper backed by real Redis. If Redis isn't reachable, every
call falls through to a cache miss rather than raising — the app should
degrade to "slower" not "broken" when a cache is unavailable, which is
the correct behavior for a cache in any real system.
"""

import json
import os

import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DEFAULT_TTL_SECONDS = 30

try:
    _client = redis.from_url(REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
    _client.ping()
    REDIS_AVAILABLE = True
except Exception:
    _client = None
    REDIS_AVAILABLE = False


def cache_get(key: str):
    if not _client:
        return None
    try:
        raw = _client.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def cache_set(key: str, value, ttl: int = DEFAULT_TTL_SECONDS) -> None:
    if not _client:
        return
    try:
        _client.setex(key, ttl, json.dumps(value, default=str))
    except Exception:
        pass


def cache_invalidate(prefix: str) -> None:
    """Called from write endpoints (e.g. a deal's stage changes) so the
    next analytics read is never stale by more than one write."""
    if not _client:
        return
    try:
        for key in _client.scan_iter(f"{prefix}*"):
            _client.delete(key)
    except Exception:
        pass
