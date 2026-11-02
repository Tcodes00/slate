from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from sqlalchemy.orm import Session

import models
import schemas
from auth import get_current_user, require_role
from database import get_db
from storage import delete_file, get_file, save_file

router = APIRouter(prefix="/api/documents", tags=["documents"])


def _doc_out(db: Session, d: models.Document) -> dict:
    uploader = db.query(models.User).filter(models.User.id == d.uploaded_by_user_id).first()
    return {
        "id": d.id, "title": d.title, "category": d.category, "version": d.version,
        "company": d.party.display_name if d.party else None,
        "uploaded_by": uploader.name if uploader else "Unknown",
        "filename": d.filename, "size_bytes": d.size_bytes, "has_file": bool(d.storage_key),
        "created_at": d.created_at,
    }


@router.get("", response_model=list[schemas.DocumentOut])
def list_documents(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    docs = db.query(models.Document).filter(models.Document.tenant_id == user.tenant_id).order_by(models.Document.created_at.desc()).all()
    return [_doc_out(db, d) for d in docs]


@router.post("", response_model=schemas.DocumentOut)
async def create_document(
    title: str,
    category: str = "General",
    company: str = None,
    file: UploadFile = File(None),
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("standard_user")),
):
    """Real multipart upload — if a file is attached, its actual bytes are
    written to storage (see storage.py) and storage_key/filename/size are
    real, not placeholder metadata."""
    party_id = None
    if company:
        party = models.Party(tenant_id=user.tenant_id, party_type="organization", display_name=company, owner_user_id=user.id)
        db.add(party)
        db.flush()
        party_id = party.id

    doc = models.Document(tenant_id=user.tenant_id, party_id=party_id, title=title, category=category, uploaded_by_user_id=user.id)

    if file is not None:
        content = await file.read()
        key = save_file(user.tenant_id, file.filename, content)
        doc.storage_key = key
        doc.filename = file.filename
        doc.mime_type = file.content_type
        doc.size_bytes = len(content)

    db.add(doc)
    db.commit()
    db.refresh(doc)
    return _doc_out(db, doc)


@router.get("/{doc_id}/download")
def download_document(doc_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    doc = db.query(models.Document).filter(models.Document.id == doc_id, models.Document.tenant_id == user.tenant_id).first()
    if not doc or not doc.storage_key:
        raise HTTPException(404, "No file attached to this document")
    content = get_file(doc.storage_key)
    return Response(
        content=content,
        media_type=doc.mime_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{doc.filename}"'},
    )


@router.post("/{doc_id}/new-version", response_model=schemas.DocumentOut)
async def new_version(
    doc_id: int,
    file: UploadFile = File(None),
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("standard_user")),
):
    doc = db.query(models.Document).filter(models.Document.id == doc_id, models.Document.tenant_id == user.tenant_id).first()
    if not doc:
        raise HTTPException(404, "Document not found")

    old_key = doc.storage_key
    if file is not None:
        content = await file.read()
        doc.storage_key = save_file(user.tenant_id, file.filename, content)
        doc.filename = file.filename
        doc.mime_type = file.content_type
        doc.size_bytes = len(content)
        if old_key:
            delete_file(old_key)

    doc.version += 1
    doc.uploaded_by_user_id = user.id
    db.commit()
    db.refresh(doc)
    return _doc_out(db, doc)
