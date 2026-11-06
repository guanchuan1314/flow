"""Broker-agnostic interface to the MT5 server.

Everything outside `app.mt5` talks to MT5 only through `MT5Gateway`, so the real
Manager API (`ManagerGateway`) and the in-memory simulator (`MockGateway`) are
interchangeable. Volumes are always in lots, money in account currency.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

BUY, SELL = 0, 1
RET_PARAMS = 3  # MT_RET_ERR_PARAMS
RET_NOT_FOUND = 13  # MT_RET_ERR_NOTFOUND


class MT5Error(Exception):
    def __init__(self, message: str, retcode: int | None = None):
        super().__init__(f"{message} (retcode={retcode})" if retcode is not None else message)
        self.retcode = retcode


@dataclass
class MT5User:
    login: int
    group: str
    leverage: int
    name: str = ""
    email: str = ""
    phone: str = ""
    country: str = ""
    enabled: bool = True
    trading_enabled: bool = True
    first_name: str = ""
    last_name: str = ""
    city: str = ""
    address: str = ""
    comment: str = ""
    registration: datetime | None = None
    last_access: datetime | None = None  # last connection from a terminal (MT5's LastAccess)


@dataclass
class MT5Group:
    """An MT5 group (Administrator > Groups); the CRM maps it to an account group."""

    name: str  # e.g. "real\\standard"
    currency: str = "USD"
    company: str = ""
    margin_call: Decimal = Decimal("0")  # margin level %
    stop_out: Decimal = Decimal("0")
    demo_leverage: int = 0
    demo_deposit: Decimal = Decimal("0")
    demo_inactivity: int = 0  # days after which MT5 itself removes inactive demo accounts (0 = never)
    spreads: dict = field(default_factory=dict)  # symbol path -> spread difference in points (only those set)
    # {name, path, value, per ("lot" | "deal"), entry}: the group's commissions (first tier); value > 0 is charged
    commissions: list = field(default_factory=list)


COMPONENT_TYPES = {0: "Main trade server", 1: "Trade server", 2: "History server", 3: "Access server",
                   4: "Backup server", 7: "Anti-DDoS server", 8: "Matching server"}


@dataclass
class MT5Component:
    """One component of the MT5 platform (trade, history, access, backup server…) with the load MT5 reports
    for it. The peaks are MT5's recent maxima; the *_critical values are the warning levels set in MT5."""

    id: int
    name: str
    type: int  # COMPONENT_TYPES
    address: str = ""
    connected: bool = True
    build: int = 0
    os: str = ""
    cpu: str = ""
    cores: int = 0
    cpu_max: int = 0  # %
    cpu_critical: int = 0
    memory_total: int = 0  # MB
    memory_free: int = 0
    memory_free_min: int = 0
    memory_free_critical: int = 0
    disk_total: int = 0  # MB
    disk_free: int = 0
    disk_free_critical: int = 0
    disk_read: int = 0  # MB/s
    disk_read_critical: int = 0
    disk_write: int = 0
    disk_write_critical: int = 0
    connections_max: int = 0
    connections_critical: int = 0
    network_max: int = 0  # KB/s
    network_critical: int = 0
    last_boot: datetime | None = None


@dataclass
class MT5AccountInfo:
    login: int
    balance: Decimal
    credit: Decimal
    equity: Decimal
    margin: Decimal
    margin_free: Decimal
    margin_level: Decimal


@dataclass
class MT5Deal:
    ticket: int
    login: int
    time: datetime
    time_msc: int
    action: int
    entry: int
    symbol: str
    volume: Decimal
    price: Decimal
    profit: Decimal
    commission: Decimal = Decimal("0")
    swap: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    position_id: int = 0
    order: int = 0
    reason: int = 0
    comment: str = ""
    contract_size: Decimal = Decimal("100000")
    tick_value: Decimal = Decimal("1")
    tick_size: Decimal = Decimal("0.00001")
    digits: int = 5


@dataclass
class MT5Position:
    position_id: int
    login: int
    symbol: str
    action: int
    volume: Decimal
    price_open: Decimal
    price_current: Decimal
    sl: Decimal
    tp: Decimal
    profit: Decimal
    swap: Decimal
    time_create: datetime
    time_update: datetime | None = None
    comment: str = ""


@dataclass
class MT5Order:
    """An MT5 order: open (pending) or from the history (filled, cancelled, expired, ...)."""

    ticket: int
    login: int
    symbol: str
    type: int  # IMTOrder::EnOrderType: 0 buy, 1 sell, 2 buy limit, 3 sell limit, 4 buy stop, 5 sell stop, ...
    state: int  # IMTOrder::EnOrderState: 1 placed, 2 cancelled, 3 partial, 4 filled, 5 rejected, 6 expired, ...
    volume_initial: Decimal
    volume_current: Decimal
    price_order: Decimal
    price_current: Decimal = Decimal("0")
    sl: Decimal = Decimal("0")
    tp: Decimal = Decimal("0")
    time_setup: datetime | None = None
    time_done: datetime | None = None
    position_id: int = 0
    reason: int = 0
    comment: str = ""


@dataclass
class MT5FirewallRule:
    """An entry of the MT5 server firewall (Administrator > Firewall): applies to every MT5 connection."""

    from_ip: str
    to_ip: str
    action: str  # block | allow
    comment: str = ""


@dataclass
class MT5Symbol:
    """The symbol settings the CRM manages (Symbols in MT5 Administrator). Volumes are in lots."""

    name: str
    path: str = ""
    description: str = ""
    source: str = ""  # symbol whose quotes this one uses ("" = its own feed)
    currency_base: str = ""
    currency_profit: str = ""
    currency_margin: str = ""
    digits: int = 5
    contract_size: Decimal = Decimal("100000")
    calc_mode: int = 0  # IMTConSymbol::EnCalcMode, 0 = Forex
    trade_mode: int = 4  # EnTradeMode: 0 disabled, 1 long only, 2 short only, 3 close only, 4 full
    exec_mode: int = 2  # EnExecutionMode: 0 request, 1 instant, 2 market, 3 exchange
    volume_min: Decimal = Decimal("0.01")
    volume_max: Decimal = Decimal("100")
    volume_step: Decimal = Decimal("0.01")
    spread: int = 0  # fixed spread in points, 0 = floating
    stops_level: int = 0
    freeze_level: int = 0
    swap_mode: int = 0  # EnSwapMode, 0 = disabled, 1 = in points
    swap_long: Decimal = Decimal("0")
    swap_short: Decimal = Decimal("0")
    swap_3day: int = 3  # weekday charged triple swap, 0 = Sunday … 6 = Saturday
    margin_initial: Decimal = Decimal("0")
    margin_maintenance: Decimal = Decimal("0")
    margin_hedged: Decimal = Decimal("0")
    # {"quote": [...], "trade": [...]}: per weekday (0 = Sunday), a list of [open, close] in minutes
    # from midnight (server time). Empty = leave the sessions as they are on MT5.
    sessions: dict = field(default_factory=dict)


@dataclass
class MT5Route:
    """A routing rule (Routing in MT5 Administrator). MT5 applies the enabled rules from the top: the
    first one whose order types, requests and conditions all match decides what happens."""

    name: str
    enabled: bool = True
    types: int = 255  # IMTConRoute::EnTypeFlags, order types it covers (255 = all)
    requests: int = 33554431  # EnRouteFlags, request kinds it covers (REQUEST_ALL)
    action: int = 1006  # EnRouteAction (1006 = confirm at the market price)
    param: str = ""  # the action's value, e.g. a delay in ms ("" = none)
    param_type: int = 0  # EnConditionType of `param`
    # {condition: EnRouteCondition, rule: EnConditionRule, type: EnConditionType, value: str}; times are "HH:MM",
    # volumes in lots
    conditions: list[dict] = field(default_factory=list)
    dealers: list[dict] = field(default_factory=list)  # {login, name}: dealers or gateways to send requests to


LINK_KINDS = ("gateway", "feed")  # MT5 gateways (trade execution at an LP) and data feeds (quotes)


@dataclass
class MT5Link:
    """A liquidity connection: an MT5 gateway (orders to an LP) or data feed (quotes from an LP)."""

    kind: str  # gateway | feed
    name: str
    module: str  # module file, e.g. MetaTrader5Gateway64.exe
    enabled: bool = True
    server: str = ""  # the LP's address (gateway TradingServer / feed FeedServer)
    login: str = ""
    password: str | None = None  # None = leave as it is on MT5
    gateway_server: str = ""  # where the MT5 server reaches the module process
    gateway_login: int = 0
    gateway_password: str | None = None
    mt5_id: int = 0  # gateways: their numeric ID
    gateway_type: str = ""  # gateways: the module's type code, e.g. MT5GWTMT5
    flags: int = 0
    keywords: str = ""  # feeds (news)
    categories: str = ""  # feeds (news)
    timeout_reconnect: int = 0
    timeout_sleep: int = 0
    timeout_attempts: int = 0
    symbols: list[str] = field(default_factory=list)  # symbol masks served
    groups: list[str] = field(default_factory=list)  # gateways: group masks
    params: dict[str, str | None] = field(default_factory=dict)  # module parameters; None = keep MT5's value
    translations: list[dict] = field(default_factory=list)  # {source, symbol, digits, bid_markup, ask_markup}
    state: dict = field(default_factory=dict)  # read only: connected, ticks received, …


@dataclass
class MT5LinkModule:
    """A gateway or data feed module installed on the MT5 server, with its default settings."""

    kind: str
    name: str
    module: str
    gateway_type: str = ""
    description: str = ""
    vendor: str = ""
    version: int = 0
    server: str = ""
    login: str = ""
    params: dict[str, str] = field(default_factory=dict)


@dataclass
class TradeResult:
    retcode: int
    deal_ticket: int = 0
    order: int = 0
    position_id: int = 0
    volume: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    comment: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.retcode in (10008, 10009, 10010)  # PLACED, DONE, DONE_PARTIAL


DealCallback = Callable[[MT5Deal], None]
TradeCallback = Callable[[str, int], None]  # "position" or "order", and the login whose ones changed


class MT5Gateway(ABC):
    def on(self, server_id: int) -> MT5Gateway:
        """The gateway of that server. A server's gateway is its own; the routing gateway finds it."""
        return self

    # -- connection
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @property
    def connections(self) -> int:
        """How many times this gateway has connected: a new number means the link dropped and came back, and
        whatever happened in between was not pushed."""
        return 1

    def ensure_connected(self) -> None:
        """Reconnect if the link was lost. Raises MT5Error if the server is unreachable."""
        if not self.connected:
            self.connect()

    # -- users / accounts
    @abstractmethod
    def create_user(
        self, *, group: str, leverage: int, name: str, email: str, phone: str, country: str,
        master_password: str, investor_password: str, login: int = 0,
    ) -> int:
        """Create an MT5 user and return its login."""

    @abstractmethod
    def get_user(self, login: int) -> MT5User: ...

    @abstractmethod
    def update_user(
        self, login: int, *, leverage: int | None = None, group: str | None = None,
        enabled: bool | None = None, trading_enabled: bool | None = None,
        name: str | None = None, email: str | None = None, phone: str | None = None,
        first_name: str | None = None, last_name: str | None = None, country: str | None = None,
        city: str | None = None, address: str | None = None,
    ) -> MT5User: ...

    @abstractmethod
    def delete_user(self, login: int) -> None:
        """Delete the MT5 user (the login and its history on the server)."""

    @abstractmethod
    def get_last_access(self) -> dict[int, datetime | None]:
        """Every account's last terminal connection (None = never), read from the Manager API's local copy of the
        accounts, so it costs the server nothing."""

    @abstractmethod
    def archive_users(self, logins: list[int]) -> dict[int, str]:
        """Move users to MT5's archive database (they can no longer log in; MT5 Administrator can restore them).
        Returns the logins MT5 refused, with the reason."""

    @abstractmethod
    def change_password(self, login: int, password: str, investor: bool = False) -> None: ...

    @abstractmethod
    def check_password(self, login: int, password: str, investor: bool = False) -> bool: ...

    @abstractmethod
    def get_accounts(self, logins: list[int]) -> list[MT5AccountInfo]: ...

    def get_account(self, login: int) -> MT5AccountInfo:
        accounts = self.get_accounts([login])
        if not accounts:
            raise MT5Error(f"account {login} not found")
        return accounts[0]

    # -- money
    @abstractmethod
    def balance_operation(self, login: int, amount: Decimal, comment: str, credit: bool = False) -> int:
        """Deposit (amount > 0) or withdraw (amount < 0). Returns the MT5 deal ticket."""

    # -- history / positions
    @abstractmethod
    def get_deals(self, logins: list[int], date_from: datetime, date_to: datetime) -> list[MT5Deal]: ...

    @abstractmethod
    def get_positions(self, logins: list[int]) -> list[MT5Position]: ...

    @abstractmethod
    def get_orders(self, logins: list[int]) -> list[MT5Order]:
        """Open (pending) orders."""

    @abstractmethod
    def get_order_history(self, logins: list[int], date_from: datetime, date_to: datetime) -> list[MT5Order]:
        """Orders that are no longer open (filled, cancelled, rejected, expired), set up in the period."""

    # -- trading (dealer)
    @abstractmethod
    def delete_orders(self, tickets: list[int]) -> dict[int, str]:
        """Delete orders from the server, open or in the history. A ticket MT5 no longer has counts as deleted.
        Returns the tickets MT5 refused, with the reason."""

    @abstractmethod
    def delete_deals(self, tickets: list[int]) -> dict[int, str]:
        """Delete deals from the server (the account's balance is not recalculated). A ticket MT5 no longer has
        counts as deleted. Returns the tickets MT5 refused, with the reason."""

    @abstractmethod
    def rewrite_deal(self, ticket: int, *, action: int, amount: Decimal, comment: str, when: datetime,
                     symbol: str = "") -> None:
        """Turn a deal into a plain money deal of this type and amount. The account's balance is not changed."""

    @abstractmethod
    def server_time(self) -> datetime:
        """The time on the server's clock, the one deals are dated by."""

    @abstractmethod
    def check_balance(self, login: int) -> tuple[bool, Decimal, Decimal]:
        """Compare the account's balance and credit with its history, changing nothing:
        (they match, balance, sum of the history)."""

    @abstractmethod
    def open_position(self, login: int, symbol: str, side: int, volume: Decimal, comment: str = "") -> TradeResult: ...

    @abstractmethod
    def close_position(
        self, login: int, position_id: int, symbol: str, side: int, volume: Decimal, comment: str = ""
    ) -> TradeResult:
        """Close `volume` lots of a position. `side` is the side of the *position*."""

    # -- server configuration
    @abstractmethod
    def get_groups(self, mask: str = "*") -> list[MT5Group]:
        """Groups visible to the manager account."""

    @abstractmethod
    def get_users_in_group(self, group: str) -> list[MT5User]:
        """All users (trading accounts) of one group."""

    @abstractmethod
    def get_group(self, name: str) -> MT5Group | None:
        """One group, or None when the server doesn't have it."""

    @abstractmethod
    def create_group(self, name: str, template: str, currency: str | None = None) -> MT5Group:
        """New group copied from `template` (symbols, margin, commissions), renamed to `name`."""

    @abstractmethod
    def update_group(self, name: str, *, margin_call: Decimal | None = None, stop_out: Decimal | None = None,
                     demo_leverage: int | None = None, demo_deposit: Decimal | None = None,
                     demo_inactivity: int | None = None) -> MT5Group:
        """Change the group settings the CRM manages; None leaves a setting as it is."""

    @abstractmethod
    def set_group_pricing(self, name: str, spreads: dict[str, int | None], commissions: list[dict],
                          prefix: str) -> MT5Group:
        """Set the group's spread difference per symbol path (None = back to the symbol's own), and replace its
        commissions whose names start with `prefix` by `commissions` ({name, path, per_lot}: money per lot,
        charged on every deal in and out). Other commissions are kept."""

    @abstractmethod
    def delete_group(self, name: str) -> None:
        """Remove a group that has no accounts left."""

    # -- firewall (MT5 Administrator API: needs a login with administrator access)
    @abstractmethod
    def get_firewall(self) -> list[MT5FirewallRule]: ...

    @abstractmethod
    def put_firewall_rule(self, tag: str, rule: MT5FirewallRule) -> None:
        """Add `rule`, replacing the entries whose comment starts with `tag`."""

    @abstractmethod
    def delete_firewall_rule(self, tag: str) -> int:
        """Remove the entries whose comment starts with `tag`. Returns how many were removed."""

    # -- symbols (Administrator API)
    @abstractmethod
    def get_symbols(self) -> list[MT5Symbol]: ...

    @abstractmethod
    def put_symbol(self, symbol: MT5Symbol, template: str | None = None) -> MT5Symbol:
        """With `template`: create the symbol as a copy of that one (sessions, margin rates, flags),
        then apply `symbol`'s settings. Without: update the existing symbol."""

    @abstractmethod
    def delete_symbol(self, name: str) -> None: ...

    # -- liquidity: gateways and data feeds (Administrator API)
    @abstractmethod
    def get_links(self, kind: str) -> list[MT5Link]: ...

    @abstractmethod
    def put_link(self, link: MT5Link, create: bool = False) -> MT5Link:
        """Create (`create`) or update the gateway / data feed called `link.name`."""

    @abstractmethod
    def delete_link(self, kind: str, name: str) -> None: ...

    @abstractmethod
    def get_link_modules(self, kind: str) -> list[MT5LinkModule]:
        """The gateway / data feed modules installed on the server."""

    # -- routing rules (Administrator API), in MT5's order
    @abstractmethod
    def get_routes(self) -> list[MT5Route]: ...

    @abstractmethod
    def put_route(self, route: MT5Route, create: bool = False) -> MT5Route:
        """Create (`create`, added at the bottom) or update the rule called `route.name`."""

    @abstractmethod
    def delete_route(self, name: str) -> None: ...

    @abstractmethod
    def move_route(self, name: str, position: int) -> None:
        """Move the rule to `position` (0 = top); the others keep their order."""

    # -- platform health
    @abstractmethod
    def get_server_health(self) -> list[MT5Component]:
        """Every component of the platform with its recent load, as MT5 Administrator shows it."""

    @abstractmethod
    def restart_component(self, component_id: int) -> None:
        """Restart one platform component (trade, access, history server…), as in MT5 Administrator. Its
        connections drop while it restarts; restarting the main trade server drops every client."""

    @abstractmethod
    def restart_links(self, kind: str) -> None:
        """Restart every gateway (`gateway`) or every data feed (`feed`) of the server: MT5 has no restart of
        a single one."""

    # -- market data
    @abstractmethod
    def get_quotes(self, symbols: list[str]) -> dict[str, tuple[Decimal, Decimal]]:
        """Latest (bid, ask) per symbol; unknown symbols are omitted."""

    # -- push events
    @abstractmethod
    def subscribe_deals(self, callback: DealCallback) -> None: ...

    @abstractmethod
    def unsubscribe_deals(self, callback: DealCallback) -> None: ...

    def subscribe_trades(self, callback: TradeCallback) -> None:
        """Be told when an account's open positions or pending orders change. Say nothing about what changed:
        reading the account again cannot get the order of events wrong."""
        raise MT5Error("this gateway does not push position and order changes")

    def unsubscribe_trades(self, callback: TradeCallback) -> None:
        pass
