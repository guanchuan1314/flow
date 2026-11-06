"""Delete orders and deals, on MT5 first and then in the CRM (Trades > Orders or Deals > Controls).

What is kept, however it was selected:
- an order that was not cancelled, rejected or expired, while its position is still open;
- a deal while its position is still open;
- balance and credit deals: they are the account's money history, and payments point at them.

The list is the CRM's copy, so each batch checks the open positions on MT5 again just before deleting. A row
leaves the CRM only once MT5 has deleted it, or no longer has it. A delete cannot be undone, and nothing here
changes a balance: the balance MT5 holds stays as it is.
"""

from datetime import datetime

from sqlalchemy import delete, false, func, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import ServiceError
from app.core.masks import filter_comment, filter_login_symbol
from app.models import Deal, MT5Server, Order, Position
from app.models.trading import TRADE_ACTIONS
from app.mt5 import MT5Error, MT5Gateway
from app.services import mt5_calls
from app.services.clients import visible_rows
from app.services.sync_runs import report

BATCH = 200
MAX_PER_RUN = 20000
ENDED_STATES = (2, 5, 6)  # cancelled, rejected, expired: deletable whatever their position does
FILLED_STATES = (3, 4)  # partial, filled
ORDER_STATUSES = {"open": "Open (pending)", "cancelled": "Cancelled", "rejected": "Rejected", "expired": "Expired",
                  "filled": "Filled"}
_ORDER_STATE = {"cancelled": 2, "rejected": 5, "expired": 6}
DEAL_STATUSES = {"closed": "Position closed", "open": "Position open", "balance": "Balance & credit"}
FILTERS = ("status", "login", "symbol", "comment", "date_from", "date_to")
KEPT_POSITION = "its position is still open"
KEPT_MONEY = "balance and credit deals are kept"


def _day(value: str, end: bool = False) -> datetime | None:
    d = datetime.fromisoformat(value)
    return d.replace(hour=23, minute=59, second=59) if end else d


def _dated(query, column, filters: dict):
    """The from / to days; a date that cannot be read matches nothing, so it never widens a delete."""
    try:
        start, stop = (_day(filters[k], k == "date_to") if filters.get(k) else None for k in ("date_from", "date_to"))
    except ValueError:
        return query.where(false())
    if start:
        query = query.where(column >= start)
    return query.where(column <= stop) if stop else query


def _open_ids(server_id: int):
    return select(Position.position_id).where(Position.server_id == server_id, Position.is_open.is_(True))


def _matching(query, model, filters: dict):
    query = filter_login_symbol(query, model, filters.get("login", ""), filters.get("symbol", ""))
    return filter_comment(query, model, filters.get("comment", ""))


def orders_query(server_id: int, actor, filters: dict):
    query = select(Order).where(Order.server_id == server_id, visible_rows(actor, Order))
    status = filters.get("status", "")
    if status == "open":
        query = query.where(Order.is_open.is_(True))
    elif status in _ORDER_STATE:
        query = query.where(Order.is_open.is_(False), Order.state == _ORDER_STATE[status])
    elif status == "filled":
        query = query.where(Order.is_open.is_(False), Order.state.in_(FILLED_STATES))
    return _dated(_matching(query, Order, filters), Order.time_setup, filters)


def deals_query(server_id: int, actor, filters: dict):
    query = select(Deal).where(Deal.server_id == server_id, visible_rows(actor, Deal))
    status = filters.get("status", "")
    if status == "balance":
        query = query.where(Deal.action.not_in(TRADE_ACTIONS))
    elif status in ("open", "closed"):
        opened = Deal.position_id.in_(_open_ids(server_id))
        query = query.where(Deal.action.in_(TRADE_ACTIONS), opened if status == "open" else ~opened)
    return _dated(_matching(query, Deal, filters), Deal.time, filters)


def _query(what: str, server_id: int, actor, filters: dict):
    return (orders_query if what == "orders" else deals_query)(server_id, actor, filters)


def kept_reason(what: str, row, open_positions) -> str | None:
    """Why this order or deal has to stay; None if it may go."""
    if what == "deals":
        if row.action not in TRADE_ACTIONS:
            return KEPT_MONEY
    elif row.state in ENDED_STATES:
        return None
    return KEPT_POSITION if row.position_id in open_positions else None


def open_positions(db: Session, server_id: int, rows) -> set[int]:
    """Which positions of these rows the CRM has as open."""
    ids = {r.position_id for r in rows if r.position_id}
    return set(db.scalars(_open_ids(server_id).where(Position.position_id.in_(ids)))) if ids else set()


def count_kept(db: Session, what: str, server_id: int, actor, filters: dict) -> int:
    """How many of the rows matching the filters have to stay (as the CRM sees it now)."""
    query = _query(what, server_id, actor, filters)
    opened = (Order if what == "orders" else Deal).position_id.in_(_open_ids(server_id))
    kept = (opened & Order.state.not_in(ENDED_STATES)) if what == "orders" else (Deal.action.not_in(TRADE_ACTIONS) | opened)
    return db.scalar(query.where(kept).with_only_columns(func.count())) or 0


def run(db: Session, actor, server: MT5Server, what: str, tickets: list[int] | None = None,
        filters: dict | None = None, gw: MT5Gateway | None = None) -> dict:
    """Delete the ticked `tickets`, or everything matching `filters` (at most MAX_PER_RUN), batch by batch."""
    if what not in ("orders", "deals"):
        raise ServiceError("Choose orders or deals")
    filters = filters or {}
    if tickets is None and not any(filters.get(k) for k in FILTERS):
        raise ServiceError("Set at least one filter before deleting everything that matches")
    model = Order if what == "orders" else Deal
    query = _query(what, server.id, actor, filters).with_only_columns(model.ticket).order_by(model.ticket)
    if tickets is not None:
        query = query.where(model.ticket.in_(tickets))
    found = list(db.scalars(query.limit(MAX_PER_RUN + 1)))
    remaining = max(0, len(found) - MAX_PER_RUN)
    found = found[:MAX_PER_RUN]
    gw = mt5_calls.gateway(server, gw)
    result = {"what": what, "done": 0, "kept": 0, "failed": 0, "errors": [], "remaining": remaining}
    report(0, len(found))
    for start in range(0, len(found), BATCH):
        rows = list(db.scalars(select(model).where(model.server_id == server.id,
                                                   model.ticket.in_(found[start:start + BATCH]))))
        try:
            live = {p.position_id for p in gw.get_positions(sorted({r.login for r in rows}))}
            live |= open_positions(db, server.id, rows)
            allowed = [r.ticket for r in rows if kept_reason(what, r, live) is None]
            refused = (gw.delete_orders if what == "orders" else gw.delete_deals)(allowed) if allowed else {}
        except MT5Error as exc:  # nothing of this batch changed in the CRM; earlier batches stay done
            db.rollback()
            result["errors"].append(str(exc))
            result["failed"] += len(found) - start
            break
        done = [t for t in allowed if t not in refused]
        if done:
            db.execute(delete(model).where(model.server_id == server.id, model.ticket.in_(done)))
            audit(db, actor, f"trade_cleanup.{what}", "mt5_server", server.id, {"tickets": done})
        db.commit()
        result["done"] += len(done)
        result["kept"] += len(rows) - len(allowed)
        result["failed"] += len(refused)
        result["errors"] += [f"{t}: {why}" for t, why in refused.items()][:10 - len(result["errors"])]
        report(start + len(rows), len(found))
    return result
