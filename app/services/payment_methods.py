"""Payment methods clients can deposit / withdraw with.

* manual: the client pays outside the CRM (bank transfer, ...) following the method's instructions,
  optionally uploading a proof of payment; finance approves the request (maker-checker).
* custom: an external payment provider. The CRM sends the client to a unique payment URL obtained
  from the provider (see services.integrations) and the provider's callback marks the deposit paid.
  With auto-credit on, a "paid" callback for the exact amount credits the deposit at once. The paid
  amount (at `amount_field`) is required then; a callback without it, or with another amount or
  currency (a "currency" field, when the provider sends one), is held for finance instead.
  Setting the callback secret or turning auto-credit on needs finance.approve as well as finance.methods.
  Withdrawals with a custom method are paid out by finance, as for manual methods.
"""

import logging
import secrets
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.audit import audit
from app.core.errors import Conflict, ServiceError
from app.core.security import encrypt_secret
from app.db import utcnow
from app.models import CUSTOM, MANUAL, Client, PaymentKind, PaymentMethod, PaymentRequest, PaymentStatus
from app.services import integrations

log = logging.getLogger(__name__)
SCOPE = "payment"
SECRET_FIELDS = ("secret", "api_key")
CURRENCY_FIELD = "currency"  # optional field in the provider's callback, compared with the request's currency


def methods_for(db: Session, kind: PaymentKind) -> list[PaymentMethod]:
    flag = PaymentMethod.allow_deposit if kind == PaymentKind.deposit else PaymentMethod.allow_withdrawal
    return list(db.scalars(select(PaymentMethod).where(PaymentMethod.enabled.is_(True), flag.is_(True))
                           .order_by(PaymentMethod.sort_order, PaymentMethod.name)))


def _apply(m: PaymentMethod, fields: dict) -> None:
    secrets_in = {k: fields.pop(k, None) for k in SECRET_FIELDS}
    for k, v in fields.items():
        setattr(m, k, v)
    if secrets_in["secret"]:
        m.secret_enc = encrypt_secret(secrets_in["secret"])
    if secrets_in["api_key"]:
        m.api_key_enc = encrypt_secret(secrets_in["api_key"])
    if m.kind not in (MANUAL, CUSTOM):
        raise ServiceError("Type must be manual or custom")
    if not (m.allow_deposit or m.allow_withdrawal):
        raise ServiceError("Allow deposits, withdrawals or both")
    if m.min_amount is not None and m.max_amount is not None and m.min_amount > m.max_amount:
        raise ServiceError("Minimum amount is above the maximum")
    if m.is_custom and m.allow_deposit:
        if not (m.api_url or m.redirect_template):
            raise ServiceError("A custom method needs the provider's API URL or a redirect URL template")
        if not m.secret_enc:
            raise ServiceError("A custom method needs the shared secret used to sign callbacks")
        if m.auto_credit and not (m.amount_field or "").strip():
            raise ServiceError("Auto-credit needs the amount field, so the paid amount can be checked")
    if m.api_url:
        integrations.check_url(m.api_url, resolve=False)


def _sensitive(db: Session, actor, m: PaymentMethod, fields: dict, was_auto: bool) -> None:
    """Changing the callback secret or turning auto-credit on lets callbacks move money: it needs
    finance.approve too, and is audited on its own."""
    secret_set = bool(fields.get("secret"))
    auto_on = bool(m.auto_credit) and m.is_custom and not was_auto
    if secret_set:
        integrations.require_perm(db, actor, "finance.approve", "Changing the callback secret")
    if auto_on:
        integrations.require_perm(db, actor, "finance.approve", "Turning auto-credit on")
    if secret_set:
        audit(db, actor, "payment_method.secret_change", "payment_method", m.id, {"name": m.name})
    if auto_on:
        audit(db, actor, "payment_method.auto_credit_on", "payment_method", m.id, {"name": m.name})


def create_method(db: Session, actor, **fields) -> PaymentMethod:
    if db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == fields["name"])):
        raise Conflict("A payment method with this name already exists")
    m = PaymentMethod(callback_token=integrations.new_token(), **integrations.PROVIDER_DEFAULTS, enabled=True,
                      allow_deposit=True, allow_withdrawal=False, currency=get_settings().base_currency,
                      requires_proof=False, auto_credit=True, amount_field="amount", icon="bank", sort_order=100)
    secret_set = bool(fields.get("secret"))
    _apply(m, fields)
    db.add(m)
    db.flush()
    _sensitive(db, actor, m, {"secret": secret_set}, was_auto=False)
    audit(db, actor, "payment_method.create", "payment_method", m.id, {"name": m.name, "kind": m.kind})
    db.commit()
    return m


def update_method(db: Session, actor, m: PaymentMethod, fields: dict) -> PaymentMethod:
    if db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == fields.get("name", m.name),
                                                PaymentMethod.id != m.id)):
        raise Conflict("A payment method with this name already exists")
    was_auto, secret_set = bool(m.auto_credit) and m.is_custom, bool(fields.get("secret"))
    _apply(m, fields)
    _sensitive(db, actor, m, {"secret": secret_set}, was_auto)
    audit(db, actor, "payment_method.update", "payment_method", m.id,
          {k: v for k, v in fields.items() if k not in SECRET_FIELDS})
    db.commit()
    return m


def new_callback_url(db: Session, actor, m: PaymentMethod) -> PaymentMethod:
    """Replace the callback URL (the old one stops working at once)."""
    m.callback_token = integrations.new_token()
    audit(db, actor, "payment_method.new_callback_url", "payment_method", m.id, {})
    db.commit()
    return m


def delete_method(db: Session, actor, m: PaymentMethod) -> None:
    used = db.scalar(select(func.count()).select_from(PaymentRequest).where(PaymentRequest.method_id == m.id))
    if used:
        raise ServiceError(f"{used} payment request(s) used this method; disable it instead to keep their history.")
    audit(db, actor, "payment_method.delete", "payment_method", m.id, {"name": m.name})
    db.delete(m)
    db.commit()


def check_request(m: PaymentMethod, kind: PaymentKind, amount: Decimal, currency: str, has_proof: bool) -> None:
    if not m.enabled:
        raise ServiceError(f"{m.name} is not available at the moment")
    if kind == PaymentKind.deposit and not m.allow_deposit or kind == PaymentKind.withdrawal and not m.allow_withdrawal:
        raise ServiceError(f"{m.name} cannot be used for {kind.value}s")
    if m.currency and currency and m.currency != currency:
        raise ServiceError(f"{m.name} accepts {m.currency} only")
    if m.min_amount is not None and amount < m.min_amount:
        raise ServiceError(f"The minimum for {m.name} is {m.min_amount} {m.currency}")
    if m.max_amount is not None and amount > m.max_amount:
        raise ServiceError(f"The maximum for {m.name} is {m.max_amount} {m.currency}")
    if kind == PaymentKind.deposit and not m.is_custom and m.requires_proof and not has_proof:
        raise ServiceError("Please attach your proof of payment (receipt or bank confirmation)")


# ---------------------------------------------------------------- custom provider round trip
def start_checkout(db: Session, req: PaymentRequest, m: PaymentMethod, client: Client, return_url: str) -> str:
    """Get the provider's unique payment URL for this deposit; the client is redirected there."""
    req.gateway_ref = req.gateway_ref or f"P{req.id}-{secrets.token_hex(6)}"
    payload = {"reference": req.gateway_ref, "amount": str(req.amount), "currency": req.currency,
               "description": f"Deposit #{req.id}", "client_id": client.id, "email": client.email,
               "first_name": client.first_name, "last_name": client.last_name, "country": client.country or "",
               "callback_url": integrations.callback_url(SCOPE, m.callback_token), "return_url": return_url}
    try:
        url = integrations.start(m, payload)
    except ServiceError as exc:
        req.status, req.gateway_status, req.review_comment = PaymentStatus.failed, "failed", exc.message[:255]
        audit(db, None, "payment.checkout_failed", "payment", req.id, {"error": exc.message})
        db.commit()
        raise ServiceError(f"{m.name} is not available right now. Please try again later or choose another method.",
                           502) from exc
    req.gateway_url, req.gateway_status = url[:1024], "redirected"
    audit(db, None, "payment.checkout", "payment", req.id, {"method": m.name, "reference": req.gateway_ref})
    db.commit()
    return url


def handle_callback(db: Session, token: str, raw: bytes, headers, content_type: str, ip: str | None) -> tuple[int, str]:
    """Process a provider callback. Returns (HTTP status, message) for the provider."""
    from app.services import payments

    m = db.scalar(select(PaymentMethod).where(PaymentMethod.callback_token == token))
    if m is None or not m.is_custom:
        return 404, "unknown callback URL"
    data = integrations.parse_body(raw, content_type)
    ref = integrations.field(data, m.ref_field)
    ok = integrations.signature_ok(m, raw, headers)

    def done(code: int, result: str) -> tuple[int, str]:
        integrations.log_callback(db, SCOPE, m.id, ref, ip, ok, result, raw, data)
        db.commit()
        return code, result

    if not ok:
        return done(401, "invalid signature")
    req = db.scalar(select(PaymentRequest).where(PaymentRequest.gateway_ref == str(ref), PaymentRequest.method_id == m.id))
    if req is None:
        return done(404, f"unknown reference {ref}")
    req = payments.lock(db, req)  # a staff decision or a parallel callback waits for this one
    state, reason = integrations.outcome(m, data)
    if req.status != PaymentStatus.pending:
        return done(200, f"already {req.status.value}")
    if req.gateway_status in ("paid", "mismatch"):  # held for finance: later callbacks don't override that
        return done(200, f"already {req.gateway_status}, waiting for finance")
    if state == "pending":
        return done(200, "noted, still pending")
    if state == "failure":
        req.gateway_status = "failed"
        payments.reject(db, None, req, f"Payment not completed at {m.name}: {reason}")
        return done(200, "marked failed")
    paid = integrations.field(data, m.amount_field) if m.amount_field else None
    currency = integrations.field(data, CURRENCY_FIELD)
    req.external_ref = req.external_ref or str(integrations.field(data, "transaction_id") or ref)[:128]
    req.paid_at = utcnow()
    problem = _paid_problem(m, req, paid, currency)
    if problem:
        req.gateway_status = "mismatch"
        audit(db, None, "payment.amount_mismatch", "payment", req.id,
              {"expected": req.amount, "paid": paid, "currency": currency, "problem": problem})
        return done(200, f"{problem}: held for review")
    req.gateway_status = "paid"
    db.flush()  # approve_paid reloads the row: keep the provider's details (transaction id, paid time)
    if not m.auto_credit:
        audit(db, None, "payment.paid", "payment", req.id, {"method": m.name})
        return done(200, "paid, waiting for finance approval")
    try:
        payments.approve_paid(db, req, f"Paid via {m.name} (provider confirmation)")
    except ServiceError as exc:
        return done(200, f"paid, but crediting failed: {exc.message}")
    return done(200, "paid and credited")


def _paid_problem(m: PaymentMethod, req: PaymentRequest, paid, currency) -> str | None:
    """Why a "paid" callback can't be trusted as is (None when it matches the request).

    The amount must be there when the method checks it (always with auto-credit) and must parse."""
    if currency is not None and str(currency).strip().upper() != (req.currency or "").upper():
        return f"paid currency {currency} differs from {req.currency}"
    if paid is None:
        return "paid amount missing" if m.amount_field or m.auto_credit else None
    try:
        if Decimal(str(paid).strip()) == req.amount:
            return None
    except ArithmeticError:
        return f"paid amount {str(paid)[:32]!r} is not a number"
    return f"paid amount {paid} differs from {req.amount}"
