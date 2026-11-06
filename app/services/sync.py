"""Deals / orders / positions / account-summary sync from MT5 into the CRM database."""

import logging
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.models import AccountStatus, CompactedRecord, Deal, MT5Server, Order, Position, SyncState, TradingAccount
from app.mt5 import MT5Error, MT5Gateway, get_gateway
from app.mt5.gateway import MT5Deal, MT5Order
from app.services import activity, events
from app.services.accounts import apply_account_info
from app.services.servers import the_server
from app.services.sync_runs import report

log = logging.getLogger(__name__)

CHUNK = 100
HISTORY_START = datetime(2000, 1, 1)
_ingest_lock = threading.Lock()  # push and poll paths may deliver the same deal concurrently


def _chunks(items: list, size: int = CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def tracked_logins(db: Session, server_id: int | None = None) -> list[int]:
    q = select(TradingAccount.login).where(TradingAccount.status != AccountStatus.archived)
    if server_id is not None:
        q = q.where(TradingAccount.server_id == server_id)
    return list(db.scalars(q))


def enabled_servers(db: Session) -> list[MT5Server]:
    return list(db.scalars(select(MT5Server).where(MT5Server.enabled.is_(True)).order_by(MT5Server.id)))


def default_server_id(db: Session) -> int:
    server = the_server(db)
    if server is None:
        raise MT5Error("no MT5 server is configured")
    return server.id


def _deal_values(d: MT5Deal, server_id: int, now: datetime) -> dict:
    return dict(
        server_id=server_id, ticket=d.ticket, login=d.login, time=d.time, time_msc=d.time_msc, action=d.action,
        entry=d.entry, symbol=d.symbol, volume=d.volume, price=d.price, profit=d.profit, commission=d.commission,
        swap=d.swap, fee=d.fee, position_id=d.position_id, order=d.order, reason=d.reason,
        comment=d.comment[:64], contract_size=d.contract_size, tick_value=d.tick_value,
        tick_size=d.tick_size, digits=d.digits, synced_at=now,
    )


def _state(db: Session, login: int, server_id: int | None = None) -> SyncState:
    state = db.get(SyncState, login)
    if state is None:
        state = SyncState(login=login, server_id=server_id)
        db.add(state)
    elif server_id is not None:
        state.server_id = server_id
    return state


def _bump_last_deal_time(db: Session, latest: dict[int, datetime], server_id: int) -> None:
    """Move sync_state.last_deal_time forward; an upsert, so a concurrent ingest cannot collide on the insert."""
    if not latest:
        return
    stmt = pg_insert(SyncState).values([{"login": login, "server_id": server_id, "last_deal_time": t}
                                        for login, t in latest.items()])
    stmt = stmt.on_conflict_do_update(index_elements=[SyncState.login], set_={
        "server_id": stmt.excluded.server_id,
        "last_deal_time": func.greatest(func.coalesce(SyncState.last_deal_time, stmt.excluded.last_deal_time),
                                        stmt.excluded.last_deal_time)})
    db.scalars(stmt.returning(SyncState), execution_options={"populate_existing": True}).all()  # refresh loaded rows


def _compacted(db: Session, server_id: int, kind: str, tickets: list[int]) -> set[int]:
    """Of these tickets, those compacting removed from MT5: a sync that read them just before must not add them back."""
    found: set[int] = set()
    for i in range(0, len(tickets), 1000):
        found |= set(db.scalars(select(CompactedRecord.ticket).where(
            CompactedRecord.server_id == server_id, CompactedRecord.kind == kind,
            CompactedRecord.ticket.in_(tickets[i:i + 1000]))))
    return found


def ingest_deals(db: Session, mt5_deals: list[MT5Deal], publish: bool = True,
                 server_id: int | None = None) -> list[Deal]:
    """Insert deals of one server not yet stored and publish the new ones. Idempotent.

    INSERT ... ON CONFLICT DO NOTHING RETURNING: when the API and the worker (push and poll) ingest the same deal
    at once, the database decides which one stored it; only that caller records activity and publishes it."""
    if not mt5_deals:
        return []
    server_id = server_id if server_id is not None else default_server_id(db)
    now = utcnow()
    gone = _compacted(db, server_id, "deal", [d.ticket for d in mt5_deals])
    values = list({d.ticket: _deal_values(d, server_id, now) for d in mt5_deals if d.ticket not in gone}.values())
    new: list[Deal] = []
    with _ingest_lock:
        for chunk in _chunks(values, 1000):
            stmt = (pg_insert(Deal).values(chunk)
                    .on_conflict_do_nothing(index_elements=[Deal.server_id, Deal.ticket]).returning(Deal))
            new.extend(db.scalars(stmt, execution_options={"populate_existing": True}).all())
        latest: dict[int, datetime] = {}
        for row in new:
            latest[row.login] = max(latest.get(row.login, row.time), row.time)
        activity.record_trades(db, new)
        _bump_last_deal_time(db, latest, server_id)
        db.commit()
    rows = sorted(new, key=lambda r: (r.time_msc, r.ticket))
    if publish and rows:
        events.publish(db, rows)
    return rows


def ingest_orders(db: Session, mt5_orders: list[MT5Order], server_id: int, is_open: bool) -> int:
    """Insert or update orders of one server. Returns how many were new. Idempotent."""
    if not mt5_orders:
        return 0
    now = utcnow()
    gone = _compacted(db, server_id, "order", [o.ticket for o in mt5_orders])
    by_ticket = {o.ticket: o for o in mt5_orders if o.ticket not in gone}
    existing: dict[int, Order] = {}
    for chunk in _chunks(list(by_ticket), 500):
        existing.update({r.ticket: r for r in db.scalars(select(Order).where(Order.server_id == server_id,
                                                                             Order.ticket.in_(chunk)))})
    for ticket, o in by_ticket.items():
        row = existing.get(ticket) or Order(server_id=server_id, ticket=ticket)
        row.login, row.symbol, row.type, row.state = o.login, o.symbol, o.type, o.state
        row.volume_initial, row.volume_current, row.price_order = o.volume_initial, o.volume_current, o.price_order
        row.price_current, row.sl, row.tp = o.price_current, o.sl, o.tp
        row.time_setup, row.time_done, row.position_id = o.time_setup, o.time_done, o.position_id
        row.reason, row.comment, row.is_open, row.synced_at = o.reason, o.comment[:64], is_open, now
        db.add(row)
    db.commit()
    return len(by_ticket.keys() - existing.keys())


def _resolve(db: Session, gw: MT5Gateway | None, server_id: int | None) -> tuple[MT5Gateway, int]:
    server_id = server_id if server_id is not None else default_server_id(db)
    return (gw or get_gateway(server_id)), server_id


def sync_deals(db: Session, gw: MT5Gateway | None = None, logins: list[int] | None = None,
               server_id: int | None = None) -> int:
    s = get_settings()
    gw, server_id = _resolve(db, gw, server_id)
    logins = logins if logins is not None else tracked_logins(db, server_id)
    now = utcnow()
    overlap = timedelta(seconds=s.sync_overlap_seconds)
    total = 0
    for chunk in _chunks(logins):
        created = dict(db.execute(select(TradingAccount.login, TradingAccount.created_at)
                                  .where(TradingAccount.login.in_(chunk))).all())
        states = {st.login: st for st in db.scalars(select(SyncState).where(SyncState.login.in_(chunk)))}
        starts = []
        for login in chunk:
            st = states.get(login)
            if st and st.last_deal_time:
                starts.append(st.last_deal_time - overlap)
            elif login in created:
                starts.append(created[login] - timedelta(days=1))
            else:
                starts.append(HISTORY_START)
        try:
            deals = gw.get_deals(chunk, min(starts), now + timedelta(minutes=1))
        except MT5Error as exc:
            log.error("deal sync failed for %s logins: %s", len(chunk), exc)
            for login in chunk:
                _state(db, login, server_id).last_error = str(exc)[:512]
            db.commit()
            continue
        total += len(ingest_deals(db, deals, server_id=server_id))
        try:  # orders that left the open list in the same period (filled, cancelled, expired)
            ingest_orders(db, gw.get_order_history(chunk, min(starts), now + timedelta(minutes=1)), server_id, False)
        except MT5Error as exc:
            log.error("order history sync failed for %s logins: %s", len(chunk), exc)
        for login in chunk:
            st = _state(db, login)
            st.last_deal_sync_at, st.last_error = now, None
        db.commit()
    return total


def sync_positions(db: Session, gw: MT5Gateway | None = None, logins: list[int] | None = None,
                   server_id: int | None = None) -> int:
    gw, server_id = _resolve(db, gw, server_id)
    logins = logins if logins is not None else tracked_logins(db, server_id)
    now = utcnow()
    count = 0
    for chunk in _chunks(logins):
        try:
            positions = gw.get_positions(chunk)
        except MT5Error as exc:
            log.error("position sync failed: %s", exc)
            continue
        seen = {p.position_id for p in positions}
        known: dict[int, Position] = {}
        for ids in _chunks(list(seen), 1000):
            known.update({r.position_id: r for r in db.scalars(
                select(Position).where(Position.server_id == server_id, Position.position_id.in_(ids)))})
        for p in positions:
            row = known.get(p.position_id) or Position(server_id=server_id, position_id=p.position_id)
            row.login, row.symbol, row.action, row.volume = p.login, p.symbol, p.action, p.volume
            row.price_open, row.price_current, row.sl, row.tp = p.price_open, p.price_current, p.sl, p.tp
            row.profit, row.swap, row.time_create, row.time_update = p.profit, p.swap, p.time_create, p.time_update
            row.comment, row.is_open, row.closed_at, row.synced_at = p.comment[:64], True, None, now
            db.add(row)
        stale = db.scalars(select(Position).where(Position.server_id == server_id, Position.login.in_(chunk),
                                                  Position.is_open.is_(True)))
        for row in stale:
            if row.position_id not in seen:
                row.is_open, row.closed_at, row.synced_at = False, now, now
        for login in chunk:
            _state(db, login).last_position_sync_at = now
        db.commit()
        count += len(positions)
    return count


def sync_orders(db: Session, gw: MT5Gateway | None = None, logins: list[int] | None = None,
                server_id: int | None = None) -> int:
    """Open (pending) orders. Orders no longer open are flagged; the history sync fills in how they ended."""
    gw, server_id = _resolve(db, gw, server_id)
    logins = logins if logins is not None else tracked_logins(db, server_id)
    now = utcnow()
    count = 0
    for chunk in _chunks(logins):
        try:
            orders = gw.get_orders(chunk)
        except MT5Error as exc:
            log.error("order sync failed: %s", exc)
            continue
        ingest_orders(db, orders, server_id, True)
        seen = {o.ticket for o in orders}
        for row in db.scalars(select(Order).where(Order.server_id == server_id, Order.login.in_(chunk),
                                                  Order.is_open.is_(True))):
            if row.ticket not in seen:
                row.is_open, row.synced_at = False, now
        for login in chunk:
            _state(db, login).last_order_sync_at = now
        db.commit()
        count += len(orders)
    return count


def sync_accounts(db: Session, gw: MT5Gateway | None = None, logins: list[int] | None = None,
                  server_id: int | None = None) -> int:
    gw, server_id = _resolve(db, gw, server_id)
    logins = logins if logins is not None else tracked_logins(db, server_id)
    count = 0
    for chunk in _chunks(logins):
        try:
            infos = {i.login: i for i in gw.get_accounts(chunk)}
        except MT5Error as exc:
            log.error("account sync failed: %s", exc)
            continue
        for acc in db.scalars(select(TradingAccount).where(TradingAccount.login.in_(chunk))):
            if acc.login in infos:
                apply_account_info(acc, infos[acc.login])
                count += 1
        db.commit()
    return count


def active_logins(db: Session, server_id: int, since: datetime) -> set[int]:
    """Accounts that can change any moment: an open position or order, or a deal since `since`."""
    found = set(db.scalars(select(Position.login).where(Position.server_id == server_id, Position.is_open.is_(True))))
    found.update(db.scalars(select(Order.login).where(Order.server_id == server_id, Order.is_open.is_(True))))
    found.update(db.scalars(select(SyncState.login).join(TradingAccount, TradingAccount.login == SyncState.login)
                            .where(TradingAccount.server_id == server_id, SyncState.last_deal_time >= since)))
    return found


def poll(db: Session, gw: MT5Gateway, server_id: int) -> dict:
    """One worker pass over a server: every active account, plus the idle accounts whose turn it is.

    Idle accounts are polled every `sync_idle_interval_seconds`, a slice per pass so the load stays even.
    Returns full_sync's totals plus what was polled and how long it took."""
    s = get_settings()
    started, now = time.perf_counter(), utcnow()
    tracked = tracked_logins(db, server_id)  # once per pass; the slices below are subsets of it
    active = active_logins(db, server_id, now - timedelta(minutes=s.sync_active_minutes)) & set(tracked)
    idle = [x for x in tracked if x not in active]
    due_before = now - timedelta(seconds=s.sync_idle_interval_seconds)
    last = dict(db.execute(select(SyncState.login, SyncState.last_deal_sync_at)
                           .join(TradingAccount, TradingAccount.login == SyncState.login)
                           .where(TradingAccount.server_id == server_id)).all())
    due = sorted((x for x in idle if last.get(x) is None or last[x] <= due_before),
                 key=lambda x: last.get(x) or HISTORY_START)
    share = -(-len(idle) * max(1, s.sync_interval_seconds) // max(1, s.sync_idle_interval_seconds))
    due = due[:max(share, CHUNK)]
    logins = sorted(active) + due
    totals = _sync_logins(db, gw, logins, server_id, _empty_totals()) if logins else {"new_deals": 0, "errors": []}
    seconds = time.perf_counter() - started
    calls = 5 * (-(-len(logins) // CHUNK))  # deals, order history, positions, orders, accounts per chunk
    return {**totals, "accounts": len(tracked), "active": len(active), "idle": len(idle), "idle_polled": len(due),
            "seconds": round(seconds, 3), "calls": calls,
            "ms_per_call": round(seconds * 1000 / calls, 1) if calls else None}


def full_sync(db: Session, gw: MT5Gateway | None = None, logins: list[int] | None = None,
              server_id: int | None = None) -> dict:
    """Sync one server, or every enabled server when server_id is None.

    `gw` overrides the gateway (tests); normally each server uses its own.
    """
    servers = [server_id] if server_id is not None else [s.id for s in enabled_servers(db)]
    totals = _empty_totals()
    for sid in servers:
        try:
            g = gw if gw is not None else get_gateway(sid)
        except MT5Error as exc:
            log.error("server %s unavailable for sync: %s", sid, exc)
            totals["errors"].append(f"server {sid}: {exc}")
            continue
        on_server = tracked_logins(db, sid)
        known = set(on_server)
        _sync_logins(db, g, on_server if logins is None else [x for x in logins if x in known], sid, totals)
    return totals


def _empty_totals() -> dict:
    return {"new_deals": 0, "open_positions": 0, "open_orders": 0, "accounts": 0, "errors": []}


def _sync_logins(db: Session, gw: MT5Gateway, scope: list[int], server_id: int, totals: dict) -> dict:
    """Deals, positions, orders and account summaries of these (tracked) logins, added to `totals`."""
    report(0, 4)  # progress of a background sync: four steps
    totals["new_deals"] += sync_deals(db, gw, scope, server_id)
    report(1, 4)
    totals["open_positions"] += sync_positions(db, gw, scope, server_id)
    report(2, 4)
    totals["open_orders"] += sync_orders(db, gw, scope, server_id)
    report(3, 4)
    totals["accounts"] += sync_accounts(db, gw, scope, server_id)
    report(4, 4)
    return totals
