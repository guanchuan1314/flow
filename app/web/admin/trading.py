from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import String, cast, false, func, select, text
from sqlalchemy.orm import Session

from app.core.errors import ServiceError, get_or_404
from app.db import get_db, utcnow
from app.models import (
    ORDER_STATES, ORDER_TYPES, AccountGroup, Client, Deal, MT5Server, Order, Position, Staff, SyncJob, SyncState,
    TradingAccount,
)
from app.mt5 import peek
from app.services import servers as server_svc
from app.services import sync, sync_jobs, trader_styles
from app.services.clients import visible_logins
from app.web import syncs
from app.web.common import Page, attempt, opt_int, render, staff_perm

router = APIRouter()

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


def _owners(db: Session, logins) -> dict:
    rows = db.execute(select(TradingAccount.login, Client.id, Client.first_name, Client.last_name)
                      .join(Client, Client.id == TradingAccount.client_id)
                      .where(TradingAccount.login.in_(set(logins)))).all()
    return {login: (cid, f"{fn} {ln}") for login, cid, fn, ln in rows}


def wildcard(pattern: str) -> str | None:
    """A LIKE pattern for `*` (any text) and `?` (one character) searches, e.g. *EUR* contains EUR, EUR* starts
    with it, *EUR ends with it; None when the text has no wildcard (then it's an exact match)."""
    if "*" not in pattern and "?" not in pattern:
        return None
    escaped = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped.replace("*", "%").replace("?", "_")


def _filters(db: Session, query, model, login: str, symbol: str, group: str, style: str):
    """The shared filters of the positions, deals and orders lists. Login and symbol accept * and ? wildcards."""
    login, symbol = login.strip(), symbol.strip().upper()
    if login:
        like = wildcard(login)
        if like is not None:
            query = query.where(cast(model.login, String).like(like, escape="\\"))
        elif login.isdigit():
            query = query.where(model.login == int(login))
        else:
            query = query.where(false())  # not a login and no wildcard: nothing matches
    if symbol:
        like = wildcard(symbol)
        query = query.where(func.upper(model.symbol).like(like, escape="\\") if like is not None
                            else model.symbol == symbol)
    if group.isdigit():
        query = query.where(model.login.in_(select(TradingAccount.login)
                                            .where(TradingAccount.account_group_id == int(group))))
    if style:
        query = query.where(model.login.in_(trader_styles.logins_with_style(db, style) or {-1}))
    return query


def _filter_ctx(db: Session, **values) -> dict:
    """What the filter bars need: the choices and the current values."""
    return {"groups": db.execute(select(AccountGroup.id, AccountGroup.name).order_by(AccountGroup.name)).all(),
            "styles": trader_styles.filter_options(db), **values}


@router.get("/positions")
def positions(request: Request, login: str = "", symbol: str = "", server: str = "", group: str = "", style: str = "",
              page: int = 1, partial: int = 0, me: Staff = Depends(staff_perm("trading.view")),
              db: Session = Depends(get_db)):
    query = select(Position).where(Position.is_open.is_(True), Position.login.in_(visible_logins(me)))
    if server.isdigit():
        query = query.where(Position.server_id == int(server))
    query = _filters(db, query, Position, login, symbol, group, style)
    pg = Page(db, query.order_by(Position.time_create.desc()), page, per_page=100)
    totals = db.execute(query.with_only_columns(func.coalesce(func.sum(Position.volume), 0),
                                                func.coalesce(func.sum(Position.profit), 0))).one()
    ctx = dict(page=pg, owners=_owners(db, [p.login for p in pg.items]), server=server, servers=_servers(db),
               total_lots=totals[0], total_profit=totals[1],
               **_filter_ctx(db, login=login, symbol=symbol, group=group, style=style))
    # htmx polls ?partial=1 to refresh only the table
    return render(request, "admin/trading/positions_table.html" if partial else "admin/trading/positions.html", **ctx)


@router.get("/deals")
def deals(request: Request, login: str = "", symbol: str = "", kind: str = "", date_from: str = "", date_to: str = "",
          server: str = "", group: str = "", style: str = "", page: int = 1,
          me: Staff = Depends(staff_perm("trading.view")), db: Session = Depends(get_db)):
    query = select(Deal).where(Deal.login.in_(visible_logins(me)))
    if server.isdigit():
        query = query.where(Deal.server_id == int(server))
    query = _filters(db, query, Deal, login, symbol, group, style)
    if kind == "trades":
        query = query.where(Deal.action.in_((0, 1)))
    elif kind == "balance":
        query = query.where(Deal.action.in_((2, 3)))
    for value, op in ((date_from, ">="), (date_to, "<=")):
        try:
            d = datetime.fromisoformat(value) if value else None
        except ValueError:
            d = None
        if d is not None:
            query = query.where(Deal.time >= d if op == ">=" else Deal.time <= d.replace(hour=23, minute=59, second=59))
    pg = Page(db, query.order_by(Deal.time.desc(), Deal.ticket.desc()), page, per_page=100)
    return render(request, "admin/trading/deals.html", page=pg, owners=_owners(db, [d.login for d in pg.items]),
                  kind=kind, date_from=date_from, date_to=date_to, server=server, servers=_servers(db),
                  **_filter_ctx(db, login=login, symbol=symbol, group=group, style=style))


@router.get("/orders")
def orders(request: Request, login: str = "", symbol: str = "", status: str = "open", server: str = "",
           group: str = "", style: str = "", page: int = 1, me: Staff = Depends(staff_perm("trading.view")),
           db: Session = Depends(get_db)):
    query = select(Order).where(Order.login.in_(visible_logins(me)), Order.is_open.is_(status != "history"))
    if server.isdigit():
        query = query.where(Order.server_id == int(server))
    query = _filters(db, query, Order, login, symbol, group, style)
    pg = Page(db, query.order_by(Order.time_setup.desc().nulls_last(), Order.ticket.desc()), page, per_page=100)
    return render(request, "admin/trading/orders.html", page=pg, owners=_owners(db, [o.login for o in pg.items]),
                  status=status, types=ORDER_TYPES, states=ORDER_STATES, server=server, servers=_servers(db),
                  **_filter_ctx(db, login=login, symbol=symbol, group=group, style=style))


@router.get("/import/trades")
def sync_page(request: Request, me: Staff = Depends(staff_perm("trading.view")), db: Session = Depends(get_db)):
    errors = db.scalars(select(SyncState).where(SyncState.last_error.is_not(None))
                        .order_by(SyncState.last_deal_sync_at.desc().nulls_last(), SyncState.login)
                        .limit(SYNC_ERRORS_SHOWN)).all()
    stats = db.execute(select(func.count(), func.max(SyncState.last_deal_sync_at), func.max(SyncState.last_position_sync_at))
                       .select_from(SyncState)).one()
    deal_count = table_rows(db, Deal)
    servers = db.scalars(select(MT5Server).order_by(MT5Server.id)).all()
    connected = {s.id: bool(peek(s.id) and peek(s.id).connected) for s in servers}
    return render(request, "admin/trading/sync.html", errors=errors, tracked=stats[0], last_deals=stats[1],
                  last_positions=stats[2], deal_count=deal_count, servers=servers, connected=connected,
                  usage=server_svc.usage(db), jobs=_jobs(db), server_names={s.id: s.name for s in servers},
                  order_count=table_rows(db, Order),
                  last_orders=db.scalar(select(func.max(SyncState.last_order_sync_at))),
                  today=utcnow().date())


@router.post("/import/trades")
def run_sync(request: Request, me: Staff = Depends(staff_perm("trading.sync")), db: Session = Depends(get_db)):
    return syncs.start(request, db, me, "trades", None, "/admin/import/trades")


def _jobs(db: Session):
    return db.scalars(select(SyncJob).order_by(SyncJob.id.desc()).limit(15)).all()


@router.get("/import/trades/jobs")
def sync_jobs_partial(request: Request, me: Staff = Depends(staff_perm("trading.view")), db: Session = Depends(get_db)):
    """The task list alone; the page polls it every 2 s while a task is queued or running."""
    servers = {s.id: s.name for s in db.scalars(select(MT5Server))}
    return render(request, "admin/trading/sync_jobs.html", jobs=_jobs(db), server_names=servers)


@router.post("/import/trades/jobs")
def sync_job_create(request: Request, server_id: str = Form(""), logins: str = Form(""), date_from: str = Form(...),
                    date_to: str = Form(""), include_orders: str = Form(""),
                    me: Staff = Depends(staff_perm("trading.sync")), db: Session = Depends(get_db)):
    def run():
        try:
            start = datetime.fromisoformat(date_from)
            end = datetime.fromisoformat(date_to) + timedelta(days=1) if date_to else utcnow() + timedelta(minutes=1)
        except ValueError:
            raise ServiceError("Enter the period as dates")
        wanted = [int(x) for x in logins.replace(",", " ").split() if x.isdigit()]
        if logins.strip() and not wanted:
            raise ServiceError("Accounts must be MT5 logins separated by commas or spaces")
        return sync_jobs.create_job(db, me, server_id=opt_int(server_id), logins=wanted, date_from=start,
                                    date_to=end, include_orders=include_orders == "on").id
    return attempt(request, run, "Sync task #{result} queued; progress is shown below.", "/admin/import/trades")


@router.post("/import/trades/jobs/{job_id}/cancel")
def sync_job_cancel(request: Request, job_id: int, me: Staff = Depends(staff_perm("trading.sync")),
                    db: Session = Depends(get_db)):
    job = get_or_404(db, SyncJob, job_id)
    return attempt(request, lambda: sync_jobs.cancel_job(db, me, job), f"Sync task #{job_id} cancelled.", "/admin/import/trades")
