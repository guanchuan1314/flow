from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow

Money = Numeric(18, 2)
Lots = Numeric(18, 4)
Price = Numeric(20, 8)

# MT5 deal actions / entries (IMTDeal::EnDealAction / EnDealEntry)
DEAL_BUY, DEAL_SELL, DEAL_BALANCE, DEAL_CREDIT = 0, 1, 2, 3
DEAL_CORRECTION = 5
ENTRY_IN, ENTRY_OUT, ENTRY_INOUT, ENTRY_OUT_BY = 0, 1, 2, 3
TRADE_ACTIONS = (DEAL_BUY, DEAL_SELL)
CLOSING_ENTRIES = (ENTRY_OUT, ENTRY_INOUT, ENTRY_OUT_BY)


class Deal(Base):
    """Mirror of MT5 deals. (server, ticket) is unique, which makes sync idempotent."""

    __tablename__ = "deals"
    __table_args__ = (Index("ix_deals_login_time", "login", text("time DESC")),)

    server_id: Mapped[int] = mapped_column(ForeignKey("mt5_servers.id"), primary_key=True)
    ticket: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    login: Mapped[int] = mapped_column(BigInteger, index=True)
    time: Mapped[datetime] = mapped_column(DateTime, index=True)
    time_msc: Mapped[int] = mapped_column(BigInteger, default=0)
    action: Mapped[int] = mapped_column(Integer)
    entry: Mapped[int] = mapped_column(Integer)
    symbol: Mapped[str] = mapped_column(String(32), default="")
    volume: Mapped[Decimal] = mapped_column(Lots, default=Decimal("0"))
    price: Mapped[Decimal] = mapped_column(Price, default=Decimal("0"))
    profit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    commission: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    swap: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    fee: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    position_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    order: Mapped[int] = mapped_column(BigInteger, default=0)
    reason: Mapped[int] = mapped_column(Integer, default=0)
    comment: Mapped[str] = mapped_column(String(64), default="")
    contract_size: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=Decimal("0"))
    tick_value: Mapped[Decimal] = mapped_column(Price, default=Decimal("0"))
    tick_size: Mapped[Decimal] = mapped_column(Price, default=Decimal("0"))
    digits: Mapped[int] = mapped_column(Integer, default=5)
    synced_at: Mapped[datetime] = mapped_column(DateTime)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (Index("ix_positions_open_login", "server_id", "login", postgresql_where=text("is_open")),)

    server_id: Mapped[int] = mapped_column(ForeignKey("mt5_servers.id"), primary_key=True)
    position_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    login: Mapped[int] = mapped_column(BigInteger, index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    action: Mapped[int] = mapped_column(Integer)  # 0 buy, 1 sell
    volume: Mapped[Decimal] = mapped_column(Lots)
    price_open: Mapped[Decimal] = mapped_column(Price)
    price_current: Mapped[Decimal] = mapped_column(Price, default=Decimal("0"))
    sl: Mapped[Decimal] = mapped_column(Price, default=Decimal("0"))
    tp: Mapped[Decimal] = mapped_column(Price, default=Decimal("0"))
    profit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    swap: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    time_create: Mapped[datetime] = mapped_column(DateTime)
    time_update: Mapped[datetime | None] = mapped_column(DateTime)
    comment: Mapped[str] = mapped_column(String(64), default="")
    is_open: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    synced_at: Mapped[datetime] = mapped_column(DateTime)


class Order(Base):
    """Mirror of MT5 orders: open (pending) orders and the order history."""

    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_open_login", "server_id", "login", postgresql_where=text("is_open")),)

    server_id: Mapped[int] = mapped_column(ForeignKey("mt5_servers.id"), primary_key=True)
    ticket: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    login: Mapped[int] = mapped_column(BigInteger, index=True)
    symbol: Mapped[str] = mapped_column(String(32), default="")
    type: Mapped[int] = mapped_column(Integer)  # see ORDER_TYPES
    state: Mapped[int] = mapped_column(Integer)  # see ORDER_STATES
    volume_initial: Mapped[Decimal] = mapped_column(Lots, default=Decimal("0"))
    volume_current: Mapped[Decimal] = mapped_column(Lots, default=Decimal("0"))
    price_order: Mapped[Decimal] = mapped_column(Price, default=Decimal("0"))
    price_current: Mapped[Decimal] = mapped_column(Price, default=Decimal("0"))
    sl: Mapped[Decimal] = mapped_column(Price, default=Decimal("0"))
    tp: Mapped[Decimal] = mapped_column(Price, default=Decimal("0"))
    time_setup: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    time_done: Mapped[datetime | None] = mapped_column(DateTime)
    position_id: Mapped[int] = mapped_column(BigInteger, default=0)
    reason: Mapped[int] = mapped_column(Integer, default=0)
    comment: Mapped[str] = mapped_column(String(64), default="")
    is_open: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime)


ORDER_TYPES = {0: "Buy", 1: "Sell", 2: "Buy limit", 3: "Sell limit", 4: "Buy stop", 5: "Sell stop",
               6: "Buy stop limit", 7: "Sell stop limit", 8: "Close by"}
ORDER_STATES = {0: "Started", 1: "Placed", 2: "Cancelled", 3: "Partial", 4: "Filled", 5: "Rejected", 6: "Expired",
                7: "Request add", 8: "Request modify", 9: "Request cancel"}


class SyncJob(Base):
    """A background "sync missing history" task: re-reads deals and orders from MT5 for a period."""

    __tablename__ = "sync_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    server_id: Mapped[int | None] = mapped_column(ForeignKey("mt5_servers.id"))  # None: every enabled server
    logins: Mapped[list] = mapped_column(JSON, default=list)  # empty: every tracked account
    date_from: Mapped[datetime] = mapped_column(DateTime)
    date_to: Mapped[datetime] = mapped_column(DateTime)
    include_orders: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(12), default="queued", index=True)  # queued|running|done|failed|cancelled
    total: Mapped[int] = mapped_column(Integer, default=0)  # work units: (server, batch of logins, period slice)
    done: Mapped[int] = mapped_column(Integer, default=0)
    deals_seen: Mapped[int] = mapped_column(Integer, default=0)
    deals_added: Mapped[int] = mapped_column(Integer, default=0)
    orders_seen: Mapped[int] = mapped_column(Integer, default=0)
    orders_added: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(String(512), default="")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running")

    @property
    def percent(self) -> int:
        return 100 if self.status == "done" else int(100 * self.done / self.total) if self.total else 0


class CompactSummary(Base):
    """One deal on MT5 that stands for a group of compacted deals (services.compact). Compacting the account again
    with the same grouping adds to it."""

    __tablename__ = "compact_summaries"
    __table_args__ = (UniqueConstraint("server_id", "login", "key", name="uq_compact_summary"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("mt5_servers.id"))
    login: Mapped[int] = mapped_column(BigInteger, index=True)
    key: Mapped[str] = mapped_column(String(128))  # type, label and symbol: what a later compact matches on
    action: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(32), default="")
    ticket: Mapped[int] = mapped_column(BigInteger)  # the MT5 deal
    date_from: Mapped[datetime] = mapped_column(DateTime)
    date_to: Mapped[datetime] = mapped_column(DateTime)
    amount: Mapped[Decimal] = mapped_column(Money)
    deals: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class CompactedRecord(Base):
    """An exact copy of a deal, order or closed position that compacting removed. They stay in Flow."""

    __tablename__ = "compacted_records"

    server_id: Mapped[int] = mapped_column(ForeignKey("mt5_servers.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(8), primary_key=True)  # deal | order | position
    ticket: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    summary_id: Mapped[int | None] = mapped_column(ForeignKey("compact_summaries.id"), index=True)
    login: Mapped[int] = mapped_column(BigInteger, index=True)
    time: Mapped[datetime | None] = mapped_column(DateTime)
    symbol: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[int] = mapped_column(Integer)  # the deal's action, order's type or position's direction
    amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    comment: Mapped[str] = mapped_column(String(64), default="")
    data: Mapped[dict] = mapped_column(JSON)  # every field, as MT5 had it
    compacted_at: Mapped[datetime] = mapped_column(DateTime)


class SyncRun(Base):
    """One synchronization from MT5 (account groups, trading accounts, trades, symbols, ...), started by a staff
    member or by the schedule; it runs in the background (services.sync_runs)."""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # services.sync_runs.KINDS
    server_id: Mapped[int | None] = mapped_column(ForeignKey("mt5_servers.id", ondelete="SET NULL"))  # None: every server
    trigger: Mapped[str] = mapped_column(String(16), default="manual", index=True)  # manual | schedule
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(12), default="queued", index=True)  # queued|running|done|failed
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    summary: Mapped[dict | None] = mapped_column(JSON)
    message: Mapped[str] = mapped_column(String(1024), default="")
    params: Mapped[dict | None] = mapped_column(JSON)  # what to do, e.g. the demo clean-up's logins and action
    total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # progress: work items, 0 = unknown
    done: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running")

    @property
    def percent(self) -> int | None:
        """How far it is (0-100), or None while it can't tell."""
        if self.status == "done":
            return 100
        return min(100, int(100 * self.done / self.total)) if self.total else None

    @property
    def seconds(self) -> float | None:
        if self.started_at is None:
            return None
        return ((self.finished_at or utcnow()) - self.started_at).total_seconds()


class SyncState(Base):
    __tablename__ = "sync_state"

    server_id: Mapped[int] = mapped_column(ForeignKey("mt5_servers.id"), primary_key=True)
    login: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    last_deal_time: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    last_deal_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_position_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_order_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(String(512))
