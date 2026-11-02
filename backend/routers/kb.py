from fastapi import APIRouter, Depends, HTTPException
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sqlalchemy.orm import Session

import models
import schemas
from auth import get_current_user, require_role
from database import get_db

router = APIRouter(prefix="/api/kb", tags=["knowledge-base"])


def _article_out(db: Session, a: models.Article) -> dict:
    author = db.query(models.User).filter(models.User.id == a.author_user_id).first()
    return {"id": a.id, "title": a.title, "content": a.content, "visibility": a.visibility,
            "author": author.name if author else "Unknown", "created_at": a.created_at}


@router.get("/articles", response_model=list[schemas.ArticleOut])
def list_articles(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    articles = db.query(models.Article).filter(models.Article.tenant_id == user.tenant_id).order_by(models.Article.created_at.desc()).all()
    return [_article_out(db, a) for a in articles]


@router.post("/articles", response_model=schemas.ArticleOut)
def create_article(payload: schemas.ArticleCreate, db: Session = Depends(get_db), user: models.User = Depends(require_role("standard_user"))):
    article = models.Article(tenant_id=user.tenant_id, title=payload.title, content=payload.content, visibility=payload.visibility, author_user_id=user.id)
    db.add(article)
    db.commit()
    db.refresh(article)
    return _article_out(db, article)


@router.get("/search")
def search_articles(q: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Real TF-IDF + cosine similarity ranking, not substring matching —
    computed entirely locally, no external embedding model. (Neural
    embeddings were the original plan; this sandbox's network egress is
    allowlisted to package registries only, so a pretrained model can't be
    downloaded from HuggingFace here. TF-IDF is a real, defensible
    information-retrieval technique on its own — this ranks by relevance,
    not just presence — but it's lexical, not semantic: it won't match
    'forgot my login' to an article about 'password reset' the way a real
    embedding model would. Swapping in sentence-transformers or an API
    embedding provider later is a drop-in replacement for this function.)
    """
    articles = db.query(models.Article).filter(models.Article.tenant_id == user.tenant_id).all()
    if not articles:
        return []

    corpus = [f"{a.title} {a.content}" for a in articles]
    vectorizer = TfidfVectorizer(stop_words="english")
    try:
        doc_vectors = vectorizer.fit_transform(corpus)
        query_vector = vectorizer.transform([q])
    except ValueError:
        return []  # query has no vocabulary overlap with any document at all

    scores = cosine_similarity(query_vector, doc_vectors)[0]
    ranked = sorted(zip(articles, scores), key=lambda pair: pair[1], reverse=True)
    return [_article_out(db, a) for a, score in ranked if score > 0]
