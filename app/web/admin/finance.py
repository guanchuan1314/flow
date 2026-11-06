from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFound, get_or_404
from app.db import get_db
from app.models import (
    AccountStatus, Client, PaymentKind, PaymentMethod, PaymentRequest, PaymentStatus, Staff, TradingAccount,
)
from app.services import payments as svc
from app.services.clients import ensure_visible, visible_client_ids
from app.web.common import Page, attempt, opt_enum, opt_int, redirect, render, req_dec, staff_perm

router = APIRouter()


@router.get("/payments")
def list_payments(request: Request, status: str = "pending", kind: str = "", page: int = 1,
                  me: Staff = Depends(staff_perm("finance.view")), db: Session = Depends(get_db)):
    query = select(PaymentRequest).where(PaymentRequest.client_id.in_(visible_client_ids(me)))
    if status:
        query = query.where(PaymentRequest.status == status)
    if kind:
        query = query.where(PaymentRequest.kind == kind)
    pg = Page(db, query.order_by(PaymentRequest.id.desc() if status != "pending" else PaymentRequest.id), page)
    clients = {c.id: c for c in db.scalars(select(Client).where(Client.id.in_({p.client_id for p in pg.items})))}
    staff_names = dict(db.execute(select(Staff.id, Staff.full_name)).all())
    return render(request, "admin/payments/list.html", page=pg, clients=clients, staff_names=staff_names,
                  status=status, kind=kind, statuses=list(PaymentStatus), kinds=list(PaymentKind))


@router.get("/payments/new")
def new_payment(request: Request, client_id: int, me: Staff = Depends(staff_perm("finance.create")),
                db: Session = Depends(get_db)):
    client = ensure_visible(db, me, client_id)
    accounts = db.scalars(select(TradingAccount).where(
        TradingAccount.client_id == client.id, TradingAccount.is_demo.is_(False),
        TradingAccount.status == AccountStatus.active)).all()
    options = [(a.ref, f"MT5 {a.login} on {a.server.name}" + (f" ({a.account_group.name})" if a.account_group else ""))
               for a in accounts]
    methods = [(m.name, m.name) for m in db.scalars(select(PaymentMethod).order_by(PaymentMethod.sort_order))]
    return render(request, "admin/payments/new.html", c=client, account_options=options,
                  method_options=methods + [("cash", "Cash"), ("manual", "Manual adjustment")])


@router.post("/payments/new")
def create_payment(request: Request, client_id: int = Form(...), kind: str = Form(...), amount: str = Form(...),
                   method: str = Form("bank"), trading_login: str = Form(""), promo_code: str = Form(""),
                   external_ref: str = Form(""), details: str = Form(""),
                   me: Staff = Depends(staff_perm("finance.create")), db: Session = Depends(get_db)):
    client = ensure_visible(db, me, client_id)
    created = {}

    def run():
        k = opt_enum(PaymentKind, kind, "payment type")
        created["p"] = svc.create_request(db, me, client, k, req_dec(amount), method=method,
                                          trading_login=trading_login.strip() or None, promo_code=promo_code.strip() or None,
                                          external_ref=external_ref.strip() or None, details=details.strip())
    resp = attempt(request, run, "Payment request created. Another staff member must approve it.",
                   f"/admin/clients/{client_id}?tab=finance")
    return redirect(f"/admin/payments/{created['p'].id}") if "p" in created else resp


@router.get("/payments/{payment_id}")
def payment_detail(request: Request, payment_id: int, me: Staff = Depends(staff_perm("finance.view")),
                   db: Session = Depends(get_db)):
    p = get_or_404(db, PaymentRequest, payment_id)
    client = ensure_visible(db, me, p.client_id)
    staff_names = dict(db.execute(select(Staff.id, Staff.full_name)).all())
    return render(request, "admin/payments/detail.html", p=p, c=client, staff_names=staff_names)


@router.get("/payments/{payment_id}/proof")
def payment_proof(payment_id: int, me: Staff = Depends(staff_perm("finance.view")), db: Session = Depends(get_db)):
    p = get_or_404(db, PaymentRequest, payment_id)
    ensure_visible(db, me, p.client_id)
    if not p.proof_path or not Path(p.proof_path).is_file():
        raise NotFound("No proof of payment stored")
    return FileResponse(p.proof_path, filename=p.proof_name or Path(p.proof_path).name,
                        content_disposition_type="attachment")


@router.post("/payments/{payment_id}/approve")
def approve(request: Request, payment_id: int, comment: str = Form(""),
            me: Staff = Depends(staff_perm("finance.approve")), db: Session = Depends(get_db)):
    p = get_or_404(db, PaymentRequest, payment_id)
    ensure_visible(db, me, p.client_id)
    return attempt(request, lambda: svc.approve(db, None, me, p, comment.strip() or None),
                   f"Payment #{payment_id} approved.", "/admin/payments")


@router.post("/payments/{payment_id}/reject")
def reject(request: Request, payment_id: int, comment: str = Form(""),
           me: Staff = Depends(staff_perm("finance.approve")), db: Session = Depends(get_db)):
    p = get_or_404(db, PaymentRequest, payment_id)
    ensure_visible(db, me, p.client_id)
    return attempt(request, lambda: svc.reject(db, me, p, comment.strip()), f"Payment #{payment_id} rejected.",
                   "/admin/payments")
