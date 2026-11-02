"""
Shared security helpers: field-level encryption and audit logging.

FIELD_ENCRYPTION_KEY must be a valid Fernet key (32 url-safe base64 bytes).
Generate one with: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Falls back to a fixed dev key so the app still runs out of the box —
replace it before any real sensitive data goes near this.
"""

import json
import os
from datetime import datetime

from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

import models

DEV_KEY = "emfWTE1sYWkfI1HoyeBueyaEdLGeHroETxyRE7cpipo="  # generated via Fernet.generate_key(), dev-only
_fernet = Fernet(os.getenv("FIELD_ENCRYPTION_KEY", DEV_KEY).encode())


def encrypt_field(plaintext: str) -> str:
    if not plaintext:
        return plaintext
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_field(ciphertext: str) -> str:
    if not ciphertext:
        return ciphertext
    return _fernet.decrypt(ciphertext.encode()).decode()


def log_audit(
    db: Session,
    tenant_id: int,
    actor,  # models.User or None
    action: str,
    resource_type: str,
    resource_id: int = None,
    before: dict = None,
    after: dict = None,
) -> None:
    entry = models.AuditLog(
        tenant_id=tenant_id,
        actor_user_id=actor.id if actor else None,
        actor_name=actor.name if actor else "system",
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        before=json.dumps(before, default=str) if before is not None else None,
        after=json.dumps(after, default=str) if after is not None else None,
        created_at=datetime.utcnow(),
    )
    db.add(entry)
