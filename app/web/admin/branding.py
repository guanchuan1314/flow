"""Administration > Branding: the broker's name, colour and contact details (stored in the database)."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.deps import client_ip
from app.db import get_db
from app.models import Staff
from app.services import branding as svc
from app.web.common import attempt, render, staff_perm

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
