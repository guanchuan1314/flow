"""Synchronization from MT5 in the background, with a history.

A staff member starting a sync (import account groups or trading accounts, sync trades, symbols, liquidity
providers, routing, the inactive demo account clean-up, ...) gets a SyncRun: it runs on a thread pool, so the page
answers at once and several syncs run side by side. Syncs over every server run the servers in parallel, each in
its own database session. Scheduled syncs in the worker (the MT5 import, the symbol auto sync, the daily demo
clean-up) are recorded the same way (record()), so the history shows who or what started each one.

Progress: the work a sync does calls report(done, total) as it goes; it is saved on the run (at most twice a
second), so a page can show a progress bar that survives a refresh. Outside a run, report() does nothing.
"""

import json
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import ServiceError
from app.db import SessionLocal, utcnow
from app.models import MT5Server, Staff, SyncRun
from app.mt5 import MT5Error

log = logging.getLogger("sync")

KINDS = {
    "account_groups": "Account groups", "trading_accounts": "Trading accounts", "trades": "Trades",
    "trading_setup": "Trading setup", "symbols": "Symbols", "liquidity": "Liquidity providers",
    "routing": "Routing", "pricing": "Symbols and pricing", "demo_cleanup": "Inactive demo accounts",
}
PER_SERVER = {"trading_setup", "symbols", "liquidity", "routing", "pricing", "demo_cleanup"}  # need a server

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def pool() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=max(1, get_settings().sync_threads), thread_name_prefix="sync")
        return _pool


# ---------------------------------------------------------------- progress
_local = threading.local()  # .key = (run id, server id) of the run this thread works on
_parts: dict[int, dict[int, tuple[int, int]]] = {}  # run id -> server id -> (done, total)
_saved: dict[int, float] = {}  # run id -> when its progress was last saved
_progress_lock = threading.Lock()
SAVE_EVERY = 0.5  # seconds


def report(done: int, total: int) -> None:
    """Progress of the sync running in this thread: `done` of `total` work items (servers add up)."""
    key = getattr(_local, "key", None)
    if key is None:
        return
    run_id, sid = key
    with _progress_lock:
        parts = _parts.setdefault(run_id, {})
        parts[sid] = (min(done, total), total)
        now = time.monotonic()
        if done < total and now - _saved.get(run_id, 0.0) < SAVE_EVERY:
            return
        _saved[run_id] = now
        d, t = sum(x[0] for x in parts.values()), sum(x[1] for x in parts.values())
    with SessionLocal() as db:  # its own session: the sync's own transaction is left alone
        db.execute(update(SyncRun).where(SyncRun.id == run_id).values(done=d, total=t))
        db.commit()


def _working_on(run_id: int | None, sid: int | None = None) -> None:
    _local.key = (run_id, sid) if run_id is not None else None


def _forget(run_id: int) -> None:
    with _progress_lock:
        _parts.pop(run_id, None)
        _saved.pop(run_id, None)


# ---------------------------------------------------------------- what each kind does
def _servers(db: Session, server_id: int | None) -> list[MT5Server]:
    q = select(MT5Server).where(MT5Server.enabled.is_(True)).order_by(MT5Server.id)
    if server_id is not None:
        q = q.where(MT5Server.id == server_id)
    return list(db.scalars(q))


def _each_server(run_id: int, server_ids: list[int], actor_id: int | None, fn: Callable, params: dict) -> dict:
    """fn(db, server, actor, **params) for every server at once, each in its own thread and session:
    {server name: result | "failed: why"}."""
    def one(sid: int):
        _working_on(run_id, sid)
        with SessionLocal() as db:
            server = db.get(MT5Server, sid)
            try:
                return server.name, fn(db, server, db.get(Staff, actor_id) if actor_id else None, **params)
            except (ServiceError, MT5Error) as exc:
                db.rollback()
                return server.name, f"failed: {getattr(exc, 'message', None) or exc}"
            finally:
                _working_on(None)
    if len(server_ids) == 1:
        return dict([one(server_ids[0])])
    with ThreadPoolExecutor(max_workers=min(8, len(server_ids)), thread_name_prefix="sync-server") as ex:
        return dict(ex.map(one, server_ids))


def _trades(db: Session, server: MT5Server, actor) -> dict:
    from app.services import sync
    return sync.full_sync(db, server_id=server.id)


def _pricing(db: Session, server: MT5Server, actor) -> dict:
    from app.services import pricing, symbols
    report(0, 2)
    synced = symbols.sync_symbols(db, server, actor)
    report(1, 2)
    return {"symbols": synced, "pricing": pricing.apply(db, actor, server)}


def _demo_cleanup(db: Session, server: MT5Server, actor, logins: list[int] | None = None,
                  action: str | None = None) -> dict:
    from app.services import demo_cleanup
    return demo_cleanup.run(db, actor, server, logins=logins, action=action)


def _runner(kind: str) -> Callable:
    from app.services import liquidity, mt5_setup, mt5_sync, routing_rules, symbols
    return {
        "account_groups": mt5_sync.sync_account_groups, "trading_accounts": mt5_sync.sync_trading_accounts,
        "trades": _trades, "trading_setup": mt5_setup.sync_all, "symbols": symbols.sync_symbols,
        "liquidity": liquidity.sync_liquidity, "routing": routing_rules.sync_routes, "pricing": _pricing,
        "demo_cleanup": _demo_cleanup,
    }[kind]


def _jsonable(value):
    return json.loads(json.dumps(value, default=str))


def _failed(summary: dict) -> bool:
    return bool(summary) and all(isinstance(v, str) and v.startswith("failed:") for v in summary.values())


# ---------------------------------------------------------------- running
def _execute(run_id: int) -> None:
    with SessionLocal() as db:
        run = db.get(SyncRun, run_id)
        if run is None or run.status != "queued":
            return
        run.status, run.started_at = "running", utcnow()
        db.commit()
        kind, server_id, actor_id, params = run.kind, run.server_id, run.created_by_id, run.params or {}
        server_ids = [s.id for s in _servers(db, server_id)]
    try:
        if not server_ids:
            raise ServiceError("No enabled MT5 server" if server_id is None else "The MT5 server is disabled or gone")
        summary = _jsonable(_each_server(run_id, server_ids, actor_id, _runner(kind), params))
        status, message = ("failed" if _failed(summary) else "done"), ""
    except (ServiceError, MT5Error) as exc:
        summary, status, message = None, "failed", getattr(exc, "message", None) or str(exc)
    except Exception as exc:  # noqa: BLE001 - recorded on the run, not lost in a thread
        log.exception("sync run %s (%s) failed", run_id, kind)
        summary, status, message = None, "failed", f"Unexpected error: {exc}"
    _forget(run_id)
    with SessionLocal() as db:
        db.execute(update(SyncRun).where(SyncRun.id == run_id).values(
            status=status, finished_at=utcnow(), summary=summary, message=message[:1024]))
        db.commit()


def start(db: Session, actor: Staff | None, kind: str, server_id: int | None = None,
          params: dict | None = None) -> tuple[SyncRun, Future | None]:
    """Queue a sync and start it on the pool. The same sync already queued or running is returned instead
    (with no future)."""
    if kind not in KINDS:
        raise ServiceError("Unknown synchronization")
    if kind in PER_SERVER and server_id is None:
        raise ServiceError("No enabled MT5 server")
    busy = db.scalar(select(SyncRun).where(SyncRun.kind == kind, SyncRun.status.in_(("queued", "running")),
                                           SyncRun.server_id.is_(None) if server_id is None
                                           else SyncRun.server_id == server_id).limit(1))
    if busy is not None:
        return busy, None
    run = SyncRun(kind=kind, server_id=server_id, trigger="manual", created_by_id=actor.id if actor else None,
                  status="queued", created_at=utcnow(), message="", params=params or None)
    db.add(run)
    db.commit()
    return run, pool().submit(_execute, run.id)


def record(db: Session, kind: str, server: MT5Server, fn: Callable[[], dict]) -> SyncRun:
    """Run a scheduled sync here (the worker's thread) and keep it in the history; errors are re-raised."""
    run = SyncRun(kind=kind, server_id=server.id, trigger="schedule", status="running", created_at=utcnow(),
                  started_at=utcnow(), message="")
    db.add(run)
    db.commit()
    run_id, name = run.id, server.name
    _working_on(run_id, server.id)
    try:
        result = fn()
    except Exception as exc:
        db.rollback()
        db.execute(update(SyncRun).where(SyncRun.id == run_id).values(
            status="failed", finished_at=utcnow(), message=(getattr(exc, "message", None) or str(exc))[:1024]))
        db.commit()
        raise
    finally:
        _working_on(None)
        _forget(run_id)
    db.execute(update(SyncRun).where(SyncRun.id == run_id).values(
        status="done", finished_at=utcnow(), summary=_jsonable({name: result})))
    db.commit()
    return db.get(SyncRun, run_id)


def latest(db: Session, kinds: list[str], within_minutes: int = 60) -> list[SyncRun]:
    """For a page's progress panel: the runs of these kinds that are waiting or running, else the last one
    if it finished in the last hour (so its result is still shown after a refresh)."""
    from datetime import timedelta

    active = list(db.scalars(select(SyncRun).where(SyncRun.kind.in_(kinds), SyncRun.status.in_(("queued", "running")))
                             .order_by(SyncRun.id.desc()).limit(5)))
    if active:
        return active
    last = db.scalar(select(SyncRun).where(SyncRun.kind.in_(kinds),
                                           SyncRun.created_at > utcnow() - timedelta(minutes=within_minutes))
                     .order_by(SyncRun.id.desc()).limit(1))
    return [last] if last else []


def recover_interrupted(db: Session, trigger: str) -> int:
    """Runs left queued or running when their process stopped (manual: the API; schedule: the worker)."""
    n = db.execute(update(SyncRun).where(SyncRun.trigger == trigger, SyncRun.status.in_(("queued", "running")))
                   .values(status="failed", finished_at=utcnow(), message="Interrupted: the CRM restarted")).rowcount
    db.commit()
    return n
