from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from auth import get_current_user, require_role
from database import get_db

router = APIRouter(prefix="/api/marketing", tags=["marketing"])


@router.get("/campaigns", response_model=list[schemas.CampaignOut])
def list_campaigns(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return db.query(models.Campaign).filter(models.Campaign.tenant_id == user.tenant_id).order_by(models.Campaign.created_at.desc()).all()


@router.post("/campaigns", response_model=schemas.CampaignOut)
def create_campaign(payload: schemas.CampaignCreate, db: Session = Depends(get_db), user: models.User = Depends(require_role("standard_user"))):
    campaign = models.Campaign(tenant_id=user.tenant_id, name=payload.name, channel=payload.channel, budget=payload.budget)
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return campaign


@router.patch("/campaigns/{campaign_id}", response_model=schemas.CampaignOut)
def update_campaign(campaign_id: int, payload: schemas.CampaignUpdate, db: Session = Depends(get_db), user: models.User = Depends(require_role("standard_user"))):
    campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id, models.Campaign.tenant_id == user.tenant_id).first()
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    campaign.status = payload.status
    db.commit()
    db.refresh(campaign)
    return campaign


# ---------------------------------------------------------------------------
# Real segmentation. Party stays the shared, lean core table every other
# module also reads and writes — PartyProfile is a separate 1:1 extension
# table carrying only what Marketing actually needs (industry, size, tags),
# so this doesn't touch `parties` or any other module's behavior.
# Segments are saved queries: membership is computed live at read time,
# never stored as a snapshot, so a segment can't silently go stale.
# ---------------------------------------------------------------------------

def _normalize_tags(tags) -> str:
    """Stored as ',tag-one,tag-two,' (leading/trailing delimiters) so
    filtering can match a whole tag with LIKE '%,tag,%' — a plain
    '%tag%' substring match would also match 'enterprise' when filtering
    for 'rise', which real search bars love to get wrong."""
    if not tags:
        return ""
    clean = sorted({t.strip().lower() for t in tags if t and t.strip()})
    return "," + ",".join(clean) + "," if clean else ""


def _tags_list(stored: str) -> list:
    return [t for t in (stored or "").split(",") if t]


def _profile_out(party: models.Party, profile: models.PartyProfile = None) -> dict:
    return {
        "id": party.id,
        "display_name": party.display_name,
        "primary_email": party.primary_email,
        "industry": profile.industry if profile else None,
        "company_size": profile.company_size if profile else None,
        "tags": _tags_list(profile.tags) if profile else [],
    }


@router.get("/parties", response_model=list[schemas.PartyWithProfileOut])
def list_parties_with_profile(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Every party in the tenant (created from CRM deals, invoices,
    tickets, vendors — Party is shared core, see models.py) alongside
    whatever marketing enrichment has been added on top."""
    parties = db.query(models.Party).filter(models.Party.tenant_id == user.tenant_id).order_by(models.Party.display_name).all()
    profiles = {
        p.party_id: p
        for p in db.query(models.PartyProfile).filter(models.PartyProfile.tenant_id == user.tenant_id).all()
    }
    return [_profile_out(party, profiles.get(party.id)) for party in parties]


@router.patch("/parties/{party_id}/profile", response_model=schemas.PartyWithProfileOut)
def update_party_profile(
    party_id: int,
    payload: schemas.PartyProfileUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("standard_user")),
):
    party = db.query(models.Party).filter(models.Party.id == party_id, models.Party.tenant_id == user.tenant_id).first()
    if not party:
        raise HTTPException(404, "Party not found")

    profile = db.query(models.PartyProfile).filter(models.PartyProfile.party_id == party_id).first()
    if profile is None:
        profile = models.PartyProfile(tenant_id=user.tenant_id, party_id=party_id)
        db.add(profile)

    if payload.industry is not None:
        profile.industry = payload.industry.strip().lower() or None
    if payload.company_size is not None:
        profile.company_size = payload.company_size.strip() or None
    if payload.tags is not None:
        profile.tags = _normalize_tags(payload.tags)

    db.commit()
    db.refresh(profile)
    return _profile_out(party, profile)


def _segment_query(db: Session, tenant_id: int, industry=None, company_size=None, tag=None, require_email=True):
    q = db.query(models.Party).filter(models.Party.tenant_id == tenant_id)
    if require_email:
        q = q.filter(models.Party.primary_email.isnot(None))
    if industry or company_size or tag:
        q = q.join(models.PartyProfile, models.PartyProfile.party_id == models.Party.id)
        if industry:
            q = q.filter(models.PartyProfile.industry == industry.strip().lower())
        if company_size:
            q = q.filter(models.PartyProfile.company_size == company_size)
        if tag:
            q = q.filter(models.PartyProfile.tags.like(f"%,{tag.strip().lower()},%"))
    return q


@router.get("/audience-size")
def audience_size(
    industry: str = None,
    company_size: str = None,
    tag: str = None,
    require_email: bool = True,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """A live preview count for an ad-hoc (not-yet-saved) filter combo —
    used while building a segment, and also what a plain unfiltered call
    returns: everyone addressable, same as before this existed."""
    count = _segment_query(db, user.tenant_id, industry, company_size, tag, require_email).count()
    return {"addressable_parties": count}


@router.get("/segments", response_model=list[schemas.SegmentOut])
def list_segments(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    segments = db.query(models.Segment).filter(models.Segment.tenant_id == user.tenant_id).order_by(models.Segment.created_at.desc()).all()
    out = []
    for s in segments:
        count = _segment_query(db, user.tenant_id, s.filter_industry, s.filter_company_size, s.filter_tag, s.require_email).count()
        out.append({
            "id": s.id, "name": s.name, "filter_industry": s.filter_industry,
            "filter_company_size": s.filter_company_size, "filter_tag": s.filter_tag,
            "require_email": s.require_email, "member_count": count, "created_at": s.created_at,
        })
    return out


@router.post("/segments", response_model=schemas.SegmentOut)
def create_segment(payload: schemas.SegmentCreate, db: Session = Depends(get_db), user: models.User = Depends(require_role("standard_user"))):
    segment = models.Segment(
        tenant_id=user.tenant_id, name=payload.name, filter_industry=payload.filter_industry,
        filter_company_size=payload.filter_company_size, filter_tag=payload.filter_tag,
        require_email=payload.require_email, created_by_user_id=user.id,
    )
    db.add(segment)
    db.commit()
    db.refresh(segment)
    count = _segment_query(db, user.tenant_id, segment.filter_industry, segment.filter_company_size, segment.filter_tag, segment.require_email).count()
    return {
        "id": segment.id, "name": segment.name, "filter_industry": segment.filter_industry,
        "filter_company_size": segment.filter_company_size, "filter_tag": segment.filter_tag,
        "require_email": segment.require_email, "member_count": count, "created_at": segment.created_at,
    }


@router.delete("/segments/{segment_id}")
def delete_segment(segment_id: int, db: Session = Depends(get_db), user: models.User = Depends(require_role("standard_user"))):
    segment = db.query(models.Segment).filter(models.Segment.id == segment_id, models.Segment.tenant_id == user.tenant_id).first()
    if not segment:
        raise HTTPException(404, "Segment not found")
    db.delete(segment)
    db.commit()
    return {"message": "Segment deleted"}


@router.get("/segments/{segment_id}/members", response_model=list[schemas.PartyWithProfileOut])
def segment_members(segment_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    segment = db.query(models.Segment).filter(models.Segment.id == segment_id, models.Segment.tenant_id == user.tenant_id).first()
    if not segment:
        raise HTTPException(404, "Segment not found")
    parties = _segment_query(db, user.tenant_id, segment.filter_industry, segment.filter_company_size, segment.filter_tag, segment.require_email).all()
    profiles = {
        p.party_id: p
        for p in db.query(models.PartyProfile).filter(models.PartyProfile.tenant_id == user.tenant_id).all()
    }
    return [_profile_out(party, profiles.get(party.id)) for party in parties]
