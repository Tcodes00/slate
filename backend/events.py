"""
Event bus for real-time updates: publish-only sync/async helpers, backed
by Redis pub/sub, plus the background listener that forwards messages to
locally-connected WebSocket clients (see ws_manager.py).

Delivery always goes out through Redis and back in through the listener
below — even in a single-process deployment — rather than a separate
in-process fast path. That keeps there being exactly one delivery path
to reason about instead of two that could double-deliver, and it means
this process behaves identically whether it's the only worker or one of
twenty behind a load balancer. This is the same fan-out pattern real
chat/notification systems use Redis pub/sub for.

If Redis isn't reachable, publishing is a no-op: the write that triggered
the event already committed to the database, so nothing is lost — the
person just won't see it appear live, and will see it on next refresh
instead. Same "degrade to slower, not broken" contract as cache.py.
"""

import asyncio
import json
import os
from datetime import datetime

import redis
import redis.asyncio as aioredis

from ws_manager import manager as ws_manager

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CHANNEL_PREFIX = "slate:events:tenant:"

try:
    _sync_client = redis.from_url(REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
    _sync_client.ping()
    REDIS_AVAILABLE = True
except Exception:
    _sync_client = None
    REDIS_AVAILABLE = False

_async_client = None  # lazily created on the event loop that ends up needing it

# Fire-and-forget asyncio.create_task() results must be held onto, or the
# task can be garbage-collected mid-run — this set is that hold.
_background_tasks: set = set()


def spawn_background(coro) -> "asyncio.Task":
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# In-process listeners called with (tenant_id, event_type, data) for every published event —
# how the workflow engine's event triggers hear about deal/ticket/invoice changes. Registered
# once at import time by the listener; failures are swallowed, same contract as publishing itself.
_listeners: list = []


def add_listener(fn) -> None:
    if fn not in _listeners:
        _listeners.append(fn)


def _notify_listeners(tenant_id: int, event_type: str, data: dict) -> None:
    for fn in list(_listeners):
        try:
            fn(tenant_id, event_type, data)
        except Exception:
            pass


def _channel(tenant_id: int) -> str:
    return f"{CHANNEL_PREFIX}{tenant_id}"


def _envelope(event_type: str, data: dict) -> str:
    return json.dumps({"type": event_type, "data": data, "ts": datetime.utcnow().isoformat()}, default=str)


def publish_event(tenant_id: int, event_type: str, data: dict) -> None:
    """Call from a sync (`def`) endpoint — e.g. after a deal's stage
    changes or a support message is added."""
    _notify_listeners(tenant_id, event_type, data)
    if not _sync_client:
        return
    try:
        _sync_client.publish(_channel(tenant_id), _envelope(event_type, data))
    except Exception:
        pass


async def publish_event_async(tenant_id: int, event_type: str, data: dict) -> None:
    """Call from an async (`async def`) context — e.g. the hand-off
    executor — so publishing never blocks the event loop."""
    global _async_client
    await asyncio.to_thread(_notify_listeners, tenant_id, event_type, data)
    if not REDIS_AVAILABLE:
        return
    try:
        if _async_client is None:
            _async_client = aioredis.from_url(REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
        await _async_client.publish(_channel(tenant_id), _envelope(event_type, data))
    except Exception:
        pass


async def redis_listener():
    """Background task, started once in main.py's lifespan. Subscribes to
    every tenant's channel and forwards each message to whichever
    WebSocket clients are connected to *this* process. Runs for the life
    of the app; cancelled cleanly on shutdown.

    Retries on any unexpected error instead of dying silently — a
    listener that permanently stops on one transient Redis hiccup would
    quietly break real-time delivery for the rest of the process's life.
    socket_timeout is deliberately left unset here: this connection's
    whole job is to block on listen() between messages, sometimes for a
    long time, which a short read timeout would (and, before this was
    caught by actually running it end-to-end, did) mistake for a dead
    connection and tear down."""
    if not REDIS_AVAILABLE:
        return
    backoff = 1
    while True:
        client = None
        try:
            client = aioredis.from_url(REDIS_URL, socket_connect_timeout=2)
            pubsub = client.pubsub()
            await pubsub.psubscribe(f"{CHANNEL_PREFIX}*")
            backoff = 1  # a clean subscribe resets the retry delay
            async for message in pubsub.listen():
                if message["type"] != "pmessage":
                    continue
                try:
                    channel = message["channel"]
                    channel = channel.decode() if isinstance(channel, bytes) else channel
                    tenant_id = int(channel[len(CHANNEL_PREFIX):])
                    payload = json.loads(message["data"])
                except Exception:
                    continue
                await ws_manager.broadcast_to_tenant(tenant_id, payload)
        except asyncio.CancelledError:
            return
        except Exception:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
        finally:
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
