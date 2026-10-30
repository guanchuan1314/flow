from datetime import datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.errors import ServiceError, get_or_404
from app.db import get_db
from app.models import (
    AccountGroup, Campaign, CampaignRedemption, Client, CommissionPlan, IBCommission, IBProfile, IBStatus, KycStatus,
    PaymentKind, PaymentRequest, PaymentStatus, RuleKind, Staff,
)
from app.services import campaigns, commissions
from app.web.common import Page, attempt, form_rows, opt_dec, opt_enum, opt_int, redirect, render, staff_perm

router = APIRouter()


# ---------------------------------------------------------------- IBs
@router.get("/ibs")
def ibs(request: Request, me: Staff = Depends(staff_perm("marketing.view")), db: Session = Depends(get_db)):
    rows = db.scalars(select(IBProfile).order_by(IBProfile.id)).all()
    clients = {c.id: c for c in db.scalars(select(Client).where(Client.id.in_({i.client_id for i in rows})))}
    referred = dict(db.execute(select(Client.referred_by_id, func.count()).where(Client.referred_by_id.is_not(None))
                               .group_by(Client.referred_by_id)).all())
    totals = {(ib_id, status): amount for ib_id, status, amount in db.execute(
        select(IBCommission.ib_id, IBCommission.status, func.sum(IBCommission.amount))
        .group_by(IBCommission.ib_id, IBCommission.status)).all()}
    plans = db.scalars(select(CommissionPlan).order_by(CommissionPlan.name)).all()
    return render(request, "admin/marketing/ibs.html", rows=rows, clients=clients, referred=referred, totals=totals,
                  plans=plans, plan_names={p.id: p.name for p in plans})


@router.post("/ibs")
async def create_ib(request: Request, me: Staff = Depends(staff_perm("marketing.manage")), db: Session = Depends(get_db)):
    form = await request.form()
    created = {}

    def run():
        client = get_or_404(db, Client, opt_int(form.get("client_id")) or 0, "Client")
        created["ib"] = commissions.make_ib(db, me, client, opt_int(form.get("plan_id")), opt_int(form.get("parent_ib_id")))
    resp = attempt(request, run, "IB created.", "/admin/ibs")
    return redirect(f"/admin/ibs/{created['ib'].id}") if "ib" in created else resp


@router.get("/ibs/{ib_id}")
def ib_detail(request: Request, ib_id: int, me: Staff = Depends(staff_perm("marketing.view")),
              db: Session = Depends(get_db)):
    ib = get_or_404(db, IBProfile, ib_id, "IB")

    def node(x: IBProfile, depth: int = 0) -> dict:
        kids = db.scalars(select(IBProfile).where(IBProfile.parent_ib_id == x.id)).all() if depth < 10 else []
        return {"ib": x, "client": db.get(Client, x.client_id), "children": [node(k, depth + 1) for k in kids]}

    referred = db.scalars(select(Client).where(Client.referred_by_id == ib.client_id).order_by(Client.id.desc()).limit(200)).all()
    recent = db.scalars(select(IBCommission).where(IBCommission.ib_id == ib.id).order_by(IBCommission.id.desc()).limit(100)).all()
    plans = db.scalars(select(CommissionPlan).order_by(CommissionPlan.name)).all()
    others = db.scalars(select(IBProfile).where(IBProfile.id != ib.id)).all()
    other_clients = {c.id: c for c in db.scalars(select(Client).where(Client.id.in_({o.client_id for o in others})))}
    return render(request, "admin/marketing/ib.html", ib=ib, owner=db.get(Client, ib.client_id), tree=node(ib),
                  referred=referred, recent=recent, plans=plans, others=others, other_clients=other_clients,
                  statuses=list(IBStatus))


@router.post("/ibs/{ib_id}")
async def update_ib(request: Request, ib_id: int, me: Staff = Depends(staff_perm("marketing.manage")),
                    db: Session = Depends(get_db)):
    ib = get_or_404(db, IBProfile, ib_id, "IB")
    form = await request.form()

    def run():
        commissions.update_ib(db, me, ib, {"plan_id": opt_int(form.get("plan_id")),
                                           "parent_ib_id": opt_int(form.get("parent_ib_id")),
                                           "status": opt_enum(IBStatus, form.get("status"), "status")})
    return attempt(request, run, "IB updated.", f"/admin/ibs/{ib_id}")


# ---------------------------------------------------------------- commission plans
@router.get("/plans")
def plans(request: Request, me: Staff = Depends(staff_perm("marketing.view")), db: Session = Depends(get_db)):
    rows = db.scalars(select(CommissionPlan).order_by(CommissionPlan.id)).all()
    usage = dict(db.execute(select(IBProfile.plan_id, func.count()).group_by(IBProfile.plan_id)).all())
    groups = {t.id: t.name for t in db.scalars(select(AccountGroup))}
    return render(request, "admin/marketing/plans.html", rows=rows, usage=usage, groups=groups)


@router.get("/plans/new")
def plan_new(request: Request, me: Staff = Depends(staff_perm("marketing.manage")), db: Session = Depends(get_db)):
    return render(request, "admin/marketing/plan_form.html", kinds=list(RuleKind),
                  groups=db.scalars(select(AccountGroup).order_by(AccountGroup.name)).all())


@router.post("/plans/new")
async def plan_create(request: Request, me: Staff = Depends(staff_perm("marketing.manage")), db: Session = Depends(get_db)):
    form = await request.form()

    def run():
        try:
            shares = [float(x) / 100 for x in str(form.get("level_shares", "100")).replace(";", ",").split(",") if x.strip()]
        except ValueError:
            raise ServiceError("Level shares must be percentages separated by commas, e.g. 70, 20, 10")
        rules = []
        for pattern, kind, value, group_id, prio in form_rows(form, "rule_pattern", "rule_kind", "rule_value",
                                                              "rule_type", "rule_priority", strip=False):
            if not str(value).strip():
                continue
            rk = opt_enum(RuleKind, kind, "rule type")
            v = opt_dec(value)
            if rk == RuleKind.spread_share:
                v = v / 100  # entered as % of markup
            rules.append({"symbol_pattern": (pattern or "*").strip().upper() or "*", "kind": rk, "value": v,
                          "account_group_id": opt_int(group_id), "priority": opt_int(prio) or 100})
        if not rules:
            raise ServiceError("Add at least one rule")
        commissions.create_plan(db, me, str(form.get("name", "")).strip(), shares, rules,
                                str(form.get("trigger", "close")), opt_int(form.get("min_hold_seconds")) or 0)
    return attempt(request, run, "Commission plan created.", "/admin/plans")


# ---------------------------------------------------------------- commissions
@router.get("/commissions")
def commission_list(request: Request, status: str = "pending", ib_id: str = "", page: int = 1,
                    me: Staff = Depends(staff_perm("marketing.view")), db: Session = Depends(get_db)):
    query = select(IBCommission)
    if status:
        query = query.where(IBCommission.status == status)
    if opt_int(ib_id):
        query = query.where(IBCommission.ib_id == opt_int(ib_id))
    pg = Page(db, query.order_by(IBCommission.id.desc()), page, per_page=100)
    pending_total = db.scalar(select(func.coalesce(func.sum(IBCommission.amount), 0)).where(IBCommission.status == "pending"))
    ib_clients = dict(db.execute(select(IBProfile.id, IBProfile.client_id)).all())
    names = {c.id: c.full_name for c in db.scalars(select(Client).where(
        Client.id.in_(set(ib_clients.values()) | {r.trader_client_id for r in pg.items})))}
    return render(request, "admin/marketing/commissions.html", page=pg, status=status, ib_id=ib_id,
                  pending_total=pending_total, ib_clients=ib_clients, names=names)


@router.post("/commissions/payout")
async def payout(request: Request, me: Staff = Depends(staff_perm("marketing.payout")), db: Session = Depends(get_db)):
    form = await request.form()
    target = opt_int(form.get("ib_id"))
    return attempt(request, lambda: len(commissions.payout(db, me, target)),
                   "Commissions paid to {result} IB wallet(s).", "/admin/commissions")


@router.post("/commissions/recalculate")
async def recalculate(request: Request, me: Staff = Depends(staff_perm("marketing.manage")), db: Session = Depends(get_db)):
    form = await request.form()

    def run():
        try:
            start = datetime.fromisoformat(str(form.get("date_from")))
            end = datetime.fromisoformat(str(form.get("date_to"))).replace(hour=23, minute=59, second=59)
        except ValueError:
            raise ServiceError("Choose a valid date range")
        return commissions.recalculate(db, start, end)
    return attempt(request, run, "Recalculation created {result} missing commission rows.", "/admin/commissions")


# ---------------------------------------------------------------- campaigns
@router.get("/campaigns")
def campaign_list(request: Request, me: Staff = Depends(staff_perm("marketing.view")), db: Session = Depends(get_db)):
    rows = db.scalars(select(Campaign).order_by(Campaign.id.desc())).all()
    used = dict(db.execute(select(CampaignRedemption.campaign_id, func.count()).group_by(CampaignRedemption.campaign_id)).all())
    paid = dict(db.execute(select(CampaignRedemption.campaign_id, func.sum(CampaignRedemption.bonus_amount))
                           .group_by(CampaignRedemption.campaign_id)).all())
    return render(request, "admin/marketing/campaigns.html", rows=rows, used=used, paid=paid)


@router.post("/campaigns")
async def campaign_create(request: Request, me: Staff = Depends(staff_perm("marketing.manage")), db: Session = Depends(get_db)):
    f = await request.form()

    def run():
        def when(key):
            v = str(f.get(key) or "").strip()
            try:
                return datetime.fromisoformat(v) if v else None
            except ValueError:
                raise ServiceError(f"Invalid date for {key}")
        code = str(f.get("code", "")).strip()
        if len(code) < 3:
            raise ServiceError("Promo code must be at least 3 characters")
        campaigns.create_campaign(db, me, name=str(f.get("name", "")).strip() or code, code=code,
                                  bonus_pct=opt_dec(f.get("bonus_pct")) or 0, max_bonus=opt_dec(f.get("max_bonus")) or 0,
                                  min_deposit=opt_dec(f.get("min_deposit")) or 0, starts_at=when("starts_at"),
                                  ends_at=when("ends_at"), max_redemptions=opt_int(f.get("max_redemptions")),
                                  once_per_client=f.get("once_per_client") == "on", active=True)
    return attempt(request, run, "Campaign created.", "/admin/campaigns")


@router.post("/campaigns/{campaign_id}/toggle")
def campaign_toggle(request: Request, campaign_id: int, me: Staff = Depends(staff_perm("marketing.manage")),
                    db: Session = Depends(get_db)):
    c = get_or_404(db, Campaign, campaign_id, "Campaign")
    return attempt(request, lambda: campaigns.set_active(db, me, c, not c.active),
                   "Campaign updated.", "/admin/campaigns")


@router.get("/sources")
def sources(request: Request, me: Staff = Depends(staff_perm("marketing.view")), db: Session = Depends(get_db)):
    funded = select(PaymentRequest.client_id).where(PaymentRequest.kind == PaymentKind.deposit,
                                                    PaymentRequest.status == PaymentStatus.approved)
    rows = db.execute(select(Client.lead_source, Client.utm_source, Client.utm_medium, Client.utm_campaign,
                             func.count(),
                             func.sum(case((Client.kyc_status == KycStatus.approved, 1), else_=0)),
                             func.sum(case((Client.id.in_(funded), 1), else_=0)))
                      .group_by(Client.lead_source, Client.utm_source, Client.utm_medium, Client.utm_campaign)
                      .order_by(func.count().desc())).all()
    return render(request, "admin/marketing/sources.html", rows=rows)
