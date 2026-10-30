from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import ServiceError
from app.db import utcnow
from app.models import Wallet, WalletTransaction, WalletTxKind

CENT = Decimal("0.01")


def money(v) -> Decimal:
    return Decimal(str(v)).quantize(CENT, rounding=ROUND_HALF_UP)


def get_wallet(db: Session, client_id: int, currency: str | None = None, lock: bool = False) -> Wallet:
    currency = currency or get_settings().base_currency
    q = select(Wallet).where(Wallet.client_id == client_id, Wallet.currency == currency)
    if lock:
        q = q.with_for_update()
    wallet = db.scalar(q)
    if wallet is None:
        wallet = Wallet(client_id=client_id, currency=currency, balance=Decimal("0"))
        db.add(wallet)
        db.flush()
    return wallet


def post(db: Session, client_id: int, amount, kind: WalletTxKind, *, ref_type: str | None = None,
         ref_id=None, comment: str = "", staff_id: int | None = None, allow_negative: bool = False,
         currency: str | None = None) -> WalletTransaction:
    """Apply a signed amount to a client's wallet and append a ledger row. Caller commits."""
    amount = money(amount)
    if amount == 0:
        raise ServiceError("Amount must not be zero")
    wallet = get_wallet(db, client_id, currency, lock=True)
    new_balance = money(wallet.balance) + amount
    if new_balance < 0 and not allow_negative:
        raise ServiceError("Insufficient wallet balance")
    wallet.balance = new_balance
    tx = WalletTransaction(created_at=utcnow(), wallet_id=wallet.id, kind=kind, amount=amount,
                           balance_after=new_balance, ref_type=ref_type,
                           ref_id=None if ref_id is None else str(ref_id), comment=comment, staff_id=staff_id)
    db.add(tx)
    db.flush()
    return tx
