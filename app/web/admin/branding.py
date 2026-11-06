"""Administration > Branding: the broker's name, colour and contact details, and Website prices: the symbols
the website streams (both stored in the database)."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.deps import client_ip
from app.db import get_db
from app.models import Staff
from app.services import branding as svc
from app.services import market
from app.web.common import attempt, form_rows, render, staff_perm

router = APIRouter()
PERM = "settings.branding"


@router.get("/branding")
def branding_page(request: Request, me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    return render(request, "admin/branding.html", fields=svc.FIELDS, stored=svc.stored(db), defaults=svc.defaults())


@router.post("/branding")
async def branding_save(request: Request, me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    form = await request.form()
    values = {name: form.get(name, "") for name in svc.FIELDS}
    return attempt(request, lambda: svc.save(db, me, values, client_ip(request)), "Branding saved.", "/admin/branding")


@router.get("/website-prices")
def website_prices(request: Request, me: Staff = Depends(staff_perm("symbols.manage")), db: Session = Depends(get_db)):
    return render(request, "admin/website_prices.html", website=market.website_form(db), card_symbols=market.CARD_SYMBOLS)


@router.post("/website-prices")
async def website_prices_save(request: Request, me: Staff = Depends(staff_perm("symbols.manage")),
                              db: Session = Depends(get_db)):
    form = await request.form()
    rows = [dict(symbol=s, name=n, category=c)
            for s, n, c in form_rows(form, "web_symbol", "web_name", "web_category")]
    return attempt(request, lambda: market.save_website_symbols(db, me, rows, client_ip(request)),
                   "Website prices saved: {result} symbol(s), live within 30 seconds.", "/admin/website-prices")
