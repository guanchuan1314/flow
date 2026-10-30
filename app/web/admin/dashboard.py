from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db, utcnow
from app.models import (
    AccountStatus, Client, KycStatus, PaymentKind, PaymentRequest, PaymentStatus, Position, Staff, SyncState,
    TradingAccount,
)
from app.services.clients import visible_client_ids
from app.web.common import render, web_staff


def dashboard(request: Request, staff: Staff = Depends(web_staff), db: Session = Depends(get_db)):
    client_ids = visible_client_ids(staff)
    logins = select(TradingAccount.login).where(TradingAccount.client_id.in_(client_ids),
                                                TradingAccount.is_demo.is_(False))
    now = utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def count(q):
        return db.scalar(q) or 0

    def money_sum(kind):
        return db.scalar(select(func.coalesce(func.sum(PaymentRequest.amount), 0)).where(
            PaymentRequest.client_id.in_(client_ids), PaymentRequest.kind == kind,
            PaymentRequest.status == PaymentStatus.approved, PaymentRequest.reviewed_at >= month_start)) or 0

    kpi = {
        "clients": count(select(func.count()).select_from(Client).where(Client.id.in_(client_ids))),
        "new_clients": count(select(func.count()).select_from(Client).where(
            Client.id.in_(client_ids), Client.created_at >= now - timedelta(days=7))),
        "live_accounts": count(select(func.count()).select_from(TradingAccount).where(
            TradingAccount.login.in_(logins), TradingAccount.status == AccountStatus.active)),
        "equity": db.scalar(select(func.coalesce(func.sum(TradingAccount.equity), 0)).where(TradingAccount.login.in_(logins))),
        "deposits_month": money_sum(PaymentKind.deposit),
        "withdrawals_month": money_sum(PaymentKind.withdrawal),
        "open_positions": count(select(func.count()).select_from(Position).where(
            Position.login.in_(logins), Position.is_open.is_(True))),
        "floating": db.scalar(select(func.coalesce(func.sum(Position.profit), 0)).where(
            Position.login.in_(logins), Position.is_open.is_(True))),
        "sync_errors": count(select(func.count()).select_from(SyncState).where(SyncState.last_error.is_not(None))),
    }
    pending_payments = db.scalars(select(PaymentRequest).where(
        PaymentRequest.client_id.in_(client_ids), PaymentRequest.status == PaymentStatus.pending)
        .order_by(PaymentRequest.id).limit(10)).all()
    pending_kyc = db.scalars(select(Client).where(Client.id.in_(client_ids), Client.kyc_status == KycStatus.pending)
                             .order_by(Client.updated_at).limit(10)).all()
    recent = db.scalars(select(Client).where(Client.id.in_(client_ids)).order_by(Client.id.desc()).limit(8)).all()
    names = {c.id: c for c in db.scalars(select(Client).where(Client.id.in_({p.client_id for p in pending_payments})))}
    return render(request, "admin/dashboard.html", kpi=kpi, pending_payments=pending_payments,
                  pending_kyc=pending_kyc, recent=recent, names=names)
