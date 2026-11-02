"""
Knowledge search & answers (rag.py): upload documents, index knowledge-base articles, search them, and ask
questions answered from them with citations.

Off by default per workspace (`knowledge_rag` flag): indexing sends document text to the configured embedding
provider and answering sends retrieved passages to the configured AI provider. Reading (search / ask) is for any
signed-in non-read-only user; changing the index (upload, delete, reindex) is for managers and above, because
what's indexed becomes retrievable by the whole workspace. Every retrieval is scoped to the caller's workspace and
role inside rag.search(), not here.
"""

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

import models
import rag
import schemas
from auth import require_role
from database import get_db
from feature_flags import is_enabled
from rate_limit import limiter
from security import log_audit

router = APIRouter(prefix="/api/rag", tags=["knowledge-rag"])


def _gate(db: Session, user: models.User) -> models.User:
    if not is_enabled(db, user.tenant_id, "knowledge_rag"):
        raise HTTPException(403, "Knowledge search & answers is turned off for this workspace — an org admin can turn it on under Feature Flags")
    return user


def rag_reader(db: Session = Depends(get_db), user: models.User = Depends(require_role("standard_user"))) -> models.User:
    return _gate(db, user)


def rag_manager(db: Session = Depends(get_db), user: models.User = Depends(require_role("manager"))) -> models.User:
    return _gate(db, user)


def _doc_out(d: models.RagDocument, current_model: str) -> dict:
    return {"id": d.id, "title": d.title, "source_type": d.source_type, "source_id": d.source_id, "filename": d.filename, "min_role": d.min_role,
            "chunk_count": d.chunk_count, "char_count": d.char_count, "embedding_model": d.embedding_model, "stale": d.embedding_model != current_model,
            "updated_at": d.updated_at.isoformat() if d.updated_at else None}


def _get_doc(db: Session, user: models.User, doc_id: int, need_manager: bool = False) -> models.RagDocument:
    doc = db.query(models.RagDocument).filter(models.RagDocument.id == doc_id, models.RagDocument.tenant_id == user.tenant_id).first()
    if doc is None or (not need_manager and rag.ROLE_RANK.get(doc.min_role, 99) > rag.ROLE_RANK.get(user.role, 0)):
        raise HTTPException(404, "Document not found")  # a document you may not see doesn't exist, as far as you're concerned
    return doc


@router.get("/status")
def get_status(db: Session = Depends(get_db), user: models.User = Depends(rag_reader)):
    return rag.status(db, user.tenant_id)


@router.get("/documents")
def list_documents(db: Session = Depends(get_db), user: models.User = Depends(rag_reader)):
    current = rag.embedder_for_tenant(db, user.tenant_id).name
    rank = rag.ROLE_RANK.get(user.role, 0)
    docs = db.query(models.RagDocument).filter(models.RagDocument.tenant_id == user.tenant_id).order_by(models.RagDocument.id).all()
    return [_doc_out(d, current) for d in docs if rag.ROLE_RANK.get(d.min_role, 99) <= rank]


@router.post("/documents/upload")
async def upload_document(file: UploadFile = File(...), title: Optional[str] = Form(None), min_role: str = Form("standard_user"),
                          db: Session = Depends(get_db), user: models.User = Depends(rag_manager)):
    data = await file.read(rag.MAX_UPLOAD_BYTES + 1)
    name = file.filename or "upload"
    try:
        pages = rag.extract_pages(name, data)
        result = await rag.index_document(db, user.tenant_id, user.id, title=title or name.rsplit(".", 1)[0], pages=pages, source_type="upload", min_role=min_role, filename=name)
    except rag.RagError as e:
        raise HTTPException(e.status, str(e))
    log_audit(db, user.tenant_id, user, "rag.document_indexed", "rag_document", result["document_id"], after={"title": title or name, "chunks": result["chunks"], "status": result["status"]})
    db.commit()
    return result


@router.post("/documents/text")
async def add_text(payload: schemas.RagTextIn, db: Session = Depends(get_db), user: models.User = Depends(rag_manager)):
    try:
        result = await rag.index_document(db, user.tenant_id, user.id, title=payload.title, pages=[(None, payload.text)], source_type="text", min_role=payload.min_role)
    except rag.RagError as e:
        raise HTTPException(e.status, str(e))
    log_audit(db, user.tenant_id, user, "rag.document_indexed", "rag_document", result["document_id"], after={"title": payload.title, "chunks": result["chunks"], "status": result["status"]})
    db.commit()
    return result


@router.delete("/documents/{doc_id}")
def remove_document(doc_id: int, db: Session = Depends(get_db), user: models.User = Depends(rag_manager)):
    doc = _get_doc(db, user, doc_id, need_manager=True)
    title = doc.title
    try:
        rag.delete_document(db, doc)
    except rag.RagError as e:
        raise HTTPException(e.status, str(e))
    log_audit(db, user.tenant_id, user, "rag.document_deleted", "rag_document", doc_id, after={"title": title})
    db.commit()
    return {"deleted": True}


@router.get("/documents/{doc_id}/chunks")
def document_chunks(doc_id: int, db: Session = Depends(get_db), user: models.User = Depends(rag_reader)):
    doc = _get_doc(db, user, doc_id)
    rows = db.query(models.RagChunk).filter(models.RagChunk.document_id == doc.id).order_by(models.RagChunk.ordinal).all()
    return [{"id": c.id, "ordinal": c.ordinal, "page": c.page, "text": c.text} for c in rows]


@router.post("/documents/{doc_id}/analyze")
async def analyze_document(doc_id: int, db: Session = Depends(get_db), user: models.User = Depends(rag_reader)):
    doc = _get_doc(db, user, doc_id)
    tenant = db.query(models.Tenant).filter(models.Tenant.id == user.tenant_id).first()
    try:
        return await rag.analyze(db, tenant, doc)
    except rag.RagError as e:
        raise HTTPException(e.status, str(e))


@router.post("/index/kb")
async def index_knowledge_base(db: Session = Depends(get_db), user: models.User = Depends(rag_manager)):
    try:
        result = await rag.index_kb(db, user.tenant_id, user.id)
    except rag.RagError as e:
        raise HTTPException(e.status, str(e))
    log_audit(db, user.tenant_id, user, "rag.kb_indexed", "rag_document", None, after=result)
    db.commit()
    return result


@router.post("/reindex")
async def reindex(db: Session = Depends(get_db), user: models.User = Depends(rag_manager)):
    try:
        result = await rag.reindex_stale(db, user.tenant_id)
    except rag.RagError as e:
        raise HTTPException(e.status, str(e))
    log_audit(db, user.tenant_id, user, "rag.reindexed", "rag_document", None, after=result)
    db.commit()
    return result


@router.post("/search")
@limiter.limit("60/minute")
async def search(payload: schemas.RagSearchIn, request: Request, db: Session = Depends(get_db), user: models.User = Depends(rag_reader)):
    try:
        return await rag.search(db, user.tenant_id, user.role, payload.query, payload.top_k, payload.mode)
    except rag.RagError as e:
        raise HTTPException(e.status, str(e))


@router.post("/ask")
@limiter.limit("20/minute")
async def ask(payload: schemas.RagAskIn, request: Request, db: Session = Depends(get_db), user: models.User = Depends(rag_reader)):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == user.tenant_id).first()
    try:
        return await rag.ask(db, tenant, user.role, payload.question, payload.top_k)
    except rag.RagError as e:
        raise HTTPException(e.status, str(e))
