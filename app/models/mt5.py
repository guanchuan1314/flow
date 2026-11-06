import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON, BigInteger, Boolean, DateTime, Enum, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, TimestampMixin

Money = Numeric(18, 2)


class ServerKind(str, enum.Enum):
    manager = "manager"  # real MT5 server through the Manager API
    simulator = "simulator"  # built-in simulator (development / demos only)


class MT5Server(Base, TimestampMixin):
    """An MT5 server the CRM connects to with a manager account. Managed in the admin UI."""

    __tablename__ = "mt5_servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)  # internal label, e.g. "Live London"
    kind: Mapped[ServerKind] = mapped_column(Enum(ServerKind), default=ServerKind.manager)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)  # live or demo trading environment
    address: Mapped[str] = mapped_column(String(255), default="")  # host:port
    manager_login: Mapped[int] = mapped_column(BigInteger, default=0)
    manager_password_enc: Mapped[str] = mapped_column(String(512), default="")  # Fernet-encrypted, never shown
    # Server name clients pick in the MT5 terminal, e.g. "YourBroker-Live"
    terminal_name: Mapped[str] = mapped_column(String(64), default="")
    timeout_ms: Mapped[int] = mapped_column(Integer, default=30000)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_price_feed: Mapped[bool] = mapped_column(Boolean, default=False)  # source of website quotes
    notes: Mapped[str] = mapped_column(String(512), default="")
    last_status: Mapped[str] = mapped_column(String(16), default="unknown")  # ok | error | unknown
    last_error: Mapped[str | None] = mapped_column(String(512))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Import of groups (-> account groups) and users (-> trading accounts) from this server
    groups_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    accounts_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_sync_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    # The worker's last poll of this server: accounts polled, how long it took (Server health page)
    poll_stats: Mapped[dict] = mapped_column(JSON, default=dict)
    # Demo clean-up settings and last run: {enabled, days, action, groups, last_run, last_result}
    demo_cleanup: Mapped[dict] = mapped_column(JSON, default=dict)
    # Trader styles: {ea: account group id, manual: account group id} the styles belong in
    style_groups: Mapped[dict] = mapped_column(JSON, default=dict)
    # Symbols & pricing: {auto_sync: {enabled, minutes, last_run, last_result}, spread: [{groups, symbols, points}],
    # commission: [{groups, symbols, per_lot}], swap: [{symbols, long, short}], applied: {group: [paths]}, last_apply}
    pricing: Mapped[dict] = mapped_column(JSON, default=dict)

    @property
    def display_name(self) -> str:
        return self.terminal_name or self.name


class AccountGroup(Base, TimestampMixin):
    """A product offered to clients, mapped onto an MT5 group on one server."""

    __tablename__ = "account_groups"
    __table_args__ = (UniqueConstraint("server_id", "mt5_group", name="uq_account_groups_server_group"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("mt5_servers.id"), index=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    mt5_group: Mapped[str] = mapped_column(String(128))  # e.g. "real\\standard"
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    leverage_options: Mapped[list] = mapped_column(JSON, default=lambda: [100, 200, 500])
    default_leverage: Mapped[int] = mapped_column(Integer, default=100)
    min_deposit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    demo_initial_balance: Mapped[Decimal] = mapped_column(Money, default=Decimal("10000"))
    # Spread markup configured on the MT5 group, in points. Drives spread-share IB rebates.
    spread_markup_points: Mapped[int] = mapped_column(Integer, default=0)
    max_accounts_per_client: Mapped[int] = mapped_column(Integer, default=5)
    # Kept equal to the MT5 group's settings (written on save, read back on every import).
    open_on_register: Mapped[bool] = mapped_column(Boolean, default=False)  # demo: opened at sign-up
    margin_call: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    stop_out: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    source: Mapped[str] = mapped_column(String(8), default="crm")  # crm | mt5 (imported from a group)
    on_server: Mapped[bool] = mapped_column(Boolean, default=True)  # False: group no longer exists on MT5
    synced_at: Mapped[datetime | None] = mapped_column(DateTime)

    server: Mapped[MT5Server] = relationship(lazy="joined")


class AccountStatus(str, enum.Enum):
    active = "active"
    disabled = "disabled"  # login blocked on MT5
    archived = "archived"


class AccountPurpose(str, enum.Enum):
    trading = "trading"
    copy_master = "copy_master"
    pamm_master = "pamm_master"


class TradingAccount(Base, TimestampMixin):
    __tablename__ = "trading_accounts"
    __table_args__ = (UniqueConstraint("server_id", "login", name="uq_trading_accounts_server_login"),
                      Index("ix_trading_accounts_server_activity", "server_id", "last_activity_at"),
                      Index("ix_trading_accounts_server_state", "server_id", "on_server", "status"))

    id: Mapped[int] = mapped_column(primary_key=True)
    # A login belongs to its server: two servers can have the same one, so an account is (server_id, login).
    login: Mapped[int] = mapped_column(BigInteger, index=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("mt5_servers.id"), index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    account_group_id: Mapped[int | None] = mapped_column(ForeignKey("account_groups.id"))
    group: Mapped[str] = mapped_column(String(128))
    leverage: Mapped[int] = mapped_column(Integer, default=100)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[AccountStatus] = mapped_column(Enum(AccountStatus), default=AccountStatus.active)
    trading_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    purpose: Mapped[AccountPurpose] = mapped_column(Enum(AccountPurpose), default=AccountPurpose.trading)

    # Cached from MT5 by the sync worker
    balance: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    credit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    equity: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    margin: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    margin_free: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    margin_level: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(8), default="crm")  # crm | mt5 (imported from the server)
    on_server: Mapped[bool] = mapped_column(Boolean, default=True)  # False: user deleted on MT5
    # Activity, kept by the worker: the last terminal connection (MT5's LastAccess, read from the Manager API's
    # local copy of the accounts) and the last trade (from the deals); last_activity_at is the later of the two
    # (or the registration). The inactive accounts list is a query on it.
    last_access_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_trade_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)  # last import that found it

    account_group: Mapped[AccountGroup | None] = relationship()
    server: Mapped[MT5Server] = relationship(lazy="joined")

    @property
    def ref(self) -> str:
        """How pages and links name the account: server:login."""
        return f"{self.server_id}:{self.login}"


class Symbol(Base, TimestampMixin):
    """The CRM's copy of an MT5 symbol. Every create, update and delete is applied on MT5 first."""

    __tablename__ = "symbols"
    __table_args__ = (UniqueConstraint("server_id", "name", name="uq_symbols_server_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("mt5_servers.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(String(255), default="")  # MT5 folder + name, e.g. Forex\EURUSD
    description: Mapped[str] = mapped_column(String(255), default="")
    source: Mapped[str] = mapped_column(String(64), default="")
    currency_base: Mapped[str] = mapped_column(String(16), default="")
    currency_profit: Mapped[str] = mapped_column(String(16), default="")
    currency_margin: Mapped[str] = mapped_column(String(16), default="")
    digits: Mapped[int] = mapped_column(Integer, default=5)
    contract_size: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=Decimal("100000"))
    calc_mode: Mapped[int] = mapped_column(Integer, default=0)
    trade_mode: Mapped[int] = mapped_column(Integer, default=4)
    exec_mode: Mapped[int] = mapped_column(Integer, default=2)
    volume_min: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.01"))
    volume_max: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("100"))
    volume_step: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.01"))
    spread: Mapped[int] = mapped_column(Integer, default=0)
    stops_level: Mapped[int] = mapped_column(Integer, default=0)
    freeze_level: Mapped[int] = mapped_column(Integer, default=0)
    swap_mode: Mapped[int] = mapped_column(Integer, default=0)
    swap_long: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0"))
    swap_short: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0"))
    swap_3day: Mapped[int] = mapped_column(Integer, default=3)
    margin_initial: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=Decimal("0"))
    margin_maintenance: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=Decimal("0"))
    margin_hedged: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=Decimal("0"))
    sessions: Mapped[dict] = mapped_column(JSON, default=dict)  # {"quote"|"trade": 7 days of [open, close] minutes}
    # the swap markup the CRM applied: {lp_long, lp_short, long, short} (the LP's swaps and the points taken off)
    swap_markup: Mapped[dict | None] = mapped_column(JSON)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime)

    server: Mapped[MT5Server] = relationship()


class RoutingRule(Base, TimestampMixin):
    """The CRM's copy of an MT5 routing rule; `position` is MT5's order (0 = checked first)."""

    __tablename__ = "routing_rules"
    __table_args__ = (UniqueConstraint("server_id", "name", name="uq_routing_rules_server_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("mt5_servers.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    position: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    types: Mapped[int] = mapped_column(BigInteger, default=255)
    requests: Mapped[int] = mapped_column(BigInteger, default=0)
    action: Mapped[int] = mapped_column(Integer, default=0)
    param: Mapped[str] = mapped_column(String(255), default="")
    param_type: Mapped[int] = mapped_column(Integer, default=0)
    conditions: Mapped[list] = mapped_column(JSON, default=list)
    dealers: Mapped[list] = mapped_column(JSON, default=list)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime)

    server: Mapped[MT5Server] = relationship()


class ServerHealthSample(Base):
    """What one server's components reported at one time, plus the CRM's own poll of it. Kept for a week."""

    __tablename__ = "server_health_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("mt5_servers.id"), index=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    components: Mapped[list] = mapped_column(JSON, default=list)  # MT5Component fields per component
    poll: Mapped[dict] = mapped_column(JSON, default=dict)  # MT5Server.poll_stats at the time
    error: Mapped[str | None] = mapped_column(String(512))  # MT5 could not be read


class LiquidityConnection(Base, TimestampMixin):
    """The CRM's copy of an MT5 gateway (orders to a liquidity provider) or data feed (its quotes).
    Every create, update and delete is applied on MT5 first. Passwords are never stored."""

    __tablename__ = "liquidity_connections"
    __table_args__ = (UniqueConstraint("server_id", "kind", "name", name="uq_liquidity_connections_server_kind_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("mt5_servers.id"), index=True)
    kind: Mapped[str] = mapped_column(String(8))  # gateway | feed
    name: Mapped[str] = mapped_column(String(64))
    module: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    lp_server: Mapped[str] = mapped_column(String(255), default="")
    lp_login: Mapped[str] = mapped_column(String(128), default="")
    gateway_server: Mapped[str] = mapped_column(String(255), default="")
    gateway_login: Mapped[int] = mapped_column(BigInteger, default=0)
    mt5_id: Mapped[int] = mapped_column(Integer, default=0)
    gateway_type: Mapped[str] = mapped_column(String(64), default="")
    flags: Mapped[int] = mapped_column(BigInteger, default=0)
    keywords: Mapped[str] = mapped_column(String(255), default="")
    categories: Mapped[str] = mapped_column(String(255), default="")
    timeout_reconnect: Mapped[int] = mapped_column(Integer, default=0)
    timeout_sleep: Mapped[int] = mapped_column(Integer, default=0)
    timeout_attempts: Mapped[int] = mapped_column(Integer, default=0)
    symbols: Mapped[list] = mapped_column(JSON, default=list)
    groups: Mapped[list] = mapped_column(JSON, default=list)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    translations: Mapped[list] = mapped_column(JSON, default=list)
    state: Mapped[dict] = mapped_column(JSON, default=dict)  # last seen: connected, ticks received, …
    synced_at: Mapped[datetime | None] = mapped_column(DateTime)

    server: Mapped[MT5Server] = relationship()
