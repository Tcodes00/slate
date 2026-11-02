"""
Real file storage, local-disk backed, behind the same three-function
shape a real object-storage client (boto3/MinIO) would have: save, get,
delete. Nothing that calls this module touches the filesystem directly —
that's what makes swapping to real S3 later a change in this one file,
not a rewrite of every caller.
"""

import os
import uuid

STORAGE_ROOT = os.getenv("STORAGE_ROOT", "./storage")
os.makedirs(STORAGE_ROOT, exist_ok=True)


def save_file(tenant_id: int, filename: str, content: bytes) -> str:
    """Returns a storage_key — callers store this, never a raw path."""
    key = f"{tenant_id}/{uuid.uuid4().hex}_{filename}"
    full_path = os.path.join(STORAGE_ROOT, key)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "wb") as f:
        f.write(content)
    return key


def get_file(storage_key: str) -> bytes:
    full_path = os.path.join(STORAGE_ROOT, storage_key)
    with open(full_path, "rb") as f:
        return f.read()


def delete_file(storage_key: str) -> None:
    full_path = os.path.join(STORAGE_ROOT, storage_key)
    if os.path.exists(full_path):
        os.remove(full_path)
