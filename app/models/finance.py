import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, TimestampMixin

Money = Numeric(18, 2)


class Wallet(Base, TimestampMixin):
    """CRM-side client wallet. Funds are moved between it and MT5 accounts."""

    __tablename__ = "wallets"
    __table_args__ = (UniqueConstraint("client_id", "currency"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    balance: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))


class WalletTxKind(str, enum.Enum):
    deposit = "deposit"
    withdrawal = "withdrawal"
    to_mt5 = "to_mt5"
    from_mt5 = "from_mt5"
    ib_commission = "ib_commission"
    pamm_invest = "pamm_invest"
    pamm_redeem = "pamm_redeem"
    copy_fee = "copy_fee"
    adjustment = "adjustment"


class WalletTransaction(Base):
    """Append-only ledger. Wallet.balance must always equal the sum of its rows."""

    __tablename__ = "wallet_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), index=True)
    kind: Mapped[WalletTxKind] = mapped_column(Enum(WalletTxKind))
    amount: Mapped[Decimal] = mapped_column(Money)  # signed
    balance_after: Mapped[Decimal] = mapped_column(Money)
    ref_type: Mapped[str | None] = mapped_column(String(32))
    ref_id: Mapped[str | None] = mapped_column(String(64))
    comment: Mapped[str] = mapped_column(String(255), default="")
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))


class PaymentKind(str, enum.Enum):
    deposit = "deposit"
    withdrawal = "withdrawal"


class PaymentStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"  # approved, funds moved
    rejected = "rejected"
    failed = "failed"  # approved but MT5 operation failed


class PaymentRequest(Base, TimestampMixin):
    """Deposit/withdrawal request with a maker-checker approval workflow.

    Target is either the client's wallet (trading_login is NULL) or an MT5 account.
    """

    __tablename__ = "payment_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    kind: Mapped[PaymentKind] = mapped_column(Enum(PaymentKind))
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    method: Mapped[str] = mapped_column(String(64), default="manual")  # the method's name when requested
    method_id: Mapped[int | None] = mapped_column(ForeignKey("payment_methods.id"), index=True)
    trading_login: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus), default=PaymentStatus.pending)
    promo_code: Mapped[str | None] = mapped_column(String(32))
    external_ref: Mapped[str | None] = mapped_column(String(128))
    details: Mapped[str] = mapped_column(String(512), default="")
    created_by_staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    reviewed_by_staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    review_comment: Mapped[str | None] = mapped_column(String(255))
    mt5_deal_ticket: Mapped[int | None] = mapped_column(Integer)
    # manual methods: the client's proof of payment (stored like KYC documents)
    proof_path: Mapped[str | None] = mapped_column(String(512))
    proof_name: Mapped[str | None] = mapped_column(String(255))
    # custom methods: the provider round trip
    gateway_ref: Mapped[str | None] = mapped_column(String(64), unique=True)  # our unique order reference
    gateway_url: Mapped[str | None] = mapped_column(String(1024))  # where the client was sent to pay
    gateway_status: Mapped[str | None] = mapped_column(String(16))  # redirected | paid | failed | mismatch
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
