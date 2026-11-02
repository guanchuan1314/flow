"""Background "sync missing history" tasks.

Staff queue a job (server, accounts, period) in the admin; the worker's job thread runs it. For every
batch of accounts and every slice of the period it re-reads deals and orders from MT5 and stores the
ones the CRM is missing (inserting is idempotent), then refreshes open positions, open orders and
balances. Progress is saved after each step, so the admin page shows it live. Jobs can be cancelled;
a job interrupted by a restart starts again from the beginning.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import ServiceError
from app.db import utcnow
from app.models import MT5Server, Staff, SyncJob, TradingAccount
from app.mt5 import MT5Error, get_gateway
from app.services import sync
from app.web.labels import label

log = logging.getLogger(__name__)
BATCH = 50  # accounts per MT5 request
SLICE = timedelta(days=31)  # period per MT5 request
STALE = timedelta(minutes=2)  # a running job without a heartbeat for this long was interrupted
EARLIEST = datetime(2000, 1, 1)


def create_job(db: Session, actor, *, server_id: int | None, logins: list[int], date_from: datetime,
               date_to: datetime, include_orders: bool = True) -> SyncJob:
    if date_from < EARLIEST:
        date_from = EARLIEST
    if date_to <= date_from:
        raise ServiceError("The end of the period must be after its start")
    if server_id is not None and db.get(MT5Server, server_id) is None:
        raise ServiceError("Unknown MT5 server")
    if logins:
        known = set(db.scalars(select(TradingAccount.login).where(TradingAccount.login.in_(logins))))
        unknown = sorted(set(logins) - known)
        if unknown:
            raise ServiceError(f"Not CRM accounts: {', '.join(map(str, unknown[:10]))}. Import them first (Sync from MT5).")
    job = SyncJob(created_at=utcnow(), created_by_id=actor.id if isinstance(actor, Staff) else None,
                  server_id=server_id, logins=sorted(set(logins)), date_from=date_from, date_to=date_to,
                  include_orders=include_orders, status="queued", total=0, done=0, deals_seen=0, deals_added=0,
                  orders_seen=0, orders_added=0, errors=0, message="Waiting for the worker", cancel_requested=False)
    db.add(job)
    db.flush()
    audit(db, actor, "sync_job.create", "sync_job", job.id,
          {"server_id": server_id, "logins": len(job.logins), "from": date_from, "to": date_to})
    db.commit()
    return job


def cancel_job(db: Session, actor, job: SyncJob) -> SyncJob:
    if not job.active:
        raise ServiceError(f"The task is already {label(job.status).lower()}")
    if job.status == "queued":
        job.status, job.finished_at, job.message = "cancelled", utcnow(), "Cancelled before it started"
    else:
        job.cancel_requested, job.message = True, "Cancelling…"
    audit(db, actor, "sync_job.cancel", "sync_job", job.id, {})
    db.commit()
    return job


def _plan(db: Session, job: SyncJob) -> list[tuple[int, list[int]]]:
    """(server id, batch of logins) to process."""
    if job.logins:
        rows = db.execute(select(TradingAccount.server_id, TradingAccount.login)
                          .where(TradingAccount.login.in_(job.logins))).all()
        per_server: dict[int, list[int]] = {}
        for sid, login in rows:
            if job.server_id is None or sid == job.server_id:
                per_server.setdefault(sid, []).append(login)
    else:
        sids = [job.server_id] if job.server_id else [s.id for s in sync.enabled_servers(db)]
        per_server = {sid: sync.tracked_logins(db, sid) for sid in sids}
    return [(sid, logins[i:i + BATCH]) for sid, logins in sorted(per_server.items())
            for i in range(0, len(logins), BATCH)]


def _slices(job: SyncJob) -> list[tuple[datetime, datetime]]:
    out, start = [], job.date_from
    while start < job.date_to:
        end = min(start + SLICE, job.date_to)
        out.append((start, end))
        start = end
    return out


def recover_interrupted(db: Session) -> int:
    """Requeue running jobs whose worker stopped (restart, crash)."""
    count = 0
    for job in db.scalars(select(SyncJob).where(SyncJob.status == "running",
                                                SyncJob.heartbeat_at < utcnow() - STALE)):
        job.status, job.message, job.done = "queued", "Interrupted; will start again", 0
        count += 1
    db.commit()
    return count


def run_next(db: Session) -> SyncJob | None:
    """Claim and run the oldest queued job. Returns it, or None when there is nothing to do."""
    job = db.scalar(select(SyncJob).where(SyncJob.status == "queued").order_by(SyncJob.id)
                    .with_for_update(skip_locked=True).limit(1))
    if job is None:
        db.rollback()
        return None
    job.status, job.started_at, job.heartbeat_at, job.message = "running", utcnow(), utcnow(), "Starting"
    job.done = job.deals_seen = job.deals_added = job.orders_seen = job.orders_added = job.errors = 0
    db.commit()
    try:
        _execute(db, job)
    except Exception as exc:  # noqa: BLE001 - the job records the failure; the thread keeps running
        log.exception("sync job %s failed", job.id)
        db.rollback()
        job.status, job.finished_at, job.message = "failed", utcnow(), f"Failed: {exc}"[:512]
        db.commit()
    return job


def _execute(db: Session, job: SyncJob) -> None:
    batches, slices = _plan(db, job), _slices(job)
    job.total = len(batches) * len(slices) + len(batches)  # + one refresh of open positions/orders per batch
    db.commit()
    for sid, logins in batches:
        try:
            gw = get_gateway(sid)
        except MT5Error as exc:
            job.errors += len(slices) + 1
            job.done += len(slices) + 1
            job.message = f"Server {sid} unavailable: {exc}"[:512]
            db.commit()
            continue
        for start, end in slices:
            db.refresh(job)
            if job.cancel_requested:
                job.status, job.finished_at = "cancelled", utcnow()
                job.message = f"Cancelled after {job.done} of {job.total} steps"
                db.commit()
                return
            try:
                deals = gw.get_deals(logins, start, end)
                job.deals_seen += len(deals)
                job.deals_added += len(sync.ingest_deals(db, deals, server_id=sid))
                if job.include_orders:
                    orders = gw.get_order_history(logins, start, end)
                    job.orders_seen += len(orders)
                    job.orders_added += sync.ingest_orders(db, orders, sid, False)
            except MT5Error as exc:
                job.errors += 1
                job.message = f"{start:%Y-%m-%d}–{end:%Y-%m-%d}, {len(logins)} accounts: {exc}"[:512]
            else:
                job.message = f"Server {sid}: {start:%Y-%m-%d} to {end:%Y-%m-%d}, {len(logins)} accounts"
            job.done += 1
            job.heartbeat_at = utcnow()
            db.commit()
        # current state: open positions, open orders and balances
        sync.sync_positions(db, gw, logins, sid)
        if job.include_orders:
            sync.sync_orders(db, gw, logins, sid)
        sync.sync_accounts(db, gw, logins, sid)
        job.done += 1
        job.heartbeat_at = utcnow()
        db.commit()
    job.status, job.finished_at = "done", utcnow()
    job.message = (f"Finished: {job.deals_added} missing deal(s) added of {job.deals_seen} read"
                   + (f", {job.orders_added} order(s) added of {job.orders_seen}" if job.include_orders else "")
                   + (f"; {job.errors} step(s) failed" if job.errors else ""))
    db.commit()
    log.info("sync job %s: %s", job.id, job.message)
