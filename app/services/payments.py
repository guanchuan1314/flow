"""Deposits and withdrawals with a maker-checker approval workflow.

* Deposit -> on approval funds go to the wallet, or straight to an MT5 account.
* Wallet withdrawal -> funds are held (debited) when requested, refunded on rejection.
* MT5 withdrawal -> balance is taken from MT5 on approval (free margin checked then).
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.audit import audit
from app.core.errors import Forbidden, ServiceError
from app.db import utcnow
from app.models import (
    AccountPurpose, AccountStatus, Client, KycStatus, PaymentKind, PaymentMethod, PaymentRequest, PaymentStatus,
    Staff, WalletTxKind,
)
from app.mt5 import MT5Error, MT5Gateway
from app.services import accounts, campaigns, notifications, wallet

log = logging.getLogger(__name__)


def _check_login(db: Session, client: Client, login: int | None):
    if login is None:
        return
    acc = accounts.get_client_account(db, client, login)
    if acc.is_demo or acc.status != AccountStatus.active or acc.purpose == AccountPurpose.pamm_master:
        raise ServiceError("Payments are only possible for active live trading accounts")


def check_kyc(client: Client) -> None:
    if get_settings().live_requires_kyc and client.kyc_status != KycStatus.approved:
        raise Forbidden("Identity verification (KYC) must be approved before deposits or withdrawals")


def create_request(db: Session, actor, client: Client, kind: PaymentKind, amount, *, method: str = "manual",
                   trading_login: int | None = None, promo_code: str | None = None,
                   external_ref: str | None = None, details: str = "",
                   payment_method: PaymentMethod | None = None,
                   proof: tuple[str, bytes] | None = None) -> PaymentRequest:
    """`payment_method`: a configured method (its rules are checked). `proof`: (filename, content)."""
    from app.services import clients, payment_methods

    amount = wallet.money(amount)
    if amount <= 0:
        raise ServiceError("Amount must be positive")
    check_kyc(client)
    if payment_method is not None:
        payment_methods.check_request(payment_method, kind, amount, get_settings().base_currency, proof is not None)
        method = payment_method.name
    _check_login(db, client, trading_login)
    if promo_code:
        if kind != PaymentKind.deposit or not trading_login:
            raise ServiceError("Promo codes apply only to deposits into a trading account")
        campaigns.validate_code(db, promo_code, client.id, amount)
    req = PaymentRequest(client_id=client.id, kind=kind, amount=amount, method=method[:64],
                         method_id=payment_method.id if payment_method else None,
                         trading_login=trading_login, promo_code=promo_code.upper() if promo_code else None,
                         external_ref=external_ref, details=details,
                         created_by_staff_id=actor.id if isinstance(actor, Staff) else None)
    if proof is not None:
        req.proof_path = clients.store_upload("payments", client.id, proof[0], proof[1])
        req.proof_name = proof[0][:255]
    db.add(req)
    db.flush()
    if kind == PaymentKind.withdrawal and trading_login is None:
        wallet.post(db, client.id, -amount, WalletTxKind.withdrawal, ref_type="payment", ref_id=req.id,
                    comment="Withdrawal hold")
    audit(db, actor, f"payment.{kind.value}.create", "payment", req.id,
          {"amount": amount, "login": trading_login, "method": method})
    db.commit()
    return req


def lock(db: Session, req: PaymentRequest) -> PaymentRequest:
    """Lock the request row and reload it. Every decision on a request (approve, reject, provider
    callback) takes this lock first, so two of them can't both see it pending."""
    return db.scalar(select(PaymentRequest).where(PaymentRequest.id == req.id).with_for_update()
                     .execution_options(populate_existing=True))


def approve(db: Session, gw: MT5Gateway | None, staff: Staff, req: PaymentRequest,
            comment: str | None = None) -> PaymentRequest:
    """`gw` may be None: it is then resolved from the target account's server."""
    req = lock(db, req)
    if req.status != PaymentStatus.pending:
        raise ServiceError(f"Request is already {req.status.value}")
    if req.created_by_staff_id == staff.id:
        raise Forbidden("Maker-checker: you cannot approve a request you created")
    return _execute(db, gw, staff, req, comment)


def approve_paid(db: Session, req: PaymentRequest, comment: str) -> PaymentRequest:
    """Credit a deposit the payment provider confirmed as paid (no staff approval needed)."""
    req = lock(db, req)
    if req.kind != PaymentKind.deposit or req.status != PaymentStatus.pending:
        raise ServiceError("Only a pending deposit can be credited")
    return _execute(db, None, None, req, comment)


def _execute(db: Session, gw: MT5Gateway | None, staff: Staff | None, req: PaymentRequest,
             comment: str | None) -> PaymentRequest:
    req.reviewed_by_staff_id, req.reviewed_at, req.review_comment = staff.id if staff else None, utcnow(), comment
    if gw is None and req.trading_login:
        from app.mt5 import for_login

        gw = for_login(db, req.trading_login)
    try:
        if req.kind == PaymentKind.deposit:
            if req.trading_login:
                req.mt5_deal_ticket = gw.balance_operation(req.trading_login, req.amount, f"Deposit #{req.id}")
            else:
                wallet.post(db, req.client_id, req.amount, WalletTxKind.deposit, ref_type="payment",
                            ref_id=req.id, comment=f"Deposit via {req.method}", staff_id=staff.id if staff else None)
        elif req.trading_login:
            req.mt5_deal_ticket = gw.balance_operation(req.trading_login, -req.amount, f"Withdrawal #{req.id}")
        # wallet withdrawals were already held at creation
    except MT5Error as exc:
        db.rollback()
        req = db.get(PaymentRequest, req.id)
        req.status, req.reviewed_by_staff_id, req.reviewed_at = PaymentStatus.failed, staff.id if staff else None, utcnow()
        req.review_comment = f"MT5 error: {exc}"[:255]
        audit(db, staff, "payment.failed", "payment", req.id, {"error": str(exc)})
        db.commit()
        raise ServiceError(f"MT5 operation failed: {exc}", 502) from exc
    req.status = PaymentStatus.approved
    audit(db, staff, "payment.approve", "payment", req.id, {"amount": req.amount, "deal": req.mt5_deal_ticket})
    db.commit()  # the deposit is final; a bonus failure below must not undo it
    _notify(db, req, "approved")
    if req.kind == PaymentKind.deposit and req.promo_code:
        try:
            campaigns.apply_deposit_bonus(db, gw, req)
            db.commit()
        except (MT5Error, ServiceError) as exc:
            db.rollback()
            log.warning("bonus for payment %s not applied: %s", req.id, exc)
            audit(db, staff, "payment.bonus_failed", "payment", req.id, {"error": str(exc)})
            db.commit()
    return req


def reject(db: Session, staff: Staff | None, req: PaymentRequest, comment: str) -> PaymentRequest:
    """`staff` None: rejected by the system (e.g. the payment provider reported a failure)."""
    req = lock(db, req)
    if req.status != PaymentStatus.pending:
        raise ServiceError(f"Request is already {req.status.value}")
    if not comment:
        raise ServiceError("A rejection comment is required")
    if req.kind == PaymentKind.withdrawal and req.trading_login is None:
        wallet.post(db, req.client_id, req.amount, WalletTxKind.adjustment, ref_type="payment", ref_id=req.id,
                    comment="Withdrawal rejected - hold released", staff_id=staff.id if staff else None)
    req.status, req.reviewed_by_staff_id, req.reviewed_at, req.review_comment = (
        PaymentStatus.rejected, staff.id if staff else None, utcnow(), comment)
    audit(db, staff, "payment.reject", "payment", req.id, {"comment": comment})
    db.commit()
    _notify(db, req, "rejected", comment)
    return req


def _notify(db: Session, req: PaymentRequest, outcome: str, reason: str | None = None) -> None:
    target = f"MT5 account {req.trading_login}" if req.trading_login else "your wallet"
    body = f"Your {req.kind.value} of {req.amount} {req.currency} ({target}) was {outcome}."
    if reason:
        body += f"\nReason: {reason}"
    notifications.notify_client(db.get(Client, req.client_id), f"{req.kind.value.title()} {outcome}", body)
