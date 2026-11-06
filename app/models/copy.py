"""Copy trading: MAM / social copy (separate follower accounts) and PAMM (pooled account)."""

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, TimestampMixin, utcnow

Money = Numeric(18, 2)
Lots = Numeric(18, 4)
Units = Numeric(28, 8)
Pct = Numeric(7, 4)


class StrategyStatus(str, enum.Enum):
    active = "active"
    paused = "paused"
    closed = "closed"


class AllocationMode(str, enum.Enum):
    equity_ratio = "equity_ratio"  # follower lots = master lots * (follower equity / master equity) * ratio
    balance_ratio = "balance_ratio"
    lot_multiplier = "lot_multiplier"  # follower lots = master lots * ratio
    fixed_lot = "fixed_lot"  # follower lots = ratio


class SubscriptionStatus(str, enum.Enum):
    active = "active"
    paused = "paused"  # no new opens, still mirrors closes
    stopped = "stopped"


class CopyStrategy(Base, TimestampMixin):
    """A master account whose trades are mirrored onto follower accounts (MAM)."""

    __tablename__ = "copy_strategies"
    __table_args__ = (UniqueConstraint("server_id", "master_login", name="uq_copy_strategies_server_master_login"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(String(512), default="")
    owner_client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    server_id: Mapped[int] = mapped_column(ForeignKey("mt5_servers.id"))
    master_login: Mapped[int] = mapped_column(BigInteger, index=True)
    status: Mapped[StrategyStatus] = mapped_column(Enum(StrategyStatus), default=StrategyStatus.active)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True)
    performance_fee_pct: Mapped[Decimal] = mapped_column(Pct, default=Decimal("20"))
    min_investment: Mapped[Decimal] = mapped_column(Money, default=Decimal("100"))


class CopySubscription(Base, TimestampMixin):
    __tablename__ = "copy_subscriptions"
    __table_args__ = (UniqueConstraint("strategy_id", "follower_login"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("copy_strategies.id"), index=True)
    follower_client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("mt5_servers.id"))  # the strategy's: followers share it
    follower_login: Mapped[int] = mapped_column(BigInteger, index=True)
    allocation_mode: Mapped[AllocationMode] = mapped_column(Enum(AllocationMode), default=AllocationMode.equity_ratio)
    ratio: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("1"))
    max_lot: Mapped[Decimal | None] = mapped_column(Lots)
    status: Mapped[SubscriptionStatus] = mapped_column(Enum(SubscriptionStatus), default=SubscriptionStatus.active)
    # Performance-fee high-water mark, in follower-equity terms. Adjusted by deposits/withdrawals.
    high_water_mark: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    fees_paid: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    last_fee_at: Mapped[datetime | None] = mapped_column(DateTime)

    strategy: Mapped[CopyStrategy] = relationship()


class CopyLink(Base):
    """Maps a master position to the follower position opened to mirror it."""

    __tablename__ = "copy_links"
    __table_args__ = (UniqueConstraint("subscription_id", "master_position_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("copy_subscriptions.id"), index=True)
    master_position_id: Mapped[int] = mapped_column(BigInteger, index=True)
    follower_position_id: Mapped[int] = mapped_column(BigInteger)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[int] = mapped_column(Integer)  # 0 buy, 1 sell
    master_volume_open: Mapped[Decimal] = mapped_column(Lots)
    follower_volume_open: Mapped[Decimal] = mapped_column(Lots)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CopyExecution(Base):
    """One row per (subscription, master deal): makes the copier idempotent and auditable."""

    __tablename__ = "copy_executions"
    __table_args__ = (UniqueConstraint("subscription_id", "master_deal_ticket"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("copy_subscriptions.id"), index=True)
    master_deal_ticket: Mapped[int] = mapped_column(BigInteger)
    follower_login: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(16))  # open | close
    symbol: Mapped[str] = mapped_column(String(32))
    volume: Mapped[Decimal] = mapped_column(Lots)
    status: Mapped[str] = mapped_column(String(16))  # done | failed | skipped
    retcode: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(String(255), default="")
    follower_deal_ticket: Mapped[int | None] = mapped_column(BigInteger)


# ---------------------------------------------------------------- PAMM


class PammFund(Base, TimestampMixin):
    """Pooled fund: investors own units of a single master account. NAV = equity / units."""

    __tablename__ = "pamm_funds"
    __table_args__ = (UniqueConstraint("server_id", "master_login", name="uq_pamm_funds_server_master_login"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(String(512), default="")
    manager_client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    server_id: Mapped[int] = mapped_column(ForeignKey("mt5_servers.id"))
    master_login: Mapped[int] = mapped_column(BigInteger, index=True)
    status: Mapped[StrategyStatus] = mapped_column(Enum(StrategyStatus), default=StrategyStatus.active)
    performance_fee_pct: Mapped[Decimal] = mapped_column(Pct, default=Decimal("20"))
    management_fee_pct_annual: Mapped[Decimal] = mapped_column(Pct, default=Decimal("0"))
    min_investment: Mapped[Decimal] = mapped_column(Money, default=Decimal("100"))
    total_units: Mapped[Decimal] = mapped_column(Units, default=Decimal("0"))
    last_nav: Mapped[Decimal] = mapped_column(Units, default=Decimal("1"))
    last_fee_at: Mapped[datetime | None] = mapped_column(DateTime)


class PammInvestment(Base, TimestampMixin):
    __tablename__ = "pamm_investments"
    __table_args__ = (UniqueConstraint("fund_id", "client_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    fund_id: Mapped[int] = mapped_column(ForeignKey("pamm_funds.id"), index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    units: Mapped[Decimal] = mapped_column(Units, default=Decimal("0"))
    net_invested: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    # NAV per unit above which performance fee is charged
    high_water_mark: Mapped[Decimal] = mapped_column(Units, default=Decimal("0"))
    is_manager: Mapped[bool] = mapped_column(Boolean, default=False)


class PammTxKind(str, enum.Enum):
    deposit = "deposit"
    withdrawal = "withdrawal"
    performance_fee = "performance_fee"
    management_fee = "management_fee"


class PammTransaction(Base):
    __tablename__ = "pamm_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    fund_id: Mapped[int] = mapped_column(ForeignKey("pamm_funds.id"), index=True)
    investment_id: Mapped[int] = mapped_column(ForeignKey("pamm_investments.id"), index=True)
    kind: Mapped[PammTxKind] = mapped_column(Enum(PammTxKind))
    amount: Mapped[Decimal] = mapped_column(Money)
    units: Mapped[Decimal] = mapped_column(Units)  # signed, from the investor's perspective
    nav: Mapped[Decimal] = mapped_column(Units)


class PammNavSnapshot(Base):
    __tablename__ = "pamm_nav_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    fund_id: Mapped[int] = mapped_column(ForeignKey("pamm_funds.id"), index=True)
    time: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    equity: Mapped[Decimal] = mapped_column(Money)
    total_units: Mapped[Decimal] = mapped_column(Units)
    nav: Mapped[Decimal] = mapped_column(Units)
