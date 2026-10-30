"""Public broker website: marketing pages fed by live CRM data, plus lead capture."""

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import client_ip
from app.core.errors import ServiceError
from app.core.ratelimit import check_auth_rate
from app.db import get_db, utcnow
from app.models import AccountGroup, Campaign, CommissionPlan, PammFund, StrategyStatus
from app.services import clients, market
from app.web.common import csrf_protect, flash, redirect, render

router = APIRouter(include_in_schema=False, dependencies=[Depends(csrf_protect)])

LEGAL = {
    "risk-disclosure": "Risk disclosure",
    "terms": "Terms and conditions",
    "privacy": "Privacy policy",
    "aml": "AML & KYC policy",
}


def _page(request: Request, template: str, **ctx):
    ctx.setdefault("signed_in", bool(request.session.get("client_id")))
    return render(request, template, **ctx)


def _live_groups(db: Session):
    return db.scalars(select(AccountGroup).where(AccountGroup.enabled.is_(True), AccountGroup.is_demo.is_(False))
                      .order_by(AccountGroup.min_deposit)).all()


def _active_campaigns(db: Session):
    now = utcnow()
    rows = db.scalars(select(Campaign).where(Campaign.active.is_(True)).order_by(Campaign.id.desc())).all()
    return [c for c in rows if (not c.starts_at or c.starts_at <= now) and (not c.ends_at or c.ends_at >= now)]


@router.get("/")
def home(request: Request, db: Session = Depends(get_db)):
    return _page(request, "site/home.html", quotes=market.quotes(), groups=_live_groups(db),
                 strategies=market.strategy_stats(db)[:3], campaigns=_active_campaigns(db)[:1])


@router.get("/accounts")
def account_groups(request: Request, db: Session = Depends(get_db)):
    demo = db.scalar(select(AccountGroup).where(AccountGroup.enabled.is_(True), AccountGroup.is_demo.is_(True)))
    return _page(request, "site/accounts.html", groups=_live_groups(db), demo=demo)


@router.get("/markets")
def markets(request: Request):
    return _page(request, "site/markets.html", quotes=market.quotes())


@router.get("/quotes")
def quotes_partial(request: Request, view: str = "table"):
    """htmx refresh target for the live price widgets."""
    template = {"ticker": "site/_ticker.html", "hero": "site/_hero_quotes.html",
                "mini": "site/_mini_quotes.html"}.get(view, "site/_quotes_table.html")
    return render(request, template, quotes=market.quotes())


@router.get("/platforms")
def platforms(request: Request):
    return _page(request, "site/platforms.html")


@router.get("/copy-trading")
def copy_trading(request: Request, db: Session = Depends(get_db)):
    funds = db.scalars(select(PammFund).where(PammFund.status == StrategyStatus.active)).all()
    return _page(request, "site/copy.html", strategies=market.strategy_stats(db), funds=funds)


@router.get("/partners")
def partners(request: Request, db: Session = Depends(get_db)):
    plans = db.scalars(select(CommissionPlan).order_by(CommissionPlan.id)).all()
    return _page(request, "site/partners.html", plans=plans)


@router.get("/promotions")
def promotions(request: Request, db: Session = Depends(get_db)):
    return _page(request, "site/promotions.html", campaigns=_active_campaigns(db))


@router.get("/about")
def about(request: Request):
    return _page(request, "site/about.html")


@router.get("/contact")
def contact_page(request: Request, topic: str = "general"):
    return _page(request, "site/contact.html", topic=topic)


@router.post("/contact")
def contact(request: Request, name: str = Form(...), email: str = Form(...), phone: str = Form(""),
            country: str = Form(""), topic: str = Form("general"), message: str = Form(...),
            website: str = Form(""), db: Session = Depends(get_db)):
    ip = client_ip(request)
    form = dict(name=name, email=email, phone=phone, country=country, message=message)
    try:
        check_auth_rate("contact", ip)
        if website:  # honeypot field, invisible to people: bots fill it in
            raise ServiceError("Your message could not be sent")
        clients.capture_website_lead(db, name=name, email=email, phone=phone, country=country,
                                     topic=topic[:32], message=message, ip=ip)
    except ServiceError as exc:
        return _page(request, "site/contact.html", status_code=400, error=exc.message, form=form, topic=topic)
    flash(request, "Thank you, we have received your message. Our team will contact you shortly.")
    return redirect("/contact")


@router.get("/legal/{slug}")
def legal(request: Request, slug: str):
    if slug not in LEGAL:
        from starlette.exceptions import HTTPException

        raise HTTPException(404, "Page not found")
    return _page(request, "site/legal.html", slug=slug, title=LEGAL[slug], pages=LEGAL)
