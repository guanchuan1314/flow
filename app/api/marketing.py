from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.deps import require
from app.core.errors import get_or_404
from app.db import get_db
from app.models import Campaign, Client, CommissionPlan, IBCommission, IBProfile, Staff
from app.schemas import (
    CampaignIn, CampaignOut, CommissionOut, IBIn, IBOut, IBUpdate, PlanIn, PlanOut, RecalcIn,
)
from app.services import campaigns, commissions

router = APIRouter(tags=["marketing"])


# ---------------------------------------------------------------- commission plans
@router.get("/commission-plans", response_model=list[PlanOut])
def list_plans(db: Session = Depends(get_db), _: Staff = Depends(require("marketing.view"))):
    return db.scalars(select(CommissionPlan).order_by(CommissionPlan.id)).all()


@router.post("/commission-plans", response_model=PlanOut, status_code=201)
def create_plan(body: PlanIn, db: Session = Depends(get_db), me: Staff = Depends(require("marketing.manage"))):
    return commissions.create_plan(db, me, body.name, body.level_shares, [r.model_dump() for r in body.rules],
                                   body.trigger, body.min_hold_seconds)


# ---------------------------------------------------------------- IBs
@router.get("/ibs", response_model=list[IBOut])
def list_ibs(db: Session = Depends(get_db), _: Staff = Depends(require("marketing.view"))):
    return db.scalars(select(IBProfile).order_by(IBProfile.id)).all()


@router.post("/ibs", response_model=IBOut, status_code=201)
def create_ib(body: IBIn, db: Session = Depends(get_db), me: Staff = Depends(require("marketing.manage"))):
    return commissions.make_ib(db, me, get_or_404(db, Client, body.client_id), body.plan_id, body.parent_ib_id)


@router.patch("/ibs/{ib_id}", response_model=IBOut)
def update_ib(ib_id: int, body: IBUpdate, db: Session = Depends(get_db), me: Staff = Depends(require("marketing.manage"))):
    return commissions.update_ib(db, me, get_or_404(db, IBProfile, ib_id), body.model_dump(exclude_unset=True))


@router.get("/ibs/{ib_id}/tree")
def ib_tree(ib_id: int, db: Session = Depends(get_db), _: Staff = Depends(require("marketing.view"))):
    """Downline: sub-IBs (recursive) and directly referred clients with lifetime commission totals."""
    def node(ib: IBProfile, depth: int = 0) -> dict:
        client = db.get(Client, ib.client_id)
        referred = db.scalar(select(func.count()).select_from(Client).where(Client.referred_by_id == ib.client_id))
        earned = db.scalar(select(func.coalesce(func.sum(IBCommission.amount), 0)).where(IBCommission.ib_id == ib.id))
        children = db.scalars(select(IBProfile).where(IBProfile.parent_ib_id == ib.id)) if depth < 10 else []
        return {"ib_id": ib.id, "client_id": ib.client_id, "email": client.email, "referred_clients": referred,
                "commission_total": earned, "sub_ibs": [node(c, depth + 1) for c in children]}

    return node(get_or_404(db, IBProfile, ib_id))


@router.get("/commissions", response_model=list[CommissionOut])
def list_commissions(ib_id: int | None = None, status: str | None = None, limit: int = Query(200, le=5000),
                     db: Session = Depends(get_db), _: Staff = Depends(require("marketing.view"))):
    q = select(IBCommission)
    if ib_id:
        q = q.where(IBCommission.ib_id == ib_id)
    if status:
        q = q.where(IBCommission.status == status)
    return db.scalars(q.order_by(IBCommission.id.desc()).limit(limit)).all()


@router.post("/commissions/recalculate")
def recalculate(body: RecalcIn, db: Session = Depends(get_db), _: Staff = Depends(require("marketing.manage"))):
    return {"created": commissions.recalculate(db, body.date_from, body.date_to)}


@router.post("/commissions/payout")
def payout(ib_id: int | None = None, db: Session = Depends(get_db), me: Staff = Depends(require("marketing.payout"))):
    return commissions.payout(db, me, ib_id)


# ---------------------------------------------------------------- campaigns
@router.get("/campaigns", response_model=list[CampaignOut])
def list_campaigns(db: Session = Depends(get_db), _: Staff = Depends(require("marketing.view"))):
    return db.scalars(select(Campaign).order_by(Campaign.id.desc())).all()


@router.post("/campaigns", response_model=CampaignOut, status_code=201)
def create_campaign(body: CampaignIn, db: Session = Depends(get_db), me: Staff = Depends(require("marketing.manage"))):
    return campaigns.create_campaign(db, me, **body.model_dump())


@router.get("/marketing/sources")
def lead_sources(db: Session = Depends(get_db), _: Staff = Depends(require("marketing.view"))):
    """Registrations grouped by lead source / UTM campaign."""
    rows = db.execute(select(Client.lead_source, Client.utm_source, Client.utm_campaign, func.count())
                      .group_by(Client.lead_source, Client.utm_source, Client.utm_campaign)).all()
    return [{"lead_source": a, "utm_source": b, "utm_campaign": c, "clients": n} for a, b, c, n in rows]
