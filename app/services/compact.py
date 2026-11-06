"""Trading accounts > Compact: replace an account's old history with one summary deal per group.

Balance, credit and other money deals are grouped by type and comment, trades by symbol, and each group becomes one
deal on MT5 whose amount is the group's total (a trade's total is its profit, commission, swap and fee, and its summary
is a correction). A summary deal is dated the day of the compact; its comment gives the period it covers. Compacting
again adds to the same summary: its start date stays, its end date and total move on, and it is dated that day.

How MT5 was tested (Testing server, 2026-11-06, on throwaway accounts): DealDelete and DealUpdate leave the account's
balance as it is, and UserBalanceCheck compares that balance with the sum of the history. So one deal of each group is
rewritten as the summary and the others are deleted: the balance never moves and the history still adds up to it.

- Kept: open positions and every deal of them (also when partly closed), pending orders, and anything in a position
  that has a deal outside the chosen dates, so that a position is never cut in half.
- Exact copies of all that goes off MT5 are saved in compacted_records first, and stay in Flow.
- An account whose balance and history already differ on MT5 is left alone, and each account is checked again after.
- Flow's own rows are written after MT5 is finished, and syncs do not bring back what was compacted.
"""

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import ServiceError
from app.db import utcnow
from app.models import CompactedRecord, CompactSummary, Deal, MT5Server, Order, Position, TradingAccount
from app.models.trading import DEAL_CORRECTION, TRADE_ACTIONS
from app.mt5 import MT5Error, MT5Gateway
from app.mt5.gateway import MT5Deal, MT5Order
from app.services import mt5_calls
from app.services.clients import visible_logins
from app.services.sync_runs import report

log = logging.getLogger(__name__)

BEGINNING = datetime(2000, 1, 1)
MAX_ACCOUNTS = 200
COMMENT_LENGTH = 31  # what MT5 keeps
SAVE_ATTEMPTS = 5
TYPES = {2: "Balance", 3: "Credit", 4: "Charge", 5: "Correction", 6: "Bonus", 7: "Commission"}
GROUPINGS = {"comment": "Comment (balance, credit, corrections)", "symbol": "Symbol (trades)"}


@dataclass
class Line:
    """One group of deals, and the summary it becomes."""

    key: str
    action: int
    label: str
    symbol: str
    rows: list[MT5Deal] = field(default_factory=list)
    summary: CompactSummary | None = None

    @property
    def added(self) -> Decimal:
        return sum((amount(d) for d in self.rows), Decimal("0"))

    @property
    def total(self) -> Decimal:
        return self.added + (self.summary.amount if self.summary else 0)

    @property
    def start(self) -> datetime:
        return min([d.time for d in self.rows] + ([self.summary.date_from] if self.summary else []))

    @property
    def stop(self) -> datetime:
        return max([d.time for d in self.rows] + ([self.summary.date_to] if self.summary else []))


@dataclass
class Plan:
    """What compacting one account would do. `problem` set: it is left alone."""

    login: int
    lines: list[Line] = field(default_factory=list)
    orders: list[MT5Order] = field(default_factory=list)
    positions: set[int] = field(default_factory=set)
    open_positions: int = 0
    pending_orders: int = 0
    kept_deals: int = 0
    problem: str = ""

    @property
    def deals(self) -> int:
        return sum(len(line.rows) for line in self.lines)


def amount(deal: MT5Deal) -> Decimal:
    return deal.profit + deal.commission + deal.swap + deal.fee


def _money_comment(text: str) -> str:
    """A comment without its numbers, so "Deposit #12" and "Deposit #13" are one group."""
    return re.sub(r"\s+", " ", re.sub(r"[#\d]+", " ", text)).strip(" -:")


def comment_for(label: str, start: datetime, stop: datetime) -> str:
    dates = f"{start:%y%m%d}-{stop:%y%m%d}"
    return f"{label[:COMMENT_LENGTH - len(dates) - 1].strip()} {dates}"


def _day(value: str, end: bool = False) -> datetime | None:
    if not value:
        return None
    try:
        day = datetime.fromisoformat(value)
    except ValueError:
        raise ServiceError(f"'{value}' is not a date")
    return day.replace(hour=23, minute=59, second=59) if end else day


def _line_of(deal: MT5Deal, by: list[str]) -> tuple[int, str, str]:
    """The group of a deal: (the summary's type, label, symbol)."""
    if deal.action in TRADE_ACTIONS:
        symbol = deal.symbol if "symbol" in by else ""
        return DEAL_CORRECTION, f"Correction {symbol}".strip(), symbol
    name = TYPES.get(deal.action, "Other")
    return deal.action, (_money_comment(deal.comment) if "comment" in by else "") or name, ""


def make_plan(db: Session, gw: MT5Gateway, server: MT5Server, login: int, date_from: str = "", date_to: str = "",
              by: list[str] | None = None) -> Plan:
    """Reads MT5 only, so the preview and the run see the same thing."""
    by = GROUPINGS if by is None else by
    start, stop = _day(date_from) or BEGINNING, _day(date_to, end=True) or utcnow() + timedelta(days=2)
    plan = Plan(login)
    is_ok, balance, history = gw.check_balance(login)
    if not is_ok:
        plan.problem = f"MT5's balance ({balance}) and the sum of its history ({history}) already differ"
        return plan
    deals = gw.get_deals([login], BEGINNING, utcnow() + timedelta(days=2))
    summaries = {s.key: s for s in db.scalars(select(CompactSummary).where(CompactSummary.server_id == server.id,
                                                                           CompactSummary.login == login))}
    tickets = {d.ticket for d in deals}
    lost = [s for s in summaries.values() if s.ticket not in tickets]
    if lost:
        plan.problem = f"its summary deal {lost[0].ticket} is no longer on MT5"
        return plan
    summary_tickets = {s.ticket for s in summaries.values()}
    open_ids = {p.position_id for p in gw.get_positions([login])}
    pending = gw.get_orders([login])
    plan.open_positions, plan.pending_orders = len(open_ids), len(pending)

    by_position: dict[int, list[MT5Deal]] = {}
    for deal in deals:
        if deal.action in TRADE_ACTIONS:
            by_position.setdefault(deal.position_id, []).append(deal)
    closed = {p for p, rows in by_position.items()
              if p not in open_ids and all(start <= d.time <= stop for d in rows)}
    lines: dict[str, Line] = {}
    for deal in sorted(deals, key=lambda d: (d.time_msc, d.ticket)):
        if deal.ticket in summary_tickets:
            continue
        chosen = deal.position_id in closed if deal.action in TRADE_ACTIONS else start <= deal.time <= stop
        if not chosen:
            plan.kept_deals += 1
            continue
        action, label, symbol = _line_of(deal, by)
        key = f"{action}|{label}|{symbol}"[:128]
        line = lines.setdefault(key, Line(key, action, label, symbol, summary=summaries.get(key)))
        line.rows.append(deal)
    plan.lines = list(lines.values())
    plan.positions = closed
    plan.orders = [o for o in gw.get_order_history([login], BEGINNING, utcnow() + timedelta(days=2))
                   if o.position_id in closed
                   or (not o.position_id and o.time_setup and start <= o.time_setup <= stop)]
    return plan


def choose(db: Session, actor, server: MT5Server, logins: str = "", group_id: int | None = None) -> list[int]:
    """The accounts to compact: the logins typed, and those of an account group; only accounts the actor may see."""
    typed = [int(n) for n in re.findall(r"\d+", logins)]
    query = select(TradingAccount.login).where(TradingAccount.server_id == server.id,
                                               TradingAccount.login.in_(visible_logins(actor)))
    found: set[int] = set()
    if typed:
        found |= set(db.scalars(query.where(TradingAccount.login.in_(typed))))
    if group_id:
        found |= set(db.scalars(query.where(TradingAccount.account_group_id == group_id)))
    if not found:
        raise ServiceError("Choose at least one trading account of this server")
    if len(found) > MAX_ACCOUNTS:
        raise ServiceError(f"At most {MAX_ACCOUNTS} accounts at a time")
    return sorted(found)


def _plain(row) -> dict:
    return json.loads(json.dumps(asdict(row), default=str))


def _keep(db: Session, server: MT5Server, kind: str, ticket: int, login: int, when, symbol: str, action: int,
          money: Decimal, comment: str, data: dict) -> None:
    db.merge(CompactedRecord(server_id=server.id, kind=kind, ticket=ticket, login=login, time=when, symbol=symbol or "",
                             action=action, amount=money, comment=comment or "", data=data, compacted_at=utcnow()))


def _saved(db: Session, work) -> None:
    """Flow's side of what MT5 has already done: it collides with running syncs, so it is tried again."""
    for attempt in range(1, SAVE_ATTEMPTS + 1):
        try:
            work()
            db.commit()
            return
        except OperationalError:
            db.rollback()
            if attempt == SAVE_ATTEMPTS:
                raise
            time.sleep(attempt / 2)


def _compact_line(db: Session, gw: MT5Gateway, server: MT5Server, login: int, line: Line) -> dict[int, str]:
    """Delete the group's other deals, then rewrite one as the summary. The total counts only what MT5 did delete, so
    a deal MT5 refuses to delete stays as it was and the history still adds up. Flow is not touched between the two,
    because a failure there would leave MT5's history short of the balance. Returns the refused deals."""
    holder = line.summary.ticket if line.summary else line.rows[-1].ticket
    others = [d.ticket for d in line.rows if d.ticket != holder]
    refused = gw.delete_deals(others) if others else {}
    line.rows = [d for d in line.rows if d.ticket not in refused]
    start, stop, total = line.start, line.stop, line.total
    text, today = comment_for(line.label, start, stop), gw.server_time()
    try:
        gw.rewrite_deal(holder, action=line.action, amount=total, comment=text, when=today, symbol=line.symbol)
    except MT5Error as exc:
        raise MT5Error(f"deal {holder} should now be {total} but was not rewritten: {exc}", exc.retcode) from exc

    tickets, gone = [d.ticket for d in line.rows], [t for t in others if t not in refused]

    def save():
        if refused:
            db.execute(delete(CompactedRecord).where(CompactedRecord.server_id == server.id, CompactedRecord.kind == "deal",
                                                     CompactedRecord.ticket.in_(refused)))
        if gone:
            db.execute(delete(Deal).where(Deal.server_id == server.id, Deal.ticket.in_(gone)))
        summary = line.summary or CompactSummary(server_id=server.id, login=login, key=line.key, action=line.action,
                                                 label=line.label, symbol=line.symbol, ticket=holder, deals=0,
                                                 created_at=utcnow())
        summary.date_from, summary.date_to, summary.amount, summary.updated_at = start, stop, total, utcnow()
        summary.deals += len(tickets)
        db.add(summary)
        db.flush()
        db.execute(update(CompactedRecord).where(CompactedRecord.server_id == server.id, CompactedRecord.kind == "deal",
                                                 CompactedRecord.ticket.in_(tickets)).values(summary_id=summary.id))
        db.execute(update(Deal).where(Deal.server_id == server.id, Deal.ticket == holder).values(
            action=line.action, entry=0, symbol=line.symbol, volume=0, price=0, profit=total, commission=0, swap=0, fee=0,
            position_id=0, order=0, comment=text, time=today,
            time_msc=int(today.replace(tzinfo=timezone.utc).timestamp()) * 1000))
    _saved(db, save)
    return refused


def _compact_orders(db: Session, gw: MT5Gateway, server: MT5Server, plan: Plan) -> int:
    def copies():
        for o in plan.orders:
            _keep(db, server, "order", o.ticket, o.login, o.time_setup, o.symbol, o.type, Decimal("0"), o.comment, _plain(o))
    _saved(db, copies)
    refused = gw.delete_orders([o.ticket for o in plan.orders]) if plan.orders else {}
    gone = [o.ticket for o in plan.orders if o.ticket not in refused]

    def save():
        if refused:
            db.execute(delete(CompactedRecord).where(CompactedRecord.server_id == server.id, CompactedRecord.kind == "order",
                                                     CompactedRecord.ticket.in_(refused)))
        if gone:
            db.execute(delete(Order).where(Order.server_id == server.id, Order.ticket.in_(gone)))
    _saved(db, save)
    return len(gone)


def _compact_positions(db: Session, server: MT5Server, plan: Plan) -> None:
    """Flow's closed positions (MT5 builds its own position history from the deals and orders)."""
    def save():
        rows = db.scalars(select(Position).where(Position.server_id == server.id, Position.position_id.in_(plan.positions),
                                                 Position.login == plan.login, Position.is_open.is_(False))).all()
        for p in rows:
            values = {c.name: getattr(p, c.name) for c in Position.__table__.columns}
            _keep(db, server, "position", p.position_id, p.login, p.closed_at or p.time_create, p.symbol, p.action,
                  p.profit, p.comment, json.loads(json.dumps(values, default=str)))
        if rows:
            db.flush()
            db.execute(delete(Position).where(Position.server_id == server.id,
                                              Position.position_id.in_([p.position_id for p in rows])))
    _saved(db, save)


def _compact_account(db: Session, gw: MT5Gateway, actor, server: MT5Server, plan: Plan, result: dict) -> None:
    def copies():  # saved before anything leaves MT5
        for line in plan.lines:
            for d in line.rows:
                _keep(db, server, "deal", d.ticket, d.login, d.time, d.symbol, d.action, amount(d), d.comment, _plain(d))
    _saved(db, copies)
    refused: dict[int, str] = {}
    positions_of = {d.ticket: d.position_id for line in plan.lines for d in line.rows}
    for line in plan.lines:
        refused.update(_compact_line(db, gw, server, plan.login, line))
    plan.positions -= {positions_of[t] for t in refused}
    _compact_positions(db, server, plan)
    orders = _compact_orders(db, gw, server, plan)
    _saved(db, lambda: audit(db, actor, "compact.account", "mt5_server", server.id,
                             {"login": plan.login, "orders": orders,
                              "summaries": [{"text": x.label, "deals": len(x.rows)} for x in plan.lines]}))
    result["accounts"] += 1
    result["lines"] += len(plan.lines)
    result["deals"] += plan.deals
    result["orders"] += orders
    for ticket, why in refused.items():
        result["failed"] += 1
        result["errors"].append(f"{plan.login}: deal {ticket} stayed: {why}")


def run(db: Session, actor, server: MT5Server, logins: list[int], date_from: str = "", date_to: str = "",
        by: list[str] | None = None, gw: MT5Gateway | None = None) -> dict:
    """Compact these accounts, one by one: a problem with one leaves the others to go on."""
    gw = mt5_calls.gateway(server, gw)
    allowed = set(db.scalars(select(TradingAccount.login).where(TradingAccount.server_id == server.id,
                                                                TradingAccount.login.in_(logins),
                                                                TradingAccount.login.in_(visible_logins(actor)))))
    wanted = [n for n in logins if n in allowed]
    result = {"accounts": 0, "lines": 0, "deals": 0, "orders": 0, "failed": 0, "skipped": 0, "errors": []}
    report(0, len(wanted))
    for i, login in enumerate(wanted):
        try:
            plan = make_plan(db, gw, server, login, date_from, date_to, by)
            if plan.problem:
                result["skipped"] += 1
                result["errors"].append(f"{login}: {plan.problem}")
            elif plan.lines or plan.orders:
                _compact_account(db, gw, actor, server, plan, result)
                is_ok, balance, history = gw.check_balance(login)
                if not is_ok:
                    result["failed"] += 1
                    result["errors"].append(f"{login}: check this account: its balance ({balance}) and history ({history}) "
                                            "differ after compacting")
        except Exception as exc:  # the other accounts still go on
            log.exception("compact of %s failed", login)
            db.rollback()
            result["failed"] += 1
            result["errors"].append(f"{login}: {getattr(exc, 'message', None) or exc}"[:300])
        report(i + 1, len(wanted))
    return result
