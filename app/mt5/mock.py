"""MT5 server simulator for development and tests.

Simulates users, balance operations, market execution at configurable prices,
partial closes, P/L, and deal push events. Profit is computed as
(price diff) * lots * contract size, i.e. it assumes USD-quoted symbols.

State is in memory; pass `state_file` to persist it across restarts (local dev).
"""

from __future__ import annotations

import json
import os
import pickle
import threading
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from app.mt5.gateway import (
    BUY, SELL, DealCallback, MT5AccountInfo, MT5Component, MT5Deal, MT5Error, MT5FirewallRule, MT5Gateway, MT5Group,
    MT5Link, MT5LinkModule, MT5Order, MT5Position, MT5Route, MT5Symbol, MT5User, TradeResult,
)

# Groups a fresh simulated server has, like a typical broker setup.
DEFAULT_GROUPS = {
    "real\\standard": "USD", "real\\ecn": "USD", "real\\pro": "USD",
    "demo\\standard": "USD", "demo\\ecn": "USD", "managers\\administrators": "USD",
}



def _group(name: str, currency: str = "USD") -> MT5Group:
    """Settings of a new simulated group."""
    demo = name.startswith("demo")
    return MT5Group(name=name, currency=currency, company="Simulated broker", margin_call=Decimal("100"),
                    stop_out=Decimal("50"), demo_leverage=500 if demo else 0,
                    demo_deposit=Decimal("10000") if demo else Decimal("0"), demo_inactivity=90 if demo else 0)


DEAL_BUY, DEAL_SELL, DEAL_BALANCE, DEAL_CREDIT = 0, 1, 2, 3
ORDER_PLACED, ORDER_CANCELLED, ORDER_FILLED = 1, 2, 4  # IMTOrder::EnOrderState
ENTRY_IN, ENTRY_OUT = 0, 1

SYMBOLS = {
    # symbol: (bid, ask, contract_size, digits)
    "EURUSD": (Decimal("1.10000"), Decimal("1.10010"), Decimal("100000"), 5),
    "GBPUSD": (Decimal("1.27000"), Decimal("1.27015"), Decimal("100000"), 5),
    "XAUUSD": (Decimal("2400.00"), Decimal("2400.30"), Decimal("100"), 2),
    "USDJPY": (Decimal("149.500"), Decimal("149.512"), Decimal("100000"), 3),
    "AUDUSD": (Decimal("0.66200"), Decimal("0.66212"), Decimal("100000"), 5),
    "XAGUSD": (Decimal("28.500"), Decimal("28.530"), Decimal("5000"), 3),
    "US30": (Decimal("39800.0"), Decimal("39802.0"), Decimal("1"), 1),
    "NAS100": (Decimal("18250.0"), Decimal("18251.5"), Decimal("1"), 1),
    "BTCUSD": (Decimal("67500.00"), Decimal("67530.00"), Decimal("1"), 2),
}


SYMBOL_PATHS = {"XAUUSD": "Metals", "XAGUSD": "Metals", "US30": "Indices", "NAS100": "Indices", "BTCUSD": "Crypto"}


def _sessions(folder: str) -> dict:
    """Typical sessions: crypto round the clock, indices with a daily break, the rest Monday to Friday."""
    if folder == "Crypto":
        week = [[[0, 1440]]] * 7
    elif folder == "Indices":
        week = [[]] + [[[60, 1335], [1350, 1440]]] * 5 + [[]]
    else:
        week = [[]] + [[[0, 1440]]] * 5 + [[]]
    return {"quote": [list(map(list, day)) for day in week], "trade": [list(map(list, day)) for day in week]}


def _symbol_spec(name: str) -> MT5Symbol:
    """Settings of a simulated symbol, like a typical broker's."""
    _, _, contract, digits = SYMBOLS[name]
    folder = SYMBOL_PATHS.get(name, "Forex")
    base, profit = (name[:3], name[3:]) if folder in ("Forex", "Metals", "Crypto") else ("USD", "USD")
    lots = Decimal("0.01") if folder != "Indices" else Decimal("0.1")
    return MT5Symbol(name=name, path=f"{folder}\\{name}",
                     description=f"{base} vs {profit}" if folder == "Forex" else name,
                     currency_base=base, currency_profit=profit, currency_margin=base if folder == "Forex" else profit,
                     digits=digits, contract_size=contract, calc_mode=0 if folder == "Forex" else 2,
                     volume_min=lots, volume_max=Decimal("100"), volume_step=lots, stops_level=10,
                     swap_mode=1, swap_long=Decimal("-5.5"), swap_short=Decimal("1.2"), margin_hedged=contract,
                     sessions=_sessions(folder))


# Gateway / data feed modules a simulated server has installed: the standard MetaQuotes set, as a real
# server (build 6230) reports it, with credential-like defaults blanked.
LINK_MODULES = [MT5LinkModule(**m) for m in json.loads((Path(__file__).with_name("link_modules.json"))
                                                       .read_text(encoding="utf-8"))]


def _default_links() -> dict[tuple[str, str], MT5Link]:
    feed = MT5Link(kind="feed", name="MetaTrader 5 Demo Feeder", module="MetaTrader5Feeder64.exe",
                   server="access.metatrader5.com:443", login="1000", gateway_server="127.0.0.1:16384",
                   gateway_login=100, flags=1, timeout_reconnect=1, timeout_sleep=5, timeout_attempts=60,
                   symbols=["*"], translations=[{"source": s, "symbol": s, "digits": d, "bid_markup": 0,
                                                 "ask_markup": 0} for s, (_, _, _, d) in SYMBOLS.items()])
    return {(feed.kind, feed.name): feed}


def _default_routes() -> list[MT5Route]:
    return [MT5Route(name="Auto Execution")]  # everything confirmed at the market price, as on a fresh server


def _copy(value):
    import copy

    return copy.deepcopy(value)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _money(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _persist(method):
    """Save state after a successful mutating call."""
    import functools

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        result = method(self, *args, **kwargs)
        self.save()
        return result

    return wrapper


class MockGateway(MT5Gateway):
    STATE_KEYS = ("seq", "users", "passwords", "balance", "credit", "positions", "deals", "prices", "groups",
                  "orders", "order_history", "firewall", "symbols", "links", "routes", "archived", "deal_reasons")

    def __init__(self, first_login: int = 500000, state_file: str | None = None):
        self._lock = threading.RLock()
        self._connected = False
        self.state_file = Path(state_file) if state_file else None
        self.seq = {"login": first_login, "ticket": 1000000, "position": 7000000, "order": 9000000}
        self.users: dict[int, MT5User] = {}
        self.passwords: dict[tuple[int, bool], str] = {}
        self.balance: dict[int, Decimal] = {}
        self.credit: dict[int, Decimal] = {}
        self.positions: dict[int, MT5Position] = {}
        self.deals: list[MT5Deal] = []
        self.prices = {s: [b, a] for s, (b, a, _, _) in SYMBOLS.items()}
        self.groups: dict[str, MT5Group] = {g: _group(g, c) for g, c in DEFAULT_GROUPS.items()}
        self.orders: dict[int, MT5Order] = {}  # open pending orders
        self.order_history: list[MT5Order] = []
        self.firewall: list[MT5FirewallRule] = []
        self.symbols: dict[str, MT5Symbol] = {s: _symbol_spec(s) for s in SYMBOLS}
        self.links: dict[tuple[str, str], MT5Link] = _default_links()
        self.routes: list[MT5Route] = _default_routes()
        self.archived: dict[int, MT5User] = {}  # MT5's archive database
        self.deal_reasons: dict[int, int] = {}  # login -> EnDealReason of its trades (default 0, the desktop terminal)
        self.down_components: set[int] = set()  # health: component ids reported as disconnected (not persisted)
        self.restarts: list[tuple[str, int | str]] = []  # ("component", id) / ("links", kind), in order (not persisted)
        self.booted: dict[int, datetime] = {}  # component id -> last restart (not persisted)
        self._subscribers: list[DealCallback] = []
        if self.state_file and self.state_file.exists():
            with self.state_file.open("rb") as f:
                for key, value in pickle.load(f).items():
                    setattr(self, key, value)
            self.seq.setdefault("order", 9000000)  # states saved before orders were simulated
            for symbol, (bid, ask, _, _) in SYMBOLS.items():  # symbols added since the state was saved
                self.prices.setdefault(symbol, [bid, ask])
            if not isinstance(getattr(self, "groups", None), dict):  # state saved before groups existed
                self.groups = dict(DEFAULT_GROUPS)
            self.groups = {g: c if isinstance(c, MT5Group) else _group(g, c)  # older states kept only the currency
                           for g, c in self.groups.items()}
            for g in self.groups.values():  # states saved before group pricing was simulated
                g.__dict__.setdefault("spreads", {})
                g.__dict__.setdefault("commissions", [])
            for user in self.users.values():
                self.groups.setdefault(user.group, _group(user.group))
            if not isinstance(getattr(self, "symbols", None), dict):  # states saved before symbols were simulated
                self.symbols = {s: _symbol_spec(s) for s in SYMBOLS}
            if not isinstance(getattr(self, "links", None), dict):
                self.links = _default_links()
            for name, spec in self.symbols.items():  # symbols saved before sessions were simulated
                if "sessions" not in vars(spec):
                    spec.sessions = _sessions(spec.path.rpartition("\\")[0] or "Forex")
            if not isinstance(getattr(self, "routes", None), list):
                self.routes = _default_routes()
            for key in ("archived", "deal_reasons"):  # states saved before these were simulated
                if not isinstance(getattr(self, key, None), dict):
                    setattr(self, key, {})
            for user in self.users.values():
                if "last_access" not in vars(user):
                    user.last_access = user.registration

    def _next(self, name: str) -> int:
        value = self.seq[name]
        self.seq[name] = value + 1
        return value

    def save(self) -> None:
        """Atomically write the simulator state (no-op without a state file)."""
        if not self.state_file:
            return
        with self._lock:
            data = pickle.dumps({k: getattr(self, k) for k in self.STATE_KEYS})
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, self.state_file)

    # ---------------------------------------------------------- connection
    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ---------------------------------------------------------- helpers
    def set_price(self, symbol: str, bid: Decimal | str, ask: Decimal | str | None = None) -> None:
        bid = Decimal(str(bid))
        ask = Decimal(str(ask)) if ask is not None else bid
        with self._lock:
            self.prices[symbol] = [bid, ask]

    def _require(self, login: int) -> MT5User:
        user = self.users.get(login)
        if not user:
            raise MT5Error(f"user {login} not found", 13)
        return user

    def _floating(self, pos: MT5Position) -> Decimal:
        bid, ask = self.prices[pos.symbol]
        contract = self._contract(pos.symbol)
        close_price = bid if pos.action == BUY else ask
        diff = (close_price - pos.price_open) if pos.action == BUY else (pos.price_open - close_price)
        return _money(diff * pos.volume * contract)

    def _emit(self, deal: MT5Deal) -> None:
        self.deals.append(deal)
        for cb in list(self._subscribers):
            cb(deal)

    def _new_deal(self, **kw) -> MT5Deal:
        now = _now()
        symbol = kw.get("symbol", "")
        spec = self.symbols.get(symbol)
        contract, digits = (spec.contract_size, spec.digits) if spec else (Decimal("0"), 2)
        tick_size = Decimal(1).scaleb(-digits)
        deal = MT5Deal(
            ticket=self._next("ticket"), time=now, time_msc=int(now.timestamp() * 1000),
            contract_size=contract, digits=digits, tick_size=tick_size,
            tick_value=tick_size * contract, **kw,
        )
        if deal.action in (DEAL_BUY, DEAL_SELL):  # every market deal comes from a filled market order
            if "reason" not in kw:
                deal.reason = self.deal_reasons.get(deal.login, 0)
            deal.order = self._next("order")
            self.order_history.append(MT5Order(
                ticket=deal.order, login=deal.login, symbol=deal.symbol, type=deal.action, state=ORDER_FILLED,
                volume_initial=deal.volume, volume_current=Decimal("0"), price_order=deal.price,
                price_current=deal.price, time_setup=now, time_done=now, position_id=deal.position_id,
                comment=deal.comment))
        return deal

    # ---------------------------------------------------------- users
    @_persist
    def create_user(self, *, group, leverage, name, email, phone, country,
                    master_password, investor_password, login=0) -> int:
        with self._lock:
            login = login or self._next("login")
            if login in self.users:
                raise MT5Error(f"login {login} already exists", 3004)
            first, _, last = name.partition(" ")
            self.users[login] = MT5User(login, group, leverage, name, email, phone, country,
                                        first_name=first, last_name=last, registration=_now(), last_access=_now())
            self.groups.setdefault(group, _group(group))  # the simulator creates unknown groups on the fly
            self.passwords[(login, False)] = master_password
            self.passwords[(login, True)] = investor_password
            self.balance[login] = Decimal("0")
            self.credit[login] = Decimal("0")
            return login

    def get_user(self, login: int) -> MT5User:
        with self._lock:
            return replace(self._require(login))

    @_persist
    def update_user(self, login, *, leverage=None, group=None, enabled=None, trading_enabled=None,
                    name=None, email=None, phone=None, first_name=None, last_name=None, country=None,
                    city=None, address=None) -> MT5User:
        with self._lock:
            user = self._require(login)
            for attr, val in dict(leverage=leverage, group=group, enabled=enabled,
                                  trading_enabled=trading_enabled, name=name, email=email, phone=phone,
                                  first_name=first_name, last_name=last_name, country=country, city=city,
                                  address=address).items():
                if val is not None:
                    setattr(user, attr, val)
            return replace(user)

    @_persist
    def delete_user(self, login: int) -> None:
        with self._lock:
            self._require(login)
            if any(p.login == login for p in self.positions.values()):
                raise MT5Error(f"account {login} has open positions", 3)
            for store in (self.users, self.balance, self.credit):
                store.pop(login, None)
            self.passwords.pop((login, False), None)
            self.passwords.pop((login, True), None)

    def get_last_access(self) -> dict[int, datetime | None]:
        with self._lock:
            return {login: u.last_access for login, u in self.users.items()}

    @_persist
    def touch(self, login: int) -> None:
        """Simulate the account connecting from a terminal."""
        with self._lock:
            self._require(login).last_access = _now()

    @_persist
    def archive_users(self, logins: list[int]) -> dict[int, str]:
        failed = {}
        with self._lock:
            for login in logins:
                if login not in self.users:
                    failed[login] = "Not found"
                elif any(p.login == login for p in self.positions.values()) or any(
                        o.login == login for o in self.orders.values()):
                    failed[login] = "Account has open positions or orders"
                else:
                    self.archived[login] = self.users.pop(login)
        return failed

    @_persist
    def change_password(self, login: int, password: str, investor: bool = False) -> None:
        with self._lock:
            self._require(login)
            self.passwords[(login, investor)] = password

    def check_password(self, login: int, password: str, investor: bool = False) -> bool:
        with self._lock:
            return self.passwords.get((login, investor)) == password

    def get_accounts(self, logins: list[int]) -> list[MT5AccountInfo]:
        out = []
        with self._lock:
            for login in logins:
                if login not in self.users:
                    continue
                floating = sum((self._floating(p) for p in self.positions.values() if p.login == login), Decimal("0"))
                margin = sum(
                    (p.volume * self._contract(p.symbol) * self.prices[p.symbol][0] / self.users[login].leverage
                     for p in self.positions.values() if p.login == login),
                    Decimal("0"),
                )
                margin = _money(margin)
                equity = self.balance[login] + self.credit[login] + floating
                level = _money(equity / margin * 100) if margin else Decimal("0")
                out.append(MT5AccountInfo(login, self.balance[login], self.credit[login], equity,
                                          margin, equity - margin, level))
        return out

    # ---------------------------------------------------------- money
    @_persist
    def balance_operation(self, login: int, amount: Decimal, comment: str, credit: bool = False) -> int:
        amount = _money(Decimal(amount))
        with self._lock:
            self._require(login)
            book = self.credit if credit else self.balance
            if amount < 0:
                free = self.get_account(login).margin_free
                if book[login] + amount < 0 or (not credit and free + amount < 0):
                    raise MT5Error("not enough money", 10019)
            book[login] += amount
            deal = self._new_deal(login=login, action=DEAL_CREDIT if credit else DEAL_BALANCE, entry=0,
                                  symbol="", volume=Decimal("0"), price=Decimal("0"), profit=amount,
                                  comment=comment[:31])
        self._emit(deal)
        return deal.ticket

    # ---------------------------------------------------------- history
    def get_deals(self, logins, date_from, date_to) -> list[MT5Deal]:
        wanted = set(logins)
        with self._lock:
            return [d for d in self.deals if d.login in wanted and date_from <= d.time <= date_to]

    def get_positions(self, logins) -> list[MT5Position]:
        wanted = set(logins)
        with self._lock:
            out = []
            for p in self.positions.values():
                if p.login in wanted:
                    bid, ask = self.prices[p.symbol]
                    out.append(replace(p, price_current=bid if p.action == BUY else ask, profit=self._floating(p)))
            return out

    # ---------------------------------------------------------- trading
    @_persist
    def open_position(self, login, symbol, side, volume, comment="") -> TradeResult:
        volume = Decimal(volume)
        with self._lock:
            user = self._require(login)
            if not user.enabled or not user.trading_enabled:
                return TradeResult(retcode=10017, comment="trade disabled")
            if symbol not in self.prices or symbol not in self.symbols:
                return TradeResult(retcode=10013, comment="invalid symbol")
            if self.symbols[symbol].trade_mode != 4:
                return TradeResult(retcode=10017, comment="trade disabled for the symbol")
            if volume <= 0:
                return TradeResult(retcode=10014, comment="invalid volume")
            bid, ask = self.prices[symbol]
            price = ask if side == BUY else bid
            pos_id = self._next("position")
            now = _now()
            self.positions[pos_id] = MT5Position(pos_id, login, symbol, side, volume, price, price,
                                                 Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"),
                                                 now, now, comment)
            deal = self._new_deal(login=login, action=DEAL_BUY if side == BUY else DEAL_SELL, entry=ENTRY_IN,
                                  symbol=symbol, volume=volume, price=price, profit=Decimal("0"),
                                  position_id=pos_id, comment=comment[:31])
        self._emit(deal)
        return TradeResult(10009, deal.ticket, deal.ticket, pos_id, volume, price)

    @_persist
    def close_position(self, login, position_id, symbol, side, volume, comment="") -> TradeResult:
        volume = Decimal(volume)
        with self._lock:
            pos = self.positions.get(position_id)
            if not pos or pos.login != login:
                return TradeResult(retcode=10036, comment="position not found")
            volume = min(volume, pos.volume)
            bid, ask = self.prices[pos.symbol]
            price = bid if pos.action == BUY else ask
            diff = (price - pos.price_open) if pos.action == BUY else (pos.price_open - price)
            profit = _money(diff * volume * self._contract(pos.symbol))
            self.balance[login] += profit
            pos.volume -= volume
            if pos.volume <= 0:
                del self.positions[position_id]
            deal = self._new_deal(login=login, action=DEAL_SELL if pos.action == BUY else DEAL_BUY,
                                  entry=ENTRY_OUT, symbol=pos.symbol, volume=volume, price=price,
                                  profit=profit, position_id=position_id, comment=comment[:31])
        self._emit(deal)
        return TradeResult(10009, deal.ticket, deal.ticket, position_id, volume, price)

    # ---------------------------------------------------------- orders
    def get_orders(self, logins) -> list[MT5Order]:
        wanted = set(logins)
        with self._lock:
            return [replace(o) for o in self.orders.values() if o.login in wanted]

    def get_order_history(self, logins, date_from, date_to) -> list[MT5Order]:
        wanted = set(logins)
        with self._lock:
            return [replace(o) for o in self.order_history
                    if o.login in wanted and o.time_setup and date_from <= o.time_setup <= date_to]

    @_persist
    def place_order(self, login: int, symbol: str, order_type: int, volume: Decimal, price: Decimal,
                    comment: str = "") -> int:
        """Simulator only: place a pending (limit / stop) order."""
        with self._lock:
            self._require(login)
            ticket = self._next("order")
            bid, ask = self.prices[symbol]
            self.orders[ticket] = MT5Order(ticket=ticket, login=login, symbol=symbol, type=order_type,
                                           state=ORDER_PLACED, volume_initial=Decimal(volume),
                                           volume_current=Decimal(volume), price_order=Decimal(price),
                                           price_current=ask if order_type % 2 == 0 else bid, time_setup=_now(),
                                           comment=comment)
            return ticket

    @_persist
    def cancel_order(self, ticket: int) -> None:
        """Simulator only: cancel a pending order (it moves to the history)."""
        with self._lock:
            order = self.orders.pop(ticket)
            order.state, order.time_done = ORDER_CANCELLED, _now()
            self.order_history.append(order)

    # ---------------------------------------------------------- firewall
    def get_firewall(self) -> list[MT5FirewallRule]:
        with self._lock:
            return [replace(r) for r in self.firewall]

    @_persist
    def put_firewall_rule(self, tag: str, rule: MT5FirewallRule) -> None:
        with self._lock:
            self.firewall = [r for r in self.firewall if not r.comment.startswith(tag)] + [replace(rule)]

    @_persist
    def delete_firewall_rule(self, tag: str) -> int:
        with self._lock:
            before = len(self.firewall)
            self.firewall = [r for r in self.firewall if not r.comment.startswith(tag)]
            return before - len(self.firewall)

    # ---------------------------------------------------------- symbols
    def _contract(self, symbol: str) -> Decimal:
        spec = self.symbols.get(symbol)
        return spec.contract_size if spec else SYMBOLS[symbol][2]

    def get_symbols(self) -> list[MT5Symbol]:
        with self._lock:
            return [replace(s, sessions=_copy(s.sessions)) for _, s in sorted(self.symbols.items())]

    @_persist
    def put_symbol(self, symbol: MT5Symbol, template: str | None = None) -> MT5Symbol:
        with self._lock:
            if template is not None:
                if symbol.name in self.symbols:
                    raise MT5Error(f"Symbol {symbol.name} already exists on MT5")
                if template not in self.symbols:
                    raise MT5Error(f"Template symbol {template} not found on MT5", 13)
                if template in self.prices:  # quotes for the new symbol: the template's, until a feed sends its own
                    self.prices.setdefault(symbol.name, list(self.prices[template]))
            elif symbol.name not in self.symbols:
                raise MT5Error(f"Symbol {symbol.name} not found on MT5", 13)
            base = self.symbols[template if template is not None else symbol.name]
            saved = replace(symbol, sessions=_copy(symbol.sessions or base.sessions))  # empty: keep the sessions
            self.symbols[symbol.name] = saved
            return replace(saved, sessions=_copy(saved.sessions))

    @_persist
    def delete_symbol(self, name: str) -> None:
        with self._lock:
            if name not in self.symbols:
                raise MT5Error(f"Symbol {name} not found on MT5", 13)
            if any(p.symbol == name for p in self.positions.values()) or any(
                    o.symbol == name for o in self.orders.values()):
                raise MT5Error(f"Symbol {name} has open positions or orders on MT5")
            del self.symbols[name]

    # ---------------------------------------------------------- liquidity: gateways and data feeds
    def get_links(self, kind: str) -> list[MT5Link]:
        with self._lock:
            return [replace(link, password=None, gateway_password=None,
                            state={"connected": link.enabled, "ticks": 0})
                    for (k, _), link in sorted(self.links.items()) if k == kind]

    @_persist
    def put_link(self, link: MT5Link, create: bool = False) -> MT5Link:
        with self._lock:
            key = (link.kind, link.name)
            module = next((m for m in LINK_MODULES if m.kind == link.kind and m.module == link.module), None)
            if module is None:
                raise MT5Error(f"Module {link.module} is not installed on the MT5 server", 13)
            if create:
                if key in self.links:
                    raise MT5Error(f"{link.name} already exists on MT5")
                link = replace(link, gateway_type=module.gateway_type,
                               params={k: "" if v is None else v for k, v in link.params.items()})
                others = [x for x in self.links.values() if x.kind == link.kind]
                if link.kind == "gateway" and not link.mt5_id:
                    link.mt5_id = max((x.mt5_id for x in others), default=0) + 1
                if not link.gateway_server:
                    link.gateway_server = f"127.0.0.1:{16384 + len(self.links) + 1}"
            else:
                if key not in self.links:
                    raise MT5Error(f"{link.name} not found on MT5", 13)
                old = self.links[key]
                link = replace(link, params={k: old.params.get(k, "") if v is None else v for k, v in link.params.items()},
                               password=old.password if link.password is None else link.password,
                               gateway_password=old.gateway_password if link.gateway_password is None
                               else link.gateway_password, mt5_id=old.mt5_id, gateway_type=old.gateway_type)
            self.links[key] = replace(link, state={})
            return next(x for x in self.get_links(link.kind) if x.name == link.name)

    @_persist
    def delete_link(self, kind: str, name: str) -> None:
        with self._lock:
            if self.links.pop((kind, name), None) is None:
                raise MT5Error(f"{name} not found on MT5", 13)

    def get_link_modules(self, kind: str) -> list[MT5LinkModule]:
        return [replace(m) for m in LINK_MODULES if m.kind == kind]

    # ---------------------------------------------------------- routing rules
    def _route_pos(self, name: str) -> int:
        pos = next((i for i, r in enumerate(self.routes) if r.name == name), None)
        if pos is None:
            raise MT5Error(f"Routing rule {name} not found on MT5", 13)
        return pos

    def get_routes(self) -> list[MT5Route]:
        with self._lock:
            return [_copy(r) for r in self.routes]

    @_persist
    def put_route(self, route: MT5Route, create: bool = False) -> MT5Route:
        with self._lock:
            if create:
                if any(r.name == route.name for r in self.routes):
                    raise MT5Error(f"Routing rule {route.name} already exists on MT5")
                self.routes.append(_copy(route))
            else:
                self.routes[self._route_pos(route.name)] = _copy(route)
            return _copy(route)

    @_persist
    def delete_route(self, name: str) -> None:
        with self._lock:
            del self.routes[self._route_pos(name)]

    @_persist
    def move_route(self, name: str, position: int) -> None:
        with self._lock:
            route = self.routes.pop(self._route_pos(name))
            self.routes.insert(max(0, min(position, len(self.routes))), route)

    # ---------------------------------------------------------- server configuration
    def add_group(self, name: str, currency: str = "USD") -> None:
        with self._lock:
            self.groups[name] = _group(name, currency)
        self.save()

    def create_group(self, name: str, template: str, currency: str | None = None) -> MT5Group:
        with self._lock:
            if name in self.groups:
                raise MT5Error(f"Group {name} already exists on MT5")
            if template not in self.groups:
                raise MT5Error(f"Template group {template} not found on MT5", 13)
            tpl = self.groups[template]
            self.groups[name] = replace(tpl, name=name, currency=currency or tpl.currency)
        self.save()
        return self.get_group(name)

    def get_group(self, name: str) -> MT5Group | None:
        with self._lock:
            group = self.groups.get(name)
            return replace(group) if group else None

    def update_group(self, name, *, margin_call=None, stop_out=None, demo_leverage=None, demo_deposit=None,
                     demo_inactivity=None):
        with self._lock:
            if name not in self.groups:
                raise MT5Error(f"Group {name} not found on MT5", 13)
            changes = dict(margin_call=margin_call, stop_out=stop_out, demo_leverage=demo_leverage,
                           demo_deposit=demo_deposit, demo_inactivity=demo_inactivity)
            self.groups[name] = replace(self.groups[name], **{k: v for k, v in changes.items() if v is not None})
        self.save()
        return self.get_group(name)

    def set_group_pricing(self, name, spreads, commissions, prefix):
        with self._lock:
            if name not in self.groups:
                raise MT5Error(f"Group {name} not found on MT5", 13)
            g = self.groups[name]
            merged = {**g.spreads, **spreads}
            kept = [c for c in g.commissions if not c["name"].startswith(prefix)]
            added = [{"name": c["name"], "path": c["path"], "value": float(c["per_lot"]), "per": "lot", "entry": 0}
                     for c in commissions]
            self.groups[name] = replace(g, spreads={p: v for p, v in merged.items() if v is not None},
                                        commissions=kept + added)
        self.save()
        return self.get_group(name)

    def delete_group(self, name: str) -> None:
        with self._lock:
            if name not in self.groups:
                raise MT5Error(f"Group {name} not found on MT5", 13)
            if self.get_users_in_group(name):
                raise MT5Error(f"Group {name} still has accounts on MT5")
            del self.groups[name]
        self.save()

    def get_groups(self, mask: str = "*") -> list[MT5Group]:
        from app.core.masks import matches

        with self._lock:
            return [replace(g) for name, g in sorted(self.groups.items()) if matches(mask, name)]

    def get_users_in_group(self, group: str) -> list[MT5User]:
        with self._lock:
            return [replace(u) for u in self.users.values() if u.group == group]

    # ---------------------------------------------------------- platform health
    def get_server_health(self) -> list[MT5Component]:
        """One simulated machine running every component; the load grows with the number of accounts."""
        with self._lock:
            users, positions = len(self.users), len(self.positions)
        boot = _now().replace(hour=0, minute=0, second=0, microsecond=0)
        common = dict(address="127.0.0.1", build=5000, os="Simulated Windows Server", cpu="Simulated CPU", cores=8,
                      cpu_critical=80, memory_total=16384, memory_free_critical=200, disk_total=512000,
                      disk_free=400000, disk_free_critical=8192, disk_read=900, disk_read_critical=30,
                      disk_write=800, disk_write_critical=15, connections_critical=10000, network_critical=102400,
                      last_boot=boot)
        load = min(95, 5 + users // 20 + positions // 10)
        free = 16384 - 2048 - users * 2
        out = [MT5Component(1, "Main Trade Server", 0, cpu_max=load, memory_free=free, memory_free_min=free - 300,
                            connections_max=users // 4 + 2, network_max=50 + users, **common),
               MT5Component(2, "Access Server Europe", 3, cpu_max=load // 2, memory_free=free, memory_free_min=free - 300,
                            connections_max=users // 3 + 5, network_max=80 + users * 2, **common),
               MT5Component(4, "Access Server Asia", 3, cpu_max=load // 3, memory_free=free, memory_free_min=free - 300,
                            connections_max=users // 5 + 3, network_max=60 + users, **{**common, "address": "127.0.0.2"}),
               MT5Component(3, "History Server", 2, cpu_max=load // 3, memory_free=free, memory_free_min=free - 300,
                            connections_max=3, network_max=40, **common)]
        for c in out:
            c.last_boot = self.booted.get(c.id, c.last_boot)
        for c in out:  # components "switched off" with down_components
            if c.id in self.down_components:
                c.connected, c.connections_max = False, 0
        return out

    def restart_component(self, component_id: int) -> None:
        if component_id not in {c.id for c in self.get_server_health()}:
            raise MT5Error(f"Server component {component_id} not found on MT5", 13)
        with self._lock:
            self.restarts.append(("component", component_id))
            self.booted[component_id] = _now()

    def restart_links(self, kind: str) -> None:
        with self._lock:
            self.restarts.append(("links", kind))

    # ---------------------------------------------------------- market data
    def get_quotes(self, symbols):
        with self._lock:
            return {s: tuple(self.prices[s]) for s in symbols if s in self.prices}

    # ---------------------------------------------------------- events
    def subscribe_deals(self, callback: DealCallback) -> None:
        self._subscribers.append(callback)

    def unsubscribe_deals(self, callback: DealCallback) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)
