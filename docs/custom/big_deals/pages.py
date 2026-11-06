from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Staff
from app.web.common import render, staff_perm

router = APIRouter()


@router.get("")
def big_deals(request: Request, me: Staff = Depends(staff_perm("big_deals.view")), db: Session = Depends(get_db)):
    rows = db.execute(text("SELECT ticket, login, symbol, lots, time FROM custom_big_deals ORDER BY time DESC LIMIT 100"))
    return render(request, "big_deals/list.html", rows=rows.mappings().all())
