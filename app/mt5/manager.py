"""Adapter for the official MetaQuotes `MT5Manager` Python package (Windows only).

Requires a *manager* account on the MT5 server (not a trader login) with rights for
users, accounts, deals, positions, balance operations and dealer operations.

Signatures follow the package's `_core.pyi` stubs (build 5.0.6231). The parts that
cannot be verified without a live server are kept in small helpers (`_ts`,
`_DealerSink`, `_tick`) so they are easy to adjust after a first test connection.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal

from app.mt5.gateway import (
    BUY, DealCallback, MT5AccountInfo, MT5Component, MT5Deal, MT5Error, MT5FirewallRule, MT5Gateway, MT5Group, MT5Link,
    MT5LinkModule, MT5Order, MT5Position, MT5Route, MT5Symbol, MT5User, RET_NOT_FOUND, RET_PARAMS, TradeResult,
)

log = logging.getLogger(__name__)

VOLUME_DIVISOR = Decimal(10000)  # MTDeal/MTPosition.Volume is in 1/10000 lot
EXT_DIVISOR = Decimal(100000000)  # the *Ext volumes are in 1/100000000 lot
SWAP_DAYS = ("SwapRateSunday", "SwapRateMonday", "SwapRateTuesday", "SwapRateWednesday", "SwapRateThursday",
             "SwapRateFriday", "SwapRateSaturday")
# MT5Symbol field -> IMTConSymbol property, for the plain settings
SYMBOL_PROPS = {"path": "Path", "description": "Description", "source": "Source", "currency_base": "CurrencyBase",
                "currency_profit": "CurrencyProfit", "currency_margin": "CurrencyMargin", "digits": "Digits",
                "calc_mode": "CalcMode", "trade_mode": "TradeMode", "exec_mode": "ExecMode", "spread": "Spread",
                "stops_level": "StopsLevel", "freeze_level": "FreezeLevel", "swap_mode": "SwapMode"}
SYMBOL_DECIMALS = {"contract_size": "ContractSize", "swap_long": "SwapLong", "swap_short": "SwapShort",
                   "margin_initial": "MarginInitial", "margin_maintenance": "MarginMaintenance",
                   "margin_hedged": "MarginHedged"}
# per kind: the API's name ("Gateway" / "Feeder") and the properties holding the LP's address and login
LINK_API = {"gateway": ("Gateway", "TradingServer", "TradingLogin", "TradingPassword"),
            "feed": ("Feeder", "FeedServer", "FeedLogin", "FeedPassword")}
SESSION_KINDS = ("quote", "trade")
# EnConditionType -> the IMTConCondition / IMTConRoute value property ("Value…" / "Param…") holding it
VALUE_PROPS = {1: "String", 20: "String", 21: "String", 22: "String", 2: "Int", 24: "Int", 3: "UInt", 13: "UInt",
               14: "UInt", 16: "UInt", 17: "UInt", 18: "UInt", 19: "UInt", 25: "UInt", 4: "Double", 15: "Double",
               5: "Color", 6: "Money", 7: "VolumeExt", 8: "Datetime", 23: "Datetime", 9: "Leverage", 10: "Bool",
               11: "Time", 12: "WeekDay"}
# EnMTAPIRetcode values meaning the link to the server is gone; the next call reconnects.
CONNECTION_LOST = {7, 9, 10, 11, 15}  # NETWORK, TIMEOUT, CONNECTION, NOSERVICE, SHUTDOWN


def _dec(v) -> Decimal:
    return Decimal(str(v))


def _dt(seconds: int) -> datetime:
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc).replace(tzinfo=None)


def _triple_day(rates: list[float], default: int | None = 3) -> int | None:
    """The weekday (0 = Sunday) charged triple swap, from the symbol's per-day swap rates."""
    return next((day for day, rate in enumerate(rates) if rate >= 3), default)


def _plain(d: Decimal) -> str:
    """1000 not 1E+3, 1.5 not 1.50000000."""
    return format(d.normalize(), "f")


def _get_value(obj, prefix: str, kind: int) -> str:
    """A condition value / action parameter as the CRM writes it: times "HH:MM", volumes in lots."""
    prop = VALUE_PROPS.get(int(kind))
    if prop is None:
        return ""
    v = getattr(obj, prefix + prop)
    if prop == "Time":
        return f"{int(v) // 60:02d}:{int(v) % 60:02d}"
    if prop == "VolumeExt":
        return _plain(Decimal(int(v)) / EXT_DIVISOR)
    if prop == "Bool":
        return "1" if v else "0"
    return _plain(_dec(v)) if prop in ("Double", "Money") else str(v)


def _set_value(obj, prefix: str, kind: int, value: str) -> None:
    prop = VALUE_PROPS.get(int(kind))
    if prop is None:
        return
    if prop == "Time":
        hours, _, minutes = value.partition(":")
        v = int(hours) * 60 + int(minutes or 0)
    elif prop == "VolumeExt":
        v = int(Decimal(value) * EXT_DIVISOR)
    elif prop == "String":
        v = value
    elif prop in ("Double", "Money"):
        v = float(value)
    elif prop == "Bool":
        v = value not in ("", "0", "false", "False")
    else:
        v = int(value)
    setattr(obj, prefix + prop, v)


def _ts(dt: datetime) -> int:
    """Convert naive-UTC datetime to the server's epoch seconds."""
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


class _DealerSink:
    """Receives asynchronous DealerSend results."""

    def __init__(self):
        self.done = threading.Event()
        self.result = None

    def OnDealerResult(self, result):  # noqa: N802 (MT5 callback name)
        self.result = result
        self.done.set()

    def OnDealerAnswer(self, request):  # noqa: N802
        self.result = request
        self.done.set()


class _DealSink:
    def __init__(self, gateway: "ManagerGateway"):
        self.gateway = gateway

    def OnDealAdd(self, deal):  # noqa: N802
        try:
            converted = self.gateway._convert_deal(deal)
        except Exception:  # never let an exception escape into the native callback
            log.exception("failed to convert pushed deal")
            return
        for cb in list(self.gateway._subscribers):
            try:
                cb(converted)
            except Exception:
                log.exception("deal subscriber failed")


class _TradeSink:
    """Which account's positions or orders changed. Runs on the MT5 thread, so it only passes the login on."""

    kind = ""

    def __init__(self, gateway: "ManagerGateway"):
        self.gateway = gateway

    def _changed(self, item):
        try:
            login = int(item.Login)
        except Exception:  # never let an exception escape into the native callback
            log.exception("failed to read a pushed %s", self.kind)
            return
        for cb in list(self.gateway._trade_subscribers):
            try:
                cb(self.kind, login)
            except Exception:
                log.exception("%s subscriber failed", self.kind)


class _PositionSink(_TradeSink):
    kind = "position"

    def OnPositionAdd(self, position):  # noqa: N802
        self._changed(position)

    def OnPositionUpdate(self, position):  # noqa: N802
        self._changed(position)

    def OnPositionDelete(self, position):  # noqa: N802
        self._changed(position)


class _OrderSink(_TradeSink):
    kind = "order"

    def OnOrderAdd(self, order):  # noqa: N802
        self._changed(order)

    def OnOrderUpdate(self, order):  # noqa: N802
        self._changed(order)

    def OnOrderDelete(self, order):  # noqa: N802
        self._changed(order)


class _ConnectionSink:
    """Tells at once that the link dropped; ensure_connected then connects again."""

    def __init__(self, gateway: "ManagerGateway"):
        self.gateway = gateway

    def OnDisconnect(self):  # noqa: N802
        self.gateway._connected = False


class ManagerGateway(MT5Gateway):
    def __init__(self, server: str, login: int, password: str, timeout_ms: int = 30000, work_dir: str | None = None):
        import MT5Manager  # imported lazily: Windows-only package

        if work_dir:
            # Separate cache per process so the API and the worker never share files.
            Path(work_dir).mkdir(parents=True, exist_ok=True)
            MT5Manager.InitializeManagerAPIPath(module_path=MT5Manager.module_dir, work_path=str(Path(work_dir).resolve()))
        self.M = MT5Manager
        self.server, self.login, self.password, self.timeout_ms = server, login, password, timeout_ms
        self.api = MT5Manager.ManagerAPI()
        self._connected = False
        self._subscribers: list[DealCallback] = []
        self._deal_sink: _DealSink | None = None
        self._trade_subscribers: list = []
        self._trade_sinks: tuple[_PositionSink, _OrderSink] | None = None
        self._connection_sink = _ConnectionSink(self)
        self._connections = 0
        self._selected: set[str] = set()  # symbols this connection receives ticks for (see get_quotes)
        # One call at a time per gateway: the API, the worker's threads and the copy engine share it, and
        # LastError() is only meaningful right after the call that failed. See _serialize_api_calls below.
        self._lock = threading.RLock()

    # ---------------------------------------------------------- plumbing
    def _fail(self, what: str):
        code, retcode, desc = self.M.LastError()
        if int(retcode) in CONNECTION_LOST:
            self._connected = False
            log.warning("MT5 connection lost during %s (%s); will reconnect", what, desc)
        raise MT5Error(f"{what}: {desc}", int(retcode))

    def _rows(self, rows, what: str) -> list:
        """List results: MT5 answers an empty result with "not found" (retcode 13), so that means []."""
        if rows is False or rows is None:
            if rows is False and int(self.M.LastError()[1]) == RET_NOT_FOUND:
                return []
            if rows is False:
                self._fail(what)
            return []
        return rows

    def _check(self, ok, what: str):
        if ok is False or ok is None:
            self._fail(what)
        return ok

    # ---------------------------------------------------------- connection
    def connect(self) -> None:
        modes = self.M.ManagerAPI.EnPumpModes
        pump = (modes.PUMP_MODE_USERS | modes.PUMP_MODE_POSITIONS | modes.PUMP_MODE_ORDERS | modes.PUMP_MODE_GROUPS
                | modes.PUMP_MODE_SYMBOLS)
        self._check(self.api.Connect(self.server, self.login, self.password, pump, self.timeout_ms), "connect")
        self._connected = True
        self._connections += 1
        self._selected = set()  # a new connection starts with the login's own selection only
        self.api.Unsubscribe(self._connection_sink)
        self._check(self.api.Subscribe(self._connection_sink), "Subscribe")
        if self._deal_sink is not None:  # re-attach push events after a reconnect
            self.api.DealUnsubscribe(self._deal_sink)
            self._check(self.api.DealSubscribe(self._deal_sink), "DealSubscribe")
        if self._trade_sinks is not None:
            self._attach_trades()
        log.info("connected to MT5 server %s as manager %s", self.server, self.login)

    def _attach_trades(self) -> None:
        positions, orders = self._trade_sinks
        self.api.PositionUnsubscribe(positions)
        self.api.OrderUnsubscribe(orders)
        self._check(self.api.PositionSubscribe(positions), "PositionSubscribe")
        self._check(self.api.OrderSubscribe(orders), "OrderSubscribe")

    def disconnect(self) -> None:
        if self._deal_sink:
            self.api.DealUnsubscribe(self._deal_sink)
            self._deal_sink = None
        if self._trade_sinks:
            self.api.PositionUnsubscribe(self._trade_sinks[0])
            self.api.OrderUnsubscribe(self._trade_sinks[1])
            self._trade_sinks = None
        self.api.Disconnect()
        self._connected = False

    def ensure_connected(self) -> None:
        if not self._connected:
            try:
                self.api.Disconnect()
            except Exception:  # noqa: BLE001 - best effort before reconnecting
                pass
            self.connect()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def connections(self) -> int:
        return self._connections

    # ---------------------------------------------------------- converters
    def _convert_user(self, u) -> MT5User:
        rights = self.M.MTUser.EnUsersRights
        return MT5User(
            login=int(u.Login), group=u.Group, leverage=int(u.Leverage), name=u.Name, email=u.EMail,
            phone=u.Phone, country=u.Country,
            enabled=bool(u.Rights & rights.USER_RIGHT_ENABLED),
            trading_enabled=not bool(u.Rights & rights.USER_RIGHT_TRADE_DISABLED),
            first_name=u.FirstName, last_name=u.LastName, city=u.City, address=u.Address, comment=u.Comment,
            registration=_dt(u.Registration) if u.Registration else None,
            last_access=_dt(u.LastAccess) if u.LastAccess else None,
        )

    def _convert_deal(self, d) -> MT5Deal:
        return MT5Deal(
            ticket=int(d.Deal), login=int(d.Login), time=_dt(d.Time), time_msc=int(d.TimeMsc),
            action=int(d.Action), entry=int(d.Entry), symbol=d.Symbol,
            volume=_dec(d.Volume) / VOLUME_DIVISOR, price=_dec(d.Price), profit=_dec(d.Profit),
            commission=_dec(d.Commission), swap=_dec(d.Storage), fee=_dec(d.Fee),
            position_id=int(d.PositionID), order=int(d.Order), reason=int(d.Reason), comment=d.Comment,
            contract_size=_dec(d.ContractSize), tick_value=_dec(d.TickValue), tick_size=_dec(d.TickSize),
            digits=int(d.Digits),
        )

    def _convert_order(self, o) -> MT5Order:
        return MT5Order(
            ticket=int(o.Order), login=int(o.Login), symbol=o.Symbol, type=int(o.Type), state=int(o.State),
            volume_initial=_dec(o.VolumeInitial) / VOLUME_DIVISOR, volume_current=_dec(o.VolumeCurrent) / VOLUME_DIVISOR,
            price_order=_dec(o.PriceOrder), price_current=_dec(o.PriceCurrent), sl=_dec(o.PriceSL),
            tp=_dec(o.PriceTP), time_setup=_dt(o.TimeSetup) if o.TimeSetup else None,
            time_done=_dt(o.TimeDone) if o.TimeDone else None, position_id=int(o.PositionID),
            reason=int(o.Reason), comment=o.Comment,
        )

    def _convert_position(self, p) -> MT5Position:
        return MT5Position(
            position_id=int(p.Position), login=int(p.Login), symbol=p.Symbol, action=int(p.Action),
            volume=_dec(p.Volume) / VOLUME_DIVISOR, price_open=_dec(p.PriceOpen),
            price_current=_dec(p.PriceCurrent), sl=_dec(p.PriceSL), tp=_dec(p.PriceTP),
            profit=_dec(p.Profit), swap=_dec(p.Storage), time_create=_dt(p.TimeCreate),
            time_update=_dt(p.TimeUpdate), comment=p.Comment,
        )

    # ---------------------------------------------------------- users
    def create_user(self, *, group, leverage, name, email, phone, country,
                    master_password, investor_password, login=0) -> int:
        user = self.M.MTUser(self.api)
        user.Login = login
        user.Group = group
        user.Leverage = leverage
        user.Name = name
        user.FirstName, _, user.LastName = name.partition(" ")
        user.EMail = email
        user.Phone = phone or ""
        user.Country = country or ""
        user.Rights = self.M.MTUser.EnUsersRights.USER_RIGHT_DEFAULT
        self._check(self.api.UserAdd(user, master_password, investor_password), "UserAdd")
        return int(user.Login)

    def _raw_user(self, login: int):
        user = self.api.UserRequest(login)
        if not user:
            self._fail(f"UserRequest {login}")
        return user

    def get_user(self, login: int) -> MT5User:
        return self._convert_user(self._raw_user(login))

    def update_user(self, login, *, leverage=None, group=None, enabled=None, trading_enabled=None,
                    name=None, email=None, phone=None, first_name=None, last_name=None, country=None,
                    city=None, address=None) -> MT5User:
        user = self._raw_user(login)
        for attr, value in (("FirstName", first_name), ("LastName", last_name), ("Country", country),
                            ("City", city), ("Address", address)):
            if value is not None:
                setattr(user, attr, value)
        rights = self.M.MTUser.EnUsersRights
        if leverage is not None:
            user.Leverage = leverage
        if group is not None:
            user.Group = group
        if name is not None:
            user.Name = name
        if email is not None:
            user.EMail = email
        if phone is not None:
            user.Phone = phone
        if enabled is not None:
            user.Rights = (user.Rights | rights.USER_RIGHT_ENABLED) if enabled else (user.Rights & ~rights.USER_RIGHT_ENABLED)
        if trading_enabled is not None:
            flag = rights.USER_RIGHT_TRADE_DISABLED
            user.Rights = (user.Rights & ~flag) if trading_enabled else (user.Rights | flag)
        self._check(self.api.UserUpdate(user), "UserUpdate")
        return self._convert_user(user)

    def delete_user(self, login: int) -> None:
        self._check(self.api.UserDelete(login), f"UserDelete {login}")

    def get_last_access(self) -> dict[int, datetime | None]:
        # the pumped copy (PUMP_MODE_USERS) of every group; checked on a live server to match UserRequest exactly
        rows = self.api.UserGetByGroupNumPy("*", "Login,LastAccess")
        if rows is None or rows is False:
            self._fail("UserGetByGroupNumPy")
        return {int(r["Login"]): _dt(r["LastAccess"]) if r["LastAccess"] > 0 else None for r in rows}

    def archive_users(self, logins: list[int]) -> dict[int, str]:
        failed = {}
        with self._admin("archiving accounts") as admin:  # the Manager API cannot archive
            for login in logins:
                if not admin.UserArchive(int(login)):
                    failed[int(login)] = str(self.M.LastError()[2])
        return failed

    def _pass_type(self, investor: bool):
        kinds = self.M.MTUser.EnUsersPasswords
        return kinds.USER_PASS_INVESTOR if investor else kinds.USER_PASS_MAIN

    def change_password(self, login: int, password: str, investor: bool = False) -> None:
        self._check(self.api.UserPasswordChange(self._pass_type(investor), login, password), "UserPasswordChange")

    def check_password(self, login: int, password: str, investor: bool = False) -> bool:
        return bool(self.api.UserPasswordCheck(self._pass_type(investor), login, password))

    def get_accounts(self, logins: list[int]) -> list[MT5AccountInfo]:
        if not logins:
            return []
        rows = self._rows(self.api.UserAccountRequestByLogins(logins), "UserAccountRequestByLogins")
        return [
            MT5AccountInfo(int(a.Login), _dec(a.Balance), _dec(a.Credit), _dec(a.Equity), _dec(a.Margin),
                           _dec(a.MarginFree), _dec(a.MarginLevel))
            for a in rows or []
        ]

    # ---------------------------------------------------------- money
    def balance_operation(self, login: int, amount: Decimal, comment: str, credit: bool = False) -> int:
        actions = self.M.MTDeal.EnDealAction
        kind = actions.DEAL_CREDIT if credit else actions.DEAL_BALANCE
        ticket = self.api.DealerBalance(login, float(amount), kind, comment[:31])
        self._check(ticket, "DealerBalance")
        return int(ticket) if not isinstance(ticket, bool) else 0

    # ---------------------------------------------------------- history
    def get_deals(self, logins, date_from, date_to) -> list[MT5Deal]:
        rows = self._rows(self.api.DealRequestByLogins(list(logins), _ts(date_from), _ts(date_to)),
                          "DealRequestByLogins")
        return [self._convert_deal(d) for d in rows or []]

    def get_positions(self, logins) -> list[MT5Position]:
        rows = self._rows(self.api.PositionRequestByLogins(list(logins)), "PositionRequestByLogins")
        return [self._convert_position(p) for p in rows or []]

    def get_orders(self, logins) -> list[MT5Order]:
        rows = self._rows(self.api.OrderRequestByLogins(list(logins)), "OrderRequestByLogins")
        return [self._convert_order(o) for o in rows or []]

    def get_order_history(self, logins, date_from, date_to) -> list[MT5Order]:
        rows = self._rows(self.api.HistoryRequestByLogins(list(logins), _ts(date_from), _ts(date_to)),
                          "HistoryRequestByLogins")
        return [self._convert_order(o) for o in rows or []]

    def _delete_each(self, delete, tickets, what: str) -> dict[int, str]:
        failed = {}
        for ticket in tickets:
            if delete(int(ticket)) is False:
                retcode, desc = self.M.LastError()[1:3]
                if int(retcode) in CONNECTION_LOST:
                    self._fail(what)
                if int(retcode) != RET_NOT_FOUND:
                    failed[int(ticket)] = str(desc)
        return failed

    def delete_orders(self, tickets) -> dict[int, str]:
        return self._delete_each(self.api.OrderDelete, tickets, "OrderDelete")

    def delete_deals(self, tickets) -> dict[int, str]:
        return self._delete_each(self.api.DealDelete, tickets, "DealDelete")

    def rewrite_deal(self, ticket, *, action, amount, comment, when, symbol=""):
        deal = self.api.DealRequest(int(ticket))
        if not deal:
            self._fail(f"DealRequest {ticket}")
        deal.Action, deal.Entry, deal.Symbol, deal.Comment = int(action), 0, symbol, comment[:31]
        deal.Profit, deal.Commission, deal.Storage, deal.Fee = float(amount), 0.0, 0.0, 0.0
        deal.Volume, deal.VolumeExt, deal.VolumeClosed, deal.VolumeClosedExt = 0, 0, 0, 0
        deal.PositionID, deal.Order = 0, 0
        deal.Time, deal.TimeMsc = _ts(when), _ts(when) * 1000
        self._check(self.api.DealUpdate(deal), f"DealUpdate {ticket}")

    def server_time(self):
        return _dt(self.api.TimeServer())

    def check_balance(self, login):
        result = self.api.UserBalanceCheck(login, False)  # False: compare only, never fix
        if not result:
            self._fail(f"UserBalanceCheck {login}")
        return bool(result[0]), _dec(result[1]), _dec(result[2])

    # ---------------------------------------------------------- trading
    def _select(self, symbol: str) -> bool:
        """Subscribe this connection to the symbol's ticks; True if it was just selected."""
        if symbol in self._selected or not self.api.SelectedAdd(symbol):
            return False
        self._selected.add(symbol)
        return True

    def _tick(self, symbol: str) -> tuple[float, float]:
        if self._select(symbol):
            for _ in range(20):  # the first tick of a newly selected symbol arrives shortly after
                if _priced(self.api.TickLast(symbol)):
                    break
                time.sleep(0.05)
        tick = self.api.TickLast(symbol)
        if not _priced(tick):
            self._fail(f"TickLast {symbol}")
        return tick.bid, tick.ask

    def _send(self, login: int, symbol: str, order_type: int, volume: Decimal, price: float,
              position_id: int = 0, comment: str = "") -> TradeResult:
        R = self.M.MTRequest
        req = R(self.api)
        req.Action = R.EnTradeActions.TA_DEALER_POS_EXECUTE
        req.Login = login
        req.Symbol = symbol
        req.Type = order_type
        req.Volume = int(volume * VOLUME_DIVISOR)
        req.PriceOrder = price
        req.TypeFill = self.M.MTOrder.EnOrderFilling.ORDER_FILL_IOC
        req.Position = position_id
        req.Comment = comment[:31]
        sink = _DealerSink()
        with self._lock:
            self._check(self.api.DealerSend(req, sink), "DealerSend")
        if not sink.done.wait(self.timeout_ms / 1000):  # outside the lock: other calls go on meanwhile
            return TradeResult(retcode=-1, comment="dealer timeout")
        res = sink.result
        if not hasattr(res, "ResultRetcode"):  # the MTConfirm of OnDealerResult got here before the answer
            return TradeResult(
                retcode=int(res.Retcode), deal_ticket=int(res.DealID), order=int(res.OrderID),
                position_id=position_id or int(res.OrderID), volume=_dec(res.Volume) / VOLUME_DIVISOR,
                price=_dec(res.Price), comment=str(res.Comment or ""),
            )
        return TradeResult(
            retcode=int(res.ResultRetcode), deal_ticket=int(res.ResultDeal), order=int(res.ResultOrder),
            position_id=position_id or int(res.ResultOrder),  # MT5 position id == opening order ticket
            volume=_dec(res.ResultVolume) / VOLUME_DIVISOR, price=_dec(res.ResultPrice),
            comment=str(res.ResultComment or ""),
        )

    def open_position(self, login, symbol, side, volume, comment="") -> TradeResult:
        bid, ask = self._tick(symbol)
        types = self.M.MTOrder.EnOrderType
        order_type = types.OP_BUY if side == BUY else types.OP_SELL
        return self._send(login, symbol, order_type, Decimal(volume), ask if side == BUY else bid, comment=comment)

    def close_position(self, login, position_id, symbol, side, volume, comment="") -> TradeResult:
        bid, ask = self._tick(symbol)
        types = self.M.MTOrder.EnOrderType
        order_type = types.OP_SELL if side == BUY else types.OP_BUY
        return self._send(login, symbol, order_type, Decimal(volume), bid if side == BUY else ask,
                          position_id=position_id, comment=comment)

    # ---------------------------------------------------------- server configuration
    def get_groups(self, mask: str = "*") -> list[MT5Group]:
        rows = self._rows(self.api.GroupRequestArray(mask), "GroupRequestArray")
        return [self._convert_group(g) for g in rows or []]

    @staticmethod
    def _convert_group(g) -> MT5Group:
        spreads = {}
        for x in (g.SymbolNext(i) for i in range(g.SymbolTotal())):
            if x and int(x.SpreadDiff) != int(x.SpreadDiffDefault):
                spreads[str(x.Path)] = int(x.SpreadDiff)
        commissions = []
        for c in (g.CommissionNext(i) for i in range(g.CommissionTotal())):
            if not c:
                continue
            tier = c.TierNext(0) if c.TierTotal() else None
            commissions.append({"name": str(c.Name), "path": str(c.Path), "entry": int(c.EntryMode),
                                "value": float(tier.Value) if tier else 0.0,
                                "per": "lot" if tier and int(tier.Type) == 1 else "deal"})
        return MT5Group(name=g.Group, currency=g.Currency, company=g.Company, margin_call=_dec(g.MarginCall),
                        stop_out=_dec(g.MarginStopOut), demo_leverage=int(g.DemoLeverage),
                        demo_deposit=_dec(g.DemoDeposit), demo_inactivity=int(g.DemoInactivityPeriod),
                        spreads=spreads, commissions=commissions)

    def set_group_pricing(self, name, spreads, commissions, prefix):
        group = self._group(name)
        if group is None:
            raise MT5Error(f"Group {name} not found on MT5", RET_NOT_FOUND)
        rows = {str(x.Path): (i, x) for i, x in ((i, group.SymbolNext(i)) for i in range(group.SymbolTotal())) if x}
        for path, points in spreads.items():
            if path in rows:
                pos, row = rows[path]
                row.SpreadDiff = int(row.SpreadDiffDefault) if points is None else int(points)
                if not group.SymbolUpdate(pos, row):
                    self._fail(f"group {name}: spread on {path}")
            elif points is not None:  # a new entry: everything else stays "as the symbol"
                row = self.M.MTConGroupSymbol(self.api)
                row.Path = path
                row.SpreadDiff = int(points)
                if not group.SymbolAdd(row):
                    self._fail(f"group {name}: spread on {path}")
        for pos in reversed(range(group.CommissionTotal())):  # from the end, so positions stay valid
            c = group.CommissionNext(pos)
            if c and str(c.Name).startswith(prefix) and not group.CommissionDelete(pos):
                self._fail(f"group {name}: removing commission {c.Name}")
        C, T = self.M.MTConCommission, self.M.MTConCommTier
        for item in commissions:
            c = C(self.api)
            c.Name, c.Description, c.Path = item["name"], "Set by the CRM", item["path"]
            c.Mode, c.RangeMode = C.EnCommMode.COMM_STANDARD, C.EnCommRangeMode.COMM_RANGE_VOLUME
            c.ChargeMode, c.EntryMode = C.EnCommChargeMode.COMM_CHARGE_INSTANT, C.EnCommEntryMode.COMM_ENTRY_ALL
            c.ActionMode, c.ProfitMode = C.EnCommActionMode.COMM_ACTION_ALL, C.EnCommProfitMode.COMM_PROFIT_ALL
            c.ReasonFlags = C.EnCommReasonFlags.COMM_REASON_FLAG_ALL
            tier = T(self.api)
            tier.Mode, tier.Type = T.EnCommissionMode.COMM_MONEY_DEPOSIT, T.EnCommissionVolumeType.COMM_TYPE_VOLUME
            tier.Value, tier.RangeFrom, tier.RangeTo = float(item["per_lot"]), 0.0, 1000000.0
            if not c.TierAdd(tier) or not group.CommissionAdd(c):
                self._fail(f"group {name}: commission {item['name']}")
        if not self.api.GroupUpdate(group):
            self._fail(f"GroupUpdate {name}")  # needs the manager's "configure groups" right
        return self.get_group(name)

    def _group(self, name: str):
        """The group's config object, or None when MT5 doesn't have it."""
        group = self.api.GroupRequest(name)
        if group is False and int(self.M.LastError()[1]) == RET_NOT_FOUND:
            return None
        if not group:
            self._fail(f"GroupRequest {name}")
        return group

    def get_users_in_group(self, group: str) -> list[MT5User]:
        rows = self._rows(self.api.UserRequestByGroup(group), f"UserRequestByGroup {group}")
        return [self._convert_user(u) for u in rows or []]

    def create_group(self, name: str, template: str, currency: str | None = None) -> MT5Group:
        # The Manager API has no blank-group constructor: copy the template's config and save it under the new name.
        if self._group(name) is not None:
            raise MT5Error(f"Group {name} already exists on MT5")
        group = self._group(template)
        if group is None:
            raise MT5Error(f"Template group {template} not found on MT5", RET_NOT_FOUND)
        group.Group = name
        if currency:
            group.Currency = currency
        if not self.api.GroupUpdate(group):
            if int(self.M.LastError()[1]) == RET_PARAMS:
                # Seen on a live server: the same manager could update existing groups but not add new ones.
                raise MT5Error(f"MT5 did not let the Manager API create group {name}. Create it in MT5 "
                               "Administrator (copy it from the template group), then add it here without "
                               "ticking \"Create this group on MT5\".", RET_PARAMS)
            self._fail(f"GroupUpdate {name}")  # needs the manager's "configure groups" right
        created = self._group(name)
        if created is None:
            raise MT5Error(f"MT5 accepted group {name} but does not return it")
        return self._convert_group(created)

    def get_group(self, name: str) -> MT5Group | None:
        group = self._group(name)
        return None if group is None else self._convert_group(group)

    def update_group(self, name, *, margin_call=None, stop_out=None, demo_leverage=None, demo_deposit=None,
                     demo_inactivity=None):
        group = self._group(name)
        if group is None:
            raise MT5Error(f"Group {name} not found on MT5", RET_NOT_FOUND)
        if margin_call is not None:
            group.MarginCall = float(margin_call)
        if stop_out is not None:
            group.MarginStopOut = float(stop_out)
        if demo_leverage is not None:
            group.DemoLeverage = int(demo_leverage)
        if demo_deposit is not None:
            group.DemoDeposit = float(demo_deposit)
        if demo_inactivity is not None:
            group.DemoInactivityPeriod = int(demo_inactivity)
        if not self.api.GroupUpdate(group):
            self._fail(f"GroupUpdate {name}")  # needs the manager's "configure groups" right
        return self.get_group(name)

    @contextmanager
    def _admin(self, what: str):
        """Short Administrator API connection, for what the Manager API cannot do (groups, firewall).
        Uses the same login, which then needs MT5 administrator access."""
        admin = self.M.AdminAPI()
        if not admin.Connect(self.server, self.login, self.password, self.M.AdminAPI.EnPumpModes(0), self.timeout_ms):
            self._fail(f"Administrator connection ({what} needs a login with MT5 administrator access)")
        try:
            yield admin
        finally:
            admin.Disconnect()

    def delete_group(self, name: str) -> None:
        if self.get_users_in_group(name):
            raise MT5Error(f"Group {name} still has accounts on MT5")
        with self._admin("deleting groups") as admin:
            if not admin.GroupDelete(name):
                self._fail(f"GroupDelete {name}")

    # ---------------------------------------------------------- firewall
    def _firewall_rows(self, admin) -> list:
        return [(pos, admin.FirewallNext(pos)) for pos in range(int(admin.FirewallTotal()))]

    def get_firewall(self) -> list[MT5FirewallRule]:
        actions = self.M.MTConFirewall.EnAction
        with self._admin("reading the firewall") as admin:
            return [MT5FirewallRule(from_ip=str(r.From), to_ip=str(r.To), comment=str(r.Comment),
                                    action="block" if int(r.Action) == int(actions.ACCESS_BLOCK) else "allow")
                    for _, r in self._firewall_rows(admin) if r]

    def _drop_tagged(self, admin, tag: str) -> int:
        removed = 0
        for pos, r in reversed(self._firewall_rows(admin)):  # from the end, so positions stay valid
            if r and str(r.Comment).startswith(tag):
                if not admin.FirewallDelete(pos):
                    self._fail(f"FirewallDelete {tag}")
                removed += 1
        return removed

    def put_firewall_rule(self, tag: str, rule: MT5FirewallRule) -> None:
        actions = self.M.MTConFirewall.EnAction
        with self._admin("changing the firewall") as admin:
            self._drop_tagged(admin, tag)
            entry = self.M.MTConFirewall(admin)
            entry.From, entry.To, entry.Comment = rule.from_ip, rule.to_ip, rule.comment[:63]
            entry.Action = actions.ACCESS_BLOCK if rule.action == "block" else actions.ACCESS_PERMIT
            if not admin.FirewallUpdate(entry):
                self._fail(f"FirewallUpdate {tag}")

    def delete_firewall_rule(self, tag: str) -> int:
        with self._admin("changing the firewall") as admin:
            return self._drop_tagged(admin, tag)

    # ---------------------------------------------------------- symbols
    @staticmethod
    def _convert_symbol(s) -> MT5Symbol:
        def lots(name: str) -> Decimal:
            ext = int(getattr(s, name + "Ext"))
            return Decimal(ext) / EXT_DIVISOR if ext else Decimal(int(getattr(s, name))) / VOLUME_DIVISOR

        rates = [float(getattr(s, day)) for day in SWAP_DAYS]
        values = {k: getattr(s, prop) for k, prop in SYMBOL_PROPS.items()}
        values.update({k: _dec(getattr(s, prop)) for k, prop in SYMBOL_DECIMALS.items()})
        sessions = {kind: [[[int(x.Open), int(x.Close)]
                            for x in (getattr(s, f"Session{kind.title()}Next")(day, i)
                                      for i in range(int(getattr(s, f"Session{kind.title()}Total")(day)))) if x]
                           for day in range(7)] for kind in SESSION_KINDS}
        return MT5Symbol(name=str(s.Symbol), **values, volume_min=lots("VolumeMin"), volume_max=lots("VolumeMax"),
                         volume_step=lots("VolumeStep"), swap_3day=_triple_day(rates), sessions=sessions)

    def _apply_symbol(self, admin, cfg, symbol: MT5Symbol) -> None:
        cfg.Symbol = symbol.name
        for kind, week in symbol.sessions.items():  # empty: the sessions stay as they are
            add = getattr(cfg, f"Session{kind.title()}Add")
            for day, sessions in enumerate(week):
                getattr(cfg, f"Session{kind.title()}Clear")(day)
                for open_, close in sessions:
                    session = self.M.MTConSymbolSession(admin)
                    session.Open, session.Close = int(open_), int(close)
                    add(day, session)
        for k, prop in SYMBOL_PROPS.items():
            setattr(cfg, prop, getattr(symbol, k))
        for k, prop in SYMBOL_DECIMALS.items():
            setattr(cfg, prop, float(getattr(symbol, k)))
        for name, value in (("VolumeMin", symbol.volume_min), ("VolumeMax", symbol.volume_max),
                            ("VolumeStep", symbol.volume_step)):
            setattr(cfg, name, int(Decimal(value) * VOLUME_DIVISOR))
            setattr(cfg, name + "Ext", int(Decimal(value) * EXT_DIVISOR))
        rates = [float(getattr(cfg, day)) for day in SWAP_DAYS]
        current = _triple_day(rates, None)
        if current != symbol.swap_3day:  # move the triple swap; the other days keep their rates
            if current is not None:
                setattr(cfg, SWAP_DAYS[current], 1.0)
            setattr(cfg, SWAP_DAYS[symbol.swap_3day], 3.0)

    def _symbol_cfg(self, admin, name: str):
        cfg = admin.SymbolGet(name)
        if cfg is False and int(self.M.LastError()[1]) == RET_NOT_FOUND:
            return None
        if not cfg:
            self._fail(f"SymbolGet {name}")
        return cfg

    def get_symbols(self) -> list[MT5Symbol]:
        with self._admin("reading symbols") as admin:
            rows = [admin.SymbolNext(pos) for pos in range(int(admin.SymbolTotal()))]
            return [self._convert_symbol(s) for s in rows if s]

    def put_symbol(self, symbol: MT5Symbol, template: str | None = None) -> MT5Symbol:
        with self._admin("changing symbols") as admin:
            if template is not None:
                if self._symbol_cfg(admin, symbol.name) is not None:
                    raise MT5Error(f"Symbol {symbol.name} already exists on MT5")
                cfg = self._symbol_cfg(admin, template)  # no blank symbol in the API: copy the template
                if cfg is None:
                    raise MT5Error(f"Template symbol {template} not found on MT5", RET_NOT_FOUND)
            else:
                cfg = self._symbol_cfg(admin, symbol.name)
                if cfg is None:
                    raise MT5Error(f"Symbol {symbol.name} not found on MT5", RET_NOT_FOUND)
            self._apply_symbol(admin, cfg, symbol)
            if not admin.SymbolUpdate(cfg):
                self._fail(f"SymbolUpdate {symbol.name}")
            saved = self._symbol_cfg(admin, symbol.name)
            if saved is None:
                raise MT5Error(f"MT5 accepted symbol {symbol.name} but does not return it")
            return self._convert_symbol(saved)

    def delete_symbol(self, name: str) -> None:
        with self._admin("deleting symbols") as admin:
            if not admin.SymbolDelete(name):
                self._fail(f"SymbolDelete {name}")

    # ---------------------------------------------------------- liquidity: gateways and data feeds
    def _links(self, admin, kind: str) -> list:
        api = LINK_API[kind][0]
        return [getattr(admin, f"{api}Next")(pos) for pos in range(int(getattr(admin, f"{api}Total")()))]

    @staticmethod
    def _items(cfg, what: str) -> list:
        return [getattr(cfg, f"{what}Next")(i) for i in range(int(getattr(cfg, f"{what}Total")()))]

    def _convert_link(self, kind: str, c) -> MT5Link:
        _, server, login, _ = LINK_API[kind]
        enabled = self.M.MTConGateway.EnGatewayMode.GATEWAY_ENABLED if kind == "gateway" \
            else self.M.MTConFeeder.EnFeedersMode.FEEDER_ENABLED
        state = {"connected": bool(c.StateConnected), "ticks": int(c.StateReceivedTicks),
                 "traffic_in": int(c.StateTrafficIn)}
        if kind == "gateway":
            state["trades"] = int(c.StateTradesTotal)
        return MT5Link(
            kind=kind, name=str(c.Name), module=str(c.Module), enabled=bool(int(c.Mode) & int(enabled)),
            server=str(getattr(c, server)), login=str(getattr(c, login)), gateway_server=str(c.GatewayServer),
            gateway_login=int(c.GatewayLogin), mt5_id=int(c.ID) if kind == "gateway" else 0,
            gateway_type=str(c.Gateway) if kind == "gateway" else "", flags=int(c.Flags),
            keywords=str(c.Keywords) if kind == "feed" else "", categories=str(c.Categories) if kind == "feed" else "",
            timeout_reconnect=int(c.TimeoutReconnect), timeout_sleep=int(c.TimeoutSleep),
            timeout_attempts=int(c.TimeoutAttempts), symbols=[str(s) for s in self._items(c, "Symbol")],
            groups=[str(g) for g in self._items(c, "Group")] if kind == "gateway" else [],
            params={str(p.Name): str(p.Value) for p in self._items(c, "Parameter")},
            translations=[{"source": str(t.Source), "symbol": str(t.Symbol), "digits": int(t.Digits),
                           "bid_markup": int(t.BidMarkup), "ask_markup": int(t.AskMarkup)}
                          for t in self._items(c, "Translate")],
            state=state)

    def _link_cfg(self, admin, kind: str, name: str):
        return next((c for c in self._links(admin, kind) if c and str(c.Name) == name), None)

    def _module(self, admin, kind: str, module: str):
        api = LINK_API[kind][0]
        for pos in range(int(getattr(admin, f"{api}ModuleTotal")())):
            m = getattr(admin, f"{api}ModuleNext")(pos)
            if m and str(m.Module) == module:
                return m
        raise MT5Error(f"Module {module} is not installed on the MT5 server", RET_NOT_FOUND)

    def _apply_link(self, admin, cfg, link: MT5Link, module) -> None:
        _, server, login, password = LINK_API[link.kind]
        modes = self.M.MTConGateway.EnGatewayMode if link.kind == "gateway" else self.M.MTConFeeder.EnFeedersMode
        on = int(modes.GATEWAY_ENABLED if link.kind == "gateway" else modes.FEEDER_ENABLED)
        cfg.Mode = (int(cfg.Mode) | on) if link.enabled else (int(cfg.Mode) & ~on)
        setattr(cfg, server, link.server)
        setattr(cfg, login, link.login)
        if link.password is not None:
            setattr(cfg, password, link.password)
        if link.gateway_server:
            cfg.GatewayServer = link.gateway_server
        if link.gateway_login:
            cfg.GatewayLogin = link.gateway_login
        if link.gateway_password is not None:
            cfg.GatewayPassword = link.gateway_password
        cfg.Flags = link.flags
        cfg.TimeoutReconnect, cfg.TimeoutSleep = link.timeout_reconnect, link.timeout_sleep
        cfg.TimeoutAttempts = link.timeout_attempts
        if link.kind == "feed":
            cfg.Keywords, cfg.Categories = link.keywords, link.categories
        cfg.SymbolClear()
        for mask in link.symbols:
            cfg.SymbolAdd(mask)
        if link.kind == "gateway":
            cfg.GroupClear()
            for mask in link.groups:
                cfg.GroupAdd(mask)
        # parameters: reuse the existing / module's parameter objects, so each keeps its type
        known = {str(p.Name): p for p in self._items(module, "Parameter")}
        known.update({str(p.Name): p for p in self._items(cfg, "Parameter")})
        cfg.ParameterClear()
        for name, value in link.params.items():
            p = known.get(name) or self.M.MTConParam(admin)
            p.Name = name
            if value is not None:  # None: a secret the CRM doesn't hold, keep MT5's value
                p.Value = str(value)
            cfg.ParameterAdd(p)
        cfg.TranslateClear()
        translate = self.M.MTConGatewayTranslate if link.kind == "gateway" else self.M.MTConFeederTranslate
        for t in link.translations:
            row = translate(admin)
            row.Source, row.Symbol = t["source"], t["symbol"]
            row.Digits, row.BidMarkup, row.AskMarkup = int(t.get("digits", 0)), int(t.get("bid_markup", 0)), \
                int(t.get("ask_markup", 0))
            cfg.TranslateAdd(row)

    def get_links(self, kind: str) -> list[MT5Link]:
        with self._admin("reading gateways and data feeds") as admin:
            return [self._convert_link(kind, c) for c in self._links(admin, kind) if c]

    def put_link(self, link: MT5Link, create: bool = False) -> MT5Link:
        import secrets

        api = LINK_API[link.kind][0]
        with self._admin("changing gateways and data feeds") as admin:
            module = self._module(admin, link.kind, link.module)
            cfg = self._link_cfg(admin, link.kind, link.name)
            if create:
                if cfg is not None:
                    raise MT5Error(f"{link.name} already exists on MT5")
                existing = [c for kind in LINK_API for c in self._links(admin, kind) if c]
                cfg = (self.M.MTConGateway if link.kind == "gateway" else self.M.MTConFeeder)(admin)
                cfg.Name, cfg.Module = link.name, link.module
                if link.kind == "gateway":
                    cfg.Gateway = str(module.Gateway)
                    cfg.ID = link.mt5_id or max((int(c.ID) for c in self._links(admin, "gateway") if c), default=0) + 1
                if not link.gateway_server:  # the module process listens locally, on a port no other one uses
                    ports = [int(str(c.GatewayServer).rpartition(":")[2] or 0) for c in existing
                             if str(c.GatewayServer).rpartition(":")[2].isdigit()]
                    link.gateway_server = f"127.0.0.1:{max(ports, default=16383) + 1}"
                if not link.gateway_login:
                    link.gateway_login = int(cfg.ID) if link.kind == "gateway" else 100 + len(existing)
                if link.gateway_password is None:
                    link.gateway_password = secrets.token_urlsafe(12)
            elif cfg is None:
                raise MT5Error(f"{link.name} not found on MT5", RET_NOT_FOUND)
            self._apply_link(admin, cfg, link, module)
            if not getattr(admin, f"{api}Update")(cfg):
                self._fail(f"{api}Update {link.name}")
            saved = self._link_cfg(admin, link.kind, link.name)
            if saved is None:
                raise MT5Error(f"MT5 accepted {link.name} but does not return it")
            return self._convert_link(link.kind, saved)

    def delete_link(self, kind: str, name: str) -> None:
        api = LINK_API[kind][0]
        with self._admin("deleting gateways and data feeds") as admin:
            pos = next((i for i, c in enumerate(self._links(admin, kind)) if c and str(c.Name) == name), None)
            if pos is None:
                raise MT5Error(f"{name} not found on MT5", RET_NOT_FOUND)
            if not getattr(admin, f"{api}Delete")(pos):  # GatewayDelete only takes a position
                self._fail(f"{api}Delete {name}")

    def get_link_modules(self, kind: str) -> list[MT5LinkModule]:
        api, server, login, _ = LINK_API[kind]
        out = []
        with self._admin("reading gateway and data feed modules") as admin:
            for pos in range(int(getattr(admin, f"{api}ModuleTotal")())):
                m = getattr(admin, f"{api}ModuleNext")(pos)
                if m:
                    out.append(MT5LinkModule(
                        kind=kind, name=str(m.Name), module=str(m.Module),
                        gateway_type=str(m.Gateway) if kind == "gateway" else "", description=str(m.Description),
                        vendor=str(m.Vendor), version=int(m.Version), server=str(getattr(m, server)),
                        login=str(getattr(m, login)),
                        params={str(p.Name): str(p.Value) for p in self._items(m, "Parameter")}))
        return out

    # ---------------------------------------------------------- routing rules
    def _routes(self, admin) -> list:
        return [admin.RouteNext(pos) for pos in range(int(admin.RouteTotal()))]

    @staticmethod
    def _convert_route(r) -> MT5Route:
        conditions = []
        for i in range(int(r.ConditionTotal())):
            c = r.ConditionNext(i)
            if c:
                conditions.append({"condition": int(c.Condition), "rule": int(c.Rule), "type": int(c.ValueType),
                                   "value": _get_value(c, "Value", c.ValueType)})
        dealers = [{"login": int(d.Login), "name": str(d.Name)}
                   for d in (r.DealerNext(i) for i in range(int(r.DealerTotal()))) if d]
        return MT5Route(name=str(r.Name), enabled=bool(int(r.Mode) & 1), types=int(r.Type), requests=int(r.Request),
                        action=int(r.Action), param_type=int(r.ParamType),
                        param=_get_value(r, "Param", r.ParamType) if int(r.ParamType) else "",
                        conditions=conditions, dealers=dealers)

    def _apply_route(self, admin, cfg, route: MT5Route) -> None:
        # ParamType and ValueType are read only: MT5 derives them from the action / condition, so those
        # are set first and the value is written in the type MT5 picked. A dealer's name comes from its login.
        cfg.Name, cfg.Mode = route.name, 1 if route.enabled else 0
        cfg.Type, cfg.Request, cfg.Action = route.types, route.requests, route.action
        if int(cfg.ParamType) and route.param != "":
            _set_value(cfg, "Param", cfg.ParamType, route.param)
        cfg.ConditionClear()
        for c in route.conditions:
            row = self.M.MTConCondition(admin)
            row.Condition, row.Rule = int(c["condition"]), int(c["rule"])
            _set_value(row, "Value", row.ValueType, str(c["value"]))
            cfg.ConditionAdd(row)
        cfg.DealerClear()
        for d in route.dealers:
            row = self.M.MTConRouteDealer(admin)
            row.Login = int(d["login"])
            cfg.DealerAdd(row)

    def _route_pos(self, admin, name: str) -> int:
        pos = next((i for i, r in enumerate(self._routes(admin)) if r and str(r.Name) == name), None)
        if pos is None:
            raise MT5Error(f"Routing rule {name} not found on MT5", RET_NOT_FOUND)
        return pos

    def get_routes(self) -> list[MT5Route]:
        with self._admin("reading routing rules") as admin:
            return [self._convert_route(r) for r in self._routes(admin) if r]

    def put_route(self, route: MT5Route, create: bool = False) -> MT5Route:
        with self._admin("changing routing rules") as admin:
            existing = next((r for r in self._routes(admin) if r and str(r.Name) == route.name), None)
            if create and existing is not None:
                raise MT5Error(f"Routing rule {route.name} already exists on MT5")
            if not create and existing is None:
                raise MT5Error(f"Routing rule {route.name} not found on MT5", RET_NOT_FOUND)
            cfg = existing or self.M.MTConRoute(admin)
            self._apply_route(admin, cfg, route)
            if not admin.RouteUpdate(cfg):  # a new rule goes to the bottom
                self._fail(f"RouteUpdate {route.name}")
            saved = next((r for r in self._routes(admin) if r and str(r.Name) == route.name), None)
            if saved is None:
                raise MT5Error(f"MT5 accepted routing rule {route.name} but does not return it")
            return self._convert_route(saved)

    def delete_route(self, name: str) -> None:
        with self._admin("deleting routing rules") as admin:
            if not admin.RouteDelete(self._route_pos(admin, name)):
                self._fail(f"RouteDelete {name}")

    def move_route(self, name: str, position: int) -> None:
        with self._admin("reordering routing rules") as admin:
            pos = self._route_pos(admin, name)
            position = max(0, min(position, int(admin.RouteTotal()) - 1))
            if position != pos and not admin.RouteShift(pos, position - pos):
                self._fail(f"RouteShift {name}")

    # ---------------------------------------------------------- platform health
    @staticmethod
    def _convert_component(s) -> MT5Component:
        return MT5Component(
            id=int(s.Id), name=str(s.Name), type=int(s.Type), address=str(s.Address), connected=bool(s.Connected),
            build=int(s.Build), os=str(s.OS), cpu=str(s.CPU), cores=int(s.CPUTotal), cpu_max=int(s.CPUUsageMax),
            cpu_critical=int(s.CPUUsageCritical), memory_total=int(s.MemoryTotal), memory_free=int(s.MemoryFree),
            memory_free_min=int(s.MemoryFreeMin), memory_free_critical=int(s.MemoryFreeCritical),
            disk_total=int(s.HDDTotal), disk_free=int(s.HDDFree), disk_free_critical=int(s.HDDFreeCritical),
            disk_read=int(s.HDDSpeedRead), disk_read_critical=int(s.HDDSpeedReadCritical),
            disk_write=int(s.HDDSpeedWrite), disk_write_critical=int(s.HDDSpeedWriteCritical),
            connections_max=int(s.ConnectsMax), connections_critical=int(s.ConnectsCritical),
            network_max=int(s.NetworkMax), network_critical=int(s.NetworkCritical),
            last_boot=_dt(s.LastBootTime) if s.LastBootTime else None)

    def get_server_health(self) -> list[MT5Component]:
        with self._admin("reading server health") as admin:
            return [self._convert_component(s) for s in (admin.NetServerNext(i) for i in range(admin.NetServerTotal()))
                    if s]

    def restart_component(self, component_id: int) -> None:
        with self._admin("restarting a server component") as admin:
            main = any(s and int(s.Id) == component_id and int(s.Type) == 0
                       for s in (admin.NetServerNext(i) for i in range(admin.NetServerTotal())))
            if not admin.NetServerRestart(int(component_id)):
                self._fail(f"NetServerRestart {component_id}")
        if main:
            self._connected = False  # the main trade server drops the CRM's connection too: reconnect

    def restart_links(self, kind: str) -> None:
        with self._admin("restarting gateways and data feeds") as admin:
            ok = admin.GatewayRestart() if kind == "gateway" else admin.FeederRestart()
            if not ok:
                self._fail("GatewayRestart" if kind == "gateway" else "FeederRestart")

    # ---------------------------------------------------------- market data
    def get_quotes(self, symbols):
        # The Manager API only receives ticks for "selected" symbols (like the terminal's Market Watch), so
        # TickLast is empty for the rest. SelectedAdd subscribes this connection; nothing changes on the server.
        # A newly selected symbol has its first tick a moment later.
        for symbol in symbols:
            self._select(symbol)
        out = {}
        for symbol in symbols:
            tick = self.api.TickLast(symbol)
            if _priced(tick):
                out[symbol] = (_dec(tick.bid), _dec(tick.ask))
        return out

    # ---------------------------------------------------------- events
    def subscribe_deals(self, callback: DealCallback) -> None:
        self._subscribers.append(callback)
        if self._deal_sink is None:
            self._deal_sink = _DealSink(self)
            self._check(self.api.DealSubscribe(self._deal_sink), "DealSubscribe")

    def unsubscribe_deals(self, callback: DealCallback) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def subscribe_trades(self, callback) -> None:
        self._trade_subscribers.append(callback)
        if self._trade_sinks is None:
            self._trade_sinks = (_PositionSink(self), _OrderSink(self))
            self._attach_trades()

    def unsubscribe_trades(self, callback) -> None:
        if callback in self._trade_subscribers:
            self._trade_subscribers.remove(callback)


def _priced(tick) -> bool:
    """A real tick: right after SelectedAdd, TickLast returns an empty one (bid and ask 0) until prices arrive."""
    return bool(tick) and tick.bid > 0 and tick.ask > 0


def _serialized(fn):
    """Run a gateway method under the gateway's lock (re-entrant, so methods may call each other)."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


def _serialize_api_calls(cls) -> None:
    """Wrap every public method that talks to MT5, plus _tick, so a call and its LastError() read are atomic and
    connect / reconnect cannot race. open_position / close_position lock their parts themselves (_tick, DealerSend)
    so the wait for the dealer's answer does not block other calls."""
    skip = {"open_position", "close_position"}
    for name, fn in list(vars(cls).items()):
        if callable(fn) and not isinstance(fn, (staticmethod, classmethod)) and (
                (not name.startswith("_") and name not in skip) or name == "_tick"):
            setattr(cls, name, _serialized(fn))


_serialize_api_calls(ManagerGateway)
