"""
In-memory registry of connected WebSocket clients, per tenant. This is
process-local — see events.py for how a message published from any
worker or process reaches every worker's locally-connected clients via
Redis pub/sub, rather than just the one that handled the originating
HTTP request.
"""

import asyncio
from typing import Dict, Optional, Set

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        self._by_tenant: Dict[int, Set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, tenant_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._by_tenant.setdefault(tenant_id, set()).add(websocket)

    async def disconnect(self, tenant_id: int, websocket: WebSocket) -> None:
        async with self._lock:
            conns = self._by_tenant.get(tenant_id)
            if conns and websocket in conns:
                conns.discard(websocket)
                if not conns:
                    self._by_tenant.pop(tenant_id, None)

    async def broadcast_to_tenant(self, tenant_id: int, message: dict) -> None:
        conns = list(self._by_tenant.get(tenant_id, ()))
        dead = []
        for ws in conns:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._by_tenant.get(tenant_id, set()).discard(ws)

    def connection_count(self, tenant_id: Optional[int] = None) -> int:
        if tenant_id is not None:
            return len(self._by_tenant.get(tenant_id, ()))
        return sum(len(v) for v in self._by_tenant.values())


manager = ConnectionManager()
