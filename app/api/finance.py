from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import gateway, require
from app.core.errors import ServiceError, get_or_404
from app.db import get_db
from app.models import PaymentKind, PaymentRequest, PaymentStatus, Staff
from app.mt5 import MT5Gateway
from app.schemas import PaymentOut, ReviewIn, StaffPaymentIn
from app.services import payments as svc
from app.services.accounts import ref_of
from app.services.clients import ensure_visible, visible_client_ids

router = APIRouter(prefix="/payments", tags=["finance"])


@router.get("", response_model=list[PaymentOut])
def list_payments(status: PaymentStatus | None = None, kind: PaymentKind | None = None, client_id: int | None = None,
                  limit: int = Query(100, le=1000), offset: int = 0,
                  db: Session = Depends(get_db), me: Staff = Depends(require("finance.view"))):
    q = select(PaymentRequest).where(PaymentRequest.client_id.in_(visible_client_ids(me)))
    if status:
        q = q.where(PaymentRequest.status == status)
    if kind:
        q = q.where(PaymentRequest.kind == kind)
    if client_id:
        q = q.where(PaymentRequest.client_id == client_id)
    return db.scalars(q.order_by(PaymentRequest.id.desc()).limit(limit).offset(offset)).all()


@router.post("", response_model=PaymentOut, status_code=201)
def create_payment(body: StaffPaymentIn, db: Session = Depends(get_db), me: Staff = Depends(require("finance.create"))):
    client = ensure_visible(db, me, body.client_id)
    data = body.model_dump(exclude={"client_id", "kind", "amount", "server_id"})
    data["trading_login"] = ref_of(body.trading_login, body.server_id)
    return svc.create_request(db, me, client, body.kind, body.amount, **data)


@router.get("/{payment_id}", response_model=PaymentOut)
def get_payment(payment_id: int, db: Session = Depends(get_db), me: Staff = Depends(require("finance.view"))):
    req = get_or_404(db, PaymentRequest, payment_id)
    ensure_visible(db, me, req.client_id)
    return req


@router.post("/{payment_id}/approve", response_model=PaymentOut)
def approve(payment_id: int, body: ReviewIn, db: Session = Depends(get_db), gw: MT5Gateway = Depends(gateway),
            me: Staff = Depends(require("finance.approve"))):
    req = get_or_404(db, PaymentRequest, payment_id)
    ensure_visible(db, me, req.client_id)
    return svc.approve(db, gw, me, req, body.comment)


@router.post("/{payment_id}/reject", response_model=PaymentOut)
def reject(payment_id: int, body: ReviewIn, db: Session = Depends(get_db), me: Staff = Depends(require("finance.approve"))):
    if not body.comment:
        raise ServiceError("A rejection comment is required")
    req = get_or_404(db, PaymentRequest, payment_id)
    ensure_visible(db, me, req.client_id)
    return svc.reject(db, me, req, body.comment)
