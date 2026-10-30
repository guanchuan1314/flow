import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON, BigInteger, Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, TimestampMixin, utcnow

Money = Numeric(18, 2)


class CommissionPlan(Base, TimestampMixin):
    """Rebate plan for IBs.

    `level_shares` splits each computed rebate up the IB tree, e.g. [0.7, 0.2, 0.1]:
    70% to the direct IB, 20% to its parent, 10% to the grandparent.
    """

    __tablename__ = "commission_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    level_shares: Mapped[list] = mapped_column(JSON, default=lambda: [1.0])
    trigger: Mapped[str] = mapped_column(String(8), default="close")  # close | open | both
    min_hold_seconds: Mapped[int] = mapped_column(Integer, default=0)  # anti-scalping rule
    rules: Mapped[list["CommissionRule"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", order_by="CommissionRule.priority"
    )


class RuleKind(str, enum.Enum):
    per_lot = "per_lot"  # value = USD per lot
    spread_share = "spread_share"  # value = fraction (0..1) of the account group's spread markup
    commission_pct = "commission_pct"  # value = % of the MT5 commission charged on the deal


class CommissionRule(Base):
    __tablename__ = "commission_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("commission_plans.id", ondelete="CASCADE"), index=True)
    symbol_pattern: Mapped[str] = mapped_column(String(32), default="*")  # fnmatch: "XAU*", "EURUSD"
    account_group_id: Mapped[int | None] = mapped_column(ForeignKey("account_groups.id"))
    kind: Mapped[RuleKind] = mapped_column(Enum(RuleKind))
    value: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    priority: Mapped[int] = mapped_column(Integer, default=100)  # lower wins


class IBStatus(str, enum.Enum):
    pending = "pending"
    active = "active"
    suspended = "suspended"


class IBProfile(Base, TimestampMixin):
    __tablename__ = "ib_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), unique=True)
    parent_ib_id: Mapped[int | None] = mapped_column(ForeignKey("ib_profiles.id"), index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("commission_plans.id"))
    status: Mapped[IBStatus] = mapped_column(Enum(IBStatus), default=IBStatus.active)

    plan: Mapped[CommissionPlan | None] = relationship()


class IBCommission(Base):
    __tablename__ = "ib_commissions"
    __table_args__ = (UniqueConstraint("server_id", "deal_ticket", "ib_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("mt5_servers.id"), index=True)
    deal_ticket: Mapped[int] = mapped_column(BigInteger, index=True)
    ib_id: Mapped[int] = mapped_column(ForeignKey("ib_profiles.id"), index=True)
    level: Mapped[int] = mapped_column(Integer)
    trader_client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    login: Mapped[int] = mapped_column(BigInteger)
    symbol: Mapped[str] = mapped_column(String(32))
    volume: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    amount: Mapped[Decimal] = mapped_column(Money)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("commission_rules.id"))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | paid | void
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)


class Campaign(Base, TimestampMixin):
    """Promo code / deposit-bonus campaign. Bonus is paid as MT5 credit."""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    bonus_pct: Mapped[Decimal] = mapped_column(Numeric(7, 2), default=Decimal("0"))
    max_bonus: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    min_deposit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    starts_at: Mapped[datetime | None] = mapped_column(DateTime)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime)
    max_redemptions: Mapped[int | None] = mapped_column(Integer)
    once_per_client: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class CampaignRedemption(Base):
    __tablename__ = "campaign_redemptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    payment_request_id: Mapped[int] = mapped_column(ForeignKey("payment_requests.id"), unique=True)
    login: Mapped[int] = mapped_column(BigInteger)
    bonus_amount: Mapped[Decimal] = mapped_column(Money)
