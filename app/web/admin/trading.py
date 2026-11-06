from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select, text, tuple_
from sqlalchemy.orm import Session

from app.core import masks
from app.core.errors import ServiceError, get_or_404
from app.db import get_db, utcnow
from app.models import (
    ORDER_STATES, ORDER_TYPES, AccountGroup, Client, Deal, MT5Server, Order, Position, Staff, SyncJob, SyncState,
    TradingAccount,
)
from app.mt5 import peek
from app.services import servers as server_svc
from app.services import closed_positions, sync, sync_jobs, trade_cleanup, trader_styles
from app.services.clients import visible_rows
from app.web import syncs
from app.web.common import (
    Page, WebForbidden, _hub_items, attempt, flash, opt_int, redirect, render, sent_from, staff_perm, web_staff,
)

router = APIRouter()

TRADES = "/admin/trades"
SYNCED = {"positions": ("open positions", SyncState.last_position_sync_at),
          "orders": ("orders", SyncState.last_order_sync_at), "deals": ("deals", SyncState.last_deal_sync_at)}
EXACT_COUNT_BELOW = 100000  # tables estimated larger than this show PostgreSQL's estimate instead of count(*)
SYNC_ERRORS_SHOWN = 100


def table_rows(db: Session, model) -> int:
    """Rows in a table: PostgreSQL's planner estimate (pg_class.reltuples, kept fresh by autovacuum) for big
    tables, an exact count for small ones and on other databases."""
    if db.get_bind().dialect.name == "postgresql":
        estimate = db.scalar(text("SELECT reltuples::bigint FROM pg_class WHERE oid = to_regclass(:t)"),
                             {"t": model.__tablename__})
        if estimate is not None and estimate >= EXACT_COUNT_BELOW:
            return int(estimate)
    return db.scalar(select(func.count()).select_from(model)) or 0


def _servers(db: Session) -> dict:
    return {s.id: s.name for s in db.scalars(select(MT5Server).order_by(MT5Server.id))}


def _owners(db: Session, rows) -> dict:
    """(server id, login) -> (client id, name), for the rows of a list."""
    accounts = list({(r.server_id, r.login) for r in rows})
    if not accounts:
        return {}
    found = db.execute(select(TradingAccount.server_id, TradingAccount.login, Client.id, Client.first_name, Client.last_name)
                       .join(Client, Client.id == TradingAccount.client_id)
                       .where(tuple_(TradingAccount.server_id, TradingAccount.login).in_(accounts))).all()
    return {(server_id, login): (cid, f"{fn} {ln}") for server_id, login, cid, fn, ln in found}


def _filters(db: Session, query, model, login: str, symbol: str, group: str, style: str, comment: str = ""):
    """The shared filters of the positions, deals and orders lists. Login, symbol and comment accept * and ?
    wildcards."""
    query = masks.filter_comment(masks.filter_login_symbol(query, model, login, symbol), model, comment)
    if group.isdigit():
        query = query.where(tuple_(model.server_id, model.login).in_(
            select(TradingAccount.server_id, TradingAccount.login).where(TradingAccount.account_group_id == int(group))))
    if style:
        query = query.where(tuple_(model.server_id, model.login).in_(
            list(trader_styles.accounts_with_style(db, style)) or [(-1, -1)]))
    return query


def _filter_ctx(db: Session, **values) -> dict:
    """What the filter bars need: the choices and the current values."""
    return {"groups": db.execute(select(AccountGroup.id, AccountGroup.name).order_by(AccountGroup.name)).all(),
            "styles": trader_styles.filter_options(db), **values}


def _trade_page(what: str, pages=tuple(SYNCED)) -> None:
    if what not in pages:
        raise HTTPException(404)


@router.get("/trades/{what}")
def trade_page(what: str, me: Staff = Depends(web_staff)):
    """The Trades menu entries open the first tab this staff member may see."""
    _trade_page(what)
    tabs = _hub_items(what, me)
    if not tabs:
        raise WebForbidden()
    return redirect(tabs[0][0])


@router.get("/trades/positions/data")
def positions(request: Request, login: str = "", symbol: str = "", server: str = "", group: str = "", style: str = "",
              comment: str = "", page: int = 1, partial: int = 0, me: Staff = Depends(staff_perm("trading.view")),
              db: Session = Depends(get_db)):
    query = select(Position).where(Position.is_open.is_(True), visible_rows(me, Position))
    if server.isdigit():
        query = query.where(Position.server_id == int(server))
    query = _filters(db, query, Position, login, symbol, group, style, comment)
    pg = Page(db, query.order_by(Position.time_create.desc()), page, per_page=100)
    totals = db.execute(query.with_only_columns(func.coalesce(func.sum(Position.volume), 0),
                                                func.coalesce(func.sum(Position.profit), 0))).one()
    ctx = dict(page=pg, owners=_owners(db, pg.items), server=server, servers=_servers(db),
               total_lots=totals[0], total_profit=totals[1],
               **_filter_ctx(db, login=login, symbol=symbol, group=group, style=style, comment=comment))
    # htmx polls ?partial=1 to refresh only the table
    return render(request, "admin/trading/positions_table.html" if partial else "admin/trading/positions.html", **ctx)


def _day(value: str, end: bool = False) -> datetime | None:
    try:
        day = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    return day.replace(hour=23, minute=59, second=59) if day and end else day


@router.get("/trades/positions/closed")
def closed(request: Request, login: str = "", symbol: str = "", server: str = "", group: str = "", style: str = "",
           comment: str = "", date_from: str = "", date_to: str = "", page: int = 1,
           me: Staff = Depends(staff_perm("trading.view")), db: Session = Depends(get_db)):
    """Positions that are closed, rebuilt from the deals that opened and closed them."""
    def narrow(query, rows):
        return _filters(db, query.where(visible_rows(me, rows)), rows, login, symbol, group, style, comment)

    query = closed_positions.query(narrow, int(server) if server.isdigit() else None, None, _day(date_from),
                                   _day(date_to, end=True))
    pg = Page(db, query, page, per_page=100, rows=True)
    return render(request, "admin/trading/closed_positions.html", page=pg, owners=_owners(db, pg.items),
                  totals=closed_positions.totals(db, query), date_from=date_from, date_to=date_to, server=server,
                  servers=_servers(db), **_filter_ctx(db, login=login, symbol=symbol, group=group, style=style, comment=comment))


@router.get("/trades/deals/data")
def deals(request: Request, login: str = "", symbol: str = "", kind: str = "", date_from: str = "", date_to: str = "",
          server: str = "", group: str = "", style: str = "", comment: str = "", position: str = "", page: int = 1,
          me: Staff = Depends(staff_perm("trading.view")), db: Session = Depends(get_db)):
    query = select(Deal).where(visible_rows(me, Deal))
    if server.isdigit():
        query = query.where(Deal.server_id == int(server))
    if position.isdigit():
        query = query.where(Deal.position_id == int(position))
    query = _filters(db, query, Deal, login, symbol, group, style, comment)
    if kind == "trades":
        query = query.where(Deal.action.in_((0, 1)))
    elif kind == "balance":
        query = query.where(Deal.action.in_((2, 3)))
    start, stop = _day(date_from), _day(date_to, end=True)
    if start is not None:
        query = query.where(Deal.time >= start)
    if stop is not None:
        query = query.where(Deal.time <= stop)
    pg = Page(db, query.order_by(Deal.time.desc(), Deal.ticket.desc()), page, per_page=100)
    return render(request, "admin/trading/deals.html", page=pg, owners=_owners(db, pg.items),
                  kind=kind, date_from=date_from, date_to=date_to, position=position, server=server, servers=_servers(db),
                  **_filter_ctx(db, login=login, symbol=symbol, group=group, style=style, comment=comment))


@router.get("/trades/orders/data")
def orders(request: Request, login: str = "", symbol: str = "", status: str = "open", server: str = "",
           group: str = "", style: str = "", comment: str = "", page: int = 1,
           me: Staff = Depends(staff_perm("trading.view")),
           db: Session = Depends(get_db)):
    query = select(Order).where(visible_rows(me, Order), Order.is_open.is_(status != "history"))
    if server.isdigit():
        query = query.where(Order.server_id == int(server))
    query = _filters(db, query, Order, login, symbol, group, style, comment)
    pg = Page(db, query.order_by(Order.time_setup.desc().nulls_last(), Order.ticket.desc()), page, per_page=100)
    return render(request, "admin/trading/orders.html", page=pg, owners=_owners(db, pg.items),
                  status=status, types=ORDER_TYPES, states=ORDER_STATES, server=server, servers=_servers(db),
                  **_filter_ctx(db, login=login, symbol=symbol, group=group, style=style, comment=comment))


@router.get("/trades/{what}/controls")
def cleanup(request: Request, what: str, status: str = "", login: str = "", symbol: str = "",
            comment: str = "", date_from: str = "", date_to: str = "", server: str = "", page: int = 1,
            me: Staff = Depends(staff_perm("mt5_trades.delete")), db: Session = Depends(get_db)):
    _trade_page(what, ("orders", "deals"))
    filters = dict(status=status, login=login, symbol=symbol, comment=comment, date_from=date_from, date_to=date_to)
    chosen = syncs.server_id(db, server) or 0
    query = trade_cleanup.orders_query(chosen, me, filters) if what == "orders" \
        else trade_cleanup.deals_query(chosen, me, filters)
    model = Order if what == "orders" else Deal
    newest = Order.time_setup.desc().nulls_last() if what == "orders" else Deal.time.desc()
    pg = Page(db, query.order_by(newest, model.ticket.desc()), page, per_page=100)
    opened = trade_cleanup.open_positions(db, chosen, pg.items)
    reasons = {r.ticket: trade_cleanup.kept_reason(what, r, opened) for r in pg.items}
    kept = trade_cleanup.count_kept(db, what, chosen, me, filters)
    return render(request, "admin/trading/cleanup.html", page=pg, what=what, filters=filters, server=chosen,
                  servers=_servers(db),
                  reasons=reasons, kept=kept, deletable=pg.total - kept, filtered=any(filters.values()),
                  statuses=trade_cleanup.ORDER_STATUSES if what == "orders" else trade_cleanup.DEAL_STATUSES,
                  run_fields={"everything": "1", "server_id": chosen, **filters}, types=ORDER_TYPES, states=ORDER_STATES)


@router.post("/trades/{what}/controls/run")
def cleanup_run(request: Request, what: str, ticket: list[str] = Form([]), everything: str = Form(""),
                status: str = Form(""), login: str = Form(""), symbol: str = Form(""), date_from: str = Form(""),
                date_to: str = Form(""), comment: str = Form(""), server_id: str = Form(""),
                me: Staff = Depends(staff_perm("mt5_trades.delete")),
                db: Session = Depends(get_db)):
    _trade_page(what, ("orders", "deals"))
    chosen = syncs.server_id(db, server_id)
    back = f"{TRADES}/{what}/controls?server={chosen or ''}"
    tickets = None if everything else [int(t) for t in ticket if t.strip().isdigit()]
    if tickets == []:
        flash(request, "Tick the rows first, or use the button for everything that matches.", "warning")
        return redirect(back)
    filters = dict(status=status, login=login, symbol=symbol, comment=comment, date_from=date_from, date_to=date_to)
    return syncs.start(request, db, me, "trade_cleanup", chosen, back,
                       {"what": what, "tickets": tickets, "filters": filters if everything else None})


def _sync_tab(request: Request, what: str = "deals") -> str:
    """The sync page a form was sent from: it opens under Trades and under Imports."""
    return sent_from(request, f"{TRADES}/{what}/sync")


@router.get("/import/trades")
def old_sync_page():
    return redirect("/admin/import/deals")


@router.get("/trades/{what}/sync")
@router.get("/import/{what}")
def sync_page(request: Request, what: str, server: str = "", me: Staff = Depends(staff_perm("trading.view")),
              db: Session = Depends(get_db)):
    _trade_page(what)
    noun, synced_at = SYNCED[what]
    chosen = int(server) if server.isdigit() else None
    states = [SyncState.server_id == chosen] if chosen else []
    errors = db.scalars(select(SyncState).where(SyncState.last_error.is_not(None), *states)
                        .order_by(SyncState.last_deal_sync_at.desc().nulls_last(), SyncState.login)
                        .limit(SYNC_ERRORS_SHOWN)).all()
    stats = db.execute(select(func.count(), func.max(synced_at)).select_from(SyncState).where(*states)).one()
    if what == "positions":
        rows = [Position.is_open.is_(True)] + ([Position.server_id == chosen] if chosen else [])
        stored = db.scalar(select(func.count()).select_from(Position).where(*rows)) or 0
    else:
        model = Order if what == "orders" else Deal
        stored = (db.scalar(select(func.count()).select_from(model).where(model.server_id == chosen)) or 0
                  if chosen else table_rows(db, model))
    servers = db.scalars(select(MT5Server).order_by(MT5Server.id)).all()
    connected = {s.id: bool(peek(s.id) and peek(s.id).connected) for s in servers}
    return render(request, "admin/trading/sync.html", what=what, noun=noun, errors=errors, tracked=stats[0],
                  last_sync=stats[1], stored=stored, servers=servers, connected=connected, server=server,
                  usage=server_svc.usage(db), jobs=_jobs(db), today=utcnow().date())


@router.get("/trades/{what}/history")
def history_page(request: Request, what: str, kind: str = "", trigger: str = "", status: str = "", partial: int = 0,
                 page: int = 1, me: Staff = Depends(staff_perm("trading.view")), db: Session = Depends(get_db)):
    """Its own syncs and the full sync, which covers it too."""
    _trade_page(what)
    return syncs.history_page(request, db, f"{SYNCED[what][0].capitalize()} sync history", (what, "trades", "catch_up"), kind,
                              trigger, status, partial, page)


@router.post("/trades/{what}/sync")
def run_sync(request: Request, what: str, server_id: str = Form(""), me: Staff = Depends(staff_perm("trading.sync")),
             db: Session = Depends(get_db)):
    _trade_page(what)
    return syncs.start(request, db, me, what, opt_int(server_id), _sync_tab(request, what))


@router.post("/trades/sync/all")
def run_full_sync(request: Request, server_id: str = Form(""), me: Staff = Depends(staff_perm("trading.sync")),
                  db: Session = Depends(get_db)):
    return syncs.start(request, db, me, "trades", opt_int(server_id), _sync_tab(request))


def _jobs(db: Session):
    return db.scalars(select(SyncJob).order_by(SyncJob.id.desc()).limit(15)).all()


@router.get("/trades/sync/jobs")
def sync_jobs_partial(request: Request, me: Staff = Depends(staff_perm("trading.view")), db: Session = Depends(get_db)):
    """The task list alone; the page polls it every 2 s while a task is queued or running."""
    servers = {s.id: s.name for s in db.scalars(select(MT5Server))}
    return render(request, "admin/trading/sync_jobs.html", jobs=_jobs(db), server_names=servers)


@router.post("/trades/sync/jobs")
def sync_job_create(request: Request, server_id: str = Form(""), logins: str = Form(""), date_from: str = Form(...),
                    date_to: str = Form(""), include_orders: str = Form(""),
                    me: Staff = Depends(staff_perm("trading.sync")), db: Session = Depends(get_db)):
    def run():
        try:
            start = datetime.fromisoformat(date_from)
            end = datetime.fromisoformat(date_to) + timedelta(days=1) if date_to else utcnow() + sync.CLOCK_MARGIN
        except ValueError:
            raise ServiceError("Enter the period as dates")
        wanted = [int(x) for x in logins.replace(",", " ").split() if x.isdigit()]
        if logins.strip() and not wanted:
            raise ServiceError("Accounts must be MT5 logins separated by commas or spaces")
        return sync_jobs.create_job(db, me, server_id=opt_int(server_id), logins=wanted, date_from=start,
                                    date_to=end, include_orders=include_orders == "on").id
    return attempt(request, run, "Sync task #{result} queued; progress is shown below.", _sync_tab(request))


@router.post("/trades/sync/jobs/{job_id}/cancel")
def sync_job_cancel(request: Request, job_id: int, me: Staff = Depends(staff_perm("trading.sync")),
                    db: Session = Depends(get_db)):
    job = get_or_404(db, SyncJob, job_id)
    return attempt(request, lambda: sync_jobs.cancel_job(db, me, job), f"Sync task #{job_id} cancelled.", _sync_tab(request))
