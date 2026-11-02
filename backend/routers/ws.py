"""
The one real-time entrypoint: browsers open a WebSocket here (with the
JWT as a query param, since the browser WebSocket API can't set custom
headers) and receive events published by events.py for their tenant —
deal changes, support ticket messages, and agent hand-off progress. Gated
by the realtime_updates feature flag, checked server-side at connect
time, same as every other flag in this codebase.
"""

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from jose import JWTError, jwt
from sqlalchemy.orm import Session

import models
from auth import ALGORITHM, SECRET_KEY
from database import SessionLocal
from feature_flags import is_enabled
from ws_manager import manager

router = APIRouter(tags=["realtime"])


def _authenticate(token: str):
    db: Session = SessionLocal()
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("purpose") == "mfa_pending":
            return None
        user_id = payload.get("sub")
        if user_id is None:
            return None
        user = db.query(models.User).filter(models.User.id == int(user_id)).first()
        if user is not None and db.query(models.UserLock.id).filter(models.UserLock.user_id == user.id).first() is not None:
            return None  # suspended / erased: no new live connections
        return user
    except JWTError:
        return None
    finally:
        db.close()


@router.websocket("/api/ws")
async def ws_endpoint(websocket: WebSocket, token: str = Query(...)):
    user = _authenticate(token)
    if user is None:
        await websocket.close(code=4401, reason="Invalid or expired token")
        return

    db: Session = SessionLocal()
    try:
        allowed = is_enabled(db, user.tenant_id, "realtime_updates")
    finally:
        db.close()
    if not allowed:
        await websocket.close(code=4403, reason="Real-time updates are turned off for this workspace")
        return

    await manager.connect(user.tenant_id, websocket)
    try:
        await websocket.send_json({"type": "connected", "data": {"user_id": user.id}})
        while True:
            # Clients don't need to send anything for this to work; this
            # just keeps the receive loop alive so a client-side close is
            # detected promptly, and lets the client send "ping" so
            # proxies/load balancers don't idle the connection out.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(user.tenant_id, websocket)
