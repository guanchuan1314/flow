"""Background worker threads for the CRM's MT5 server.

  * deal-push  : DealSubscribe events -> ingest -> copy engine + commissions
  * poller     : periodic sync of deals, positions and account summaries (safety net): active accounts every
                 pass, idle ones every SYNC_IDLE_INTERVAL_SECONDS; also notices the server being edited or
                 disabled in the admin and (re)subscribes; writes the worker heartbeat
  * scheduler  : PAMM NAV snapshots, monthly PAMM / copy performance fees, server health samples,
                 account activity (last connections), the daily demo account clean-up, pricing, automation
  * sync-jobs  : background sync tasks queued in the admin

Every loop survives errors (logged, retried with back-off); each scheduler step has its own transaction.
Run exactly one worker for the whole CRM, either standalone (`python -m app.workers`, which exits non-zero
when a thread dies so the service manager restarts it) or embedded in the API process (EMBEDDED_WORKER=true).
"""

import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal, utcnow
from app.models import MT5Server, PammFund, StrategyStatus
from app.mt5 import MT5Error, MT5Gateway, for_login, get_gateway
from app.mt5.gateway import MT5Deal
from app.core.errors import ServiceError
from app.services import (
    activity, automation, client_sync, copy_trading, demo_cleanup, events, ip_rules, mt5_sync, pamm, pricing,
    server_health, sync, sync_jobs, sync_runs,
)

log = logging.getLogger("worker")

NAV_SNAPSHOT_SECONDS = 3600
POLL_STATS_SECONDS = 60
FEE_PERIOD_DAYS = 30
PUSH_QUEUE_MAX = 20000  # pushed deals waiting for ingest; beyond this they are dropped and the poller catches up
SLOW_STEP_SECONDS = 30.0  # scheduler steps slower than this are logged
MAX_LOOP_BACKOFF = 60.0


class Worker:
    def __init__(self, gw: MT5Gateway | None = None):
        self._fixed_gw = gw  # tests: use this gateway for every server
        self.stop = threading.Event()
        self.queue: "queue.Queue[tuple[int, MT5Deal]]" = queue.Queue(maxsize=PUSH_QUEUE_MAX)
        self.threads: list[threading.Thread] = []
        self.beats: dict[str, float] = {}  # thread name -> when its loop last went round (monotonic)
        self.dropped = 0  # pushed deals dropped because the queue was full
        self._subs: dict[int, tuple[MT5Gateway, object]] = {}  # server_id -> (gateway, callback)
        self._backoff: dict[int, tuple[float, float]] = {}  # server_id -> (retry_at, delay)
        self._poll_saved: dict[int, float] = {}  # server_id -> when poll_stats was last written
        self._last_nav = 0.0
        self._poll_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="poll")  # one thread per MT5 server

    def _gateway(self, server_id: int) -> MT5Gateway:
        return self._fixed_gw or get_gateway(server_id)

    # ---------------------------------------------------------- servers
    def _enabled_servers(self) -> list[MT5Server]:
        with SessionLocal() as db:
            return list(db.scalars(select(MT5Server).where(MT5Server.enabled.is_(True))))

    def _mark(self, server_id: int, ok: bool, error: str | None = None) -> None:
        with SessionLocal() as db:
            server = db.get(MT5Server, server_id)
            if server is None:
                return
            status = "ok" if ok else "error"
            if server.last_status != status or (not ok and server.last_error != error):
                server.last_status, server.last_error = status, (error or "")[:512] or None
                server.last_checked_at = utcnow()
                db.commit()

    def _connect(self, server: MT5Server) -> MT5Gateway | None:
        """Connected gateway, or None while the server is in back-off after a failure."""
        retry_at, delay = self._backoff.get(server.id, (0.0, 1.0))
        if time.monotonic() < retry_at:
            return None
        try:
            gw = self._gateway(server.id)
            gw.ensure_connected()
        except Exception as exc:  # noqa: BLE001
            delay = min(delay * 2, 60.0)
            self._backoff[server.id] = (time.monotonic() + delay, delay)
            log.error("MT5 server '%s' unreachable (%s); retrying in %.0fs", server.name, exc, delay)
            self._mark(server.id, False, str(exc))
            return None
        self._backoff.pop(server.id, None)
        self._mark(server.id, True)
        self._subscribe(server.id, gw)
        return gw

    def _enqueue(self, server_id: int, deal: MT5Deal) -> None:
        """Push callback (runs on the MT5 thread): never blocks; a full queue drops the deal for the poller."""
        try:
            self.queue.put_nowait((server_id, deal))
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 1000 == 0:
                log.warning("push queue full (%d deals): dropped %d pushed deal(s) so far; the poller will "
                            "pick them up", PUSH_QUEUE_MAX, self.dropped)

    def _subscribe(self, server_id: int, gw: MT5Gateway) -> None:
        current = self._subs.get(server_id)
        if current and current[0] is gw:
            return
        if current:  # the gateway was rebuilt (settings changed): move the subscription
            self._unsubscribe(server_id)

        def on_deal(deal: MT5Deal, sid: int = server_id) -> None:
            self._enqueue(sid, deal)

        try:
            gw.subscribe_deals(on_deal)
            self._subs[server_id] = (gw, on_deal)
        except MT5Error as exc:
            log.warning("push events unavailable for server %s (%s); polling only", server_id, exc)

    def _unsubscribe(self, server_id: int) -> None:
        gw, callback = self._subs.pop(server_id, (None, None))
        if gw is not None:
            try:
                gw.unsubscribe_deals(callback)
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------- loops
    def _loop(self, name: str, step) -> None:
        """Run `step` until stopped. It returns the seconds to wait before the next round; an exception is logged
        and retried with a growing back-off (up to a minute), so a database or MT5 outage never kills the thread."""
        delay = 1.0
        while not self.stop.is_set():
            self.beats[name] = time.monotonic()
            try:
                wait = step()
                delay = 1.0
            except Exception:  # noqa: BLE001
                log.exception("%s loop failed; retrying in %.0fs", name, delay)
                wait, delay = delay, min(delay * 2, MAX_LOOP_BACKOFF)
            if wait:
                self.stop.wait(wait)

    def _push_step(self) -> float:
        try:
            batch = [self.queue.get(timeout=1)]
        except queue.Empty:
            return 0
        while len(batch) < 500:
            try:
                batch.append(self.queue.get_nowait())
            except queue.Empty:
                break
        by_server: dict[int, list[MT5Deal]] = {}
        for server_id, deal in batch:
            by_server.setdefault(server_id, []).append(deal)
        for server_id, deals in by_server.items():
            with SessionLocal() as db:
                try:
                    new = sync.ingest_deals(db, deals, server_id=server_id)
                    if new:
                        log.info("push: server %s ingested %d deals", server_id, len(new))
                except Exception:  # noqa: BLE001 - the poller fetches these deals again
                    db.rollback()
                    log.exception("push ingest failed (server %s)", server_id)
        return 0

    def _poll_step(self) -> float:
        interval = get_settings().sync_interval_seconds
        started = time.monotonic()
        servers = self._enabled_servers()
        for gone in set(self._subs) - {s.id for s in servers}:  # disabled or deleted in the admin
            self._unsubscribe(gone)
        if len(servers) > 1:  # each server in its own thread: a slow one doesn't hold up the others
            list(self._poll_pool.map(lambda server: self._poll_server(server, interval), servers))
        elif servers:
            self._poll_server(servers[0], interval)
        self._heartbeat()
        return max(1.0, interval - (time.monotonic() - started))

    def _poll_server(self, server: MT5Server, interval: int) -> None:
        gw = self._connect(server)
        if gw is None:
            return
        with SessionLocal() as db:
            try:
                stats = sync.poll(db, gw, server.id)
                if stats["new_deals"]:
                    log.info("poll '%s': %s", server.name, stats)
                self._record_poll(db, server.id, stats, interval)
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                log.exception("poll sync failed for server '%s'", server.name)
                self._mark(server.id, False, str(exc))

    def _record_poll(self, db, server_id: int, stats: dict, interval: int) -> None:
        """Keep the latest poll on the server row for the Server health page, at most once a minute."""
        if stats["seconds"] > interval:
            log.warning("polling server %s took %.1fs, longer than SYNC_INTERVAL_SECONDS (%ss)", server_id,
                        stats["seconds"], interval)
        if time.monotonic() - self._poll_saved.get(server_id, -POLL_STATS_SECONDS) < POLL_STATS_SECONDS:
            return
        server = db.get(MT5Server, server_id)
        if server is not None:
            keep = {k: v for k, v in (server.poll_stats or {}).items() if k.startswith("worker_")}  # heartbeat
            server.poll_stats = {**keep, **{k: v for k, v in stats.items() if k != "errors"}, "interval": interval,
                                 "errors": len(stats.get("errors", [])), "at": utcnow().isoformat()}
            db.commit()
            self._poll_saved[server_id] = time.monotonic()

    def _heartbeat(self) -> None:
        """Write the worker's last-alive time, and how long ago each thread went round, to the enabled server's
        poll_stats (`worker_alive_at`, `worker_threads`), so a dead or stuck worker can be told from a quiet one."""
        now = time.monotonic()
        beat = {"worker_alive_at": utcnow().isoformat(),
                "worker_threads": {name: round(now - at, 1) for name, at in self.beats.items()},
                "worker_dropped_pushes": self.dropped}
        with SessionLocal() as db:
            try:
                for server in db.scalars(select(MT5Server).where(MT5Server.enabled.is_(True))):
                    server.poll_stats = {**(server.poll_stats or {}), **beat}
                db.commit()
            except Exception:  # noqa: BLE001 - a missed heartbeat is not worth stopping the poller
                db.rollback()
                log.exception("worker heartbeat failed")

    def dead_threads(self) -> list[str]:
        """Names of worker threads that have exited although the worker was not stopped."""
        return [t.name for t in self.threads if not t.is_alive() and not self.stop.is_set()]

    def _scheduler_steps(self) -> list[tuple[str, object]]:
        fixed = self._fixed_gw

        def copy_fees(db) -> None:
            for sub_id, fee in copy_trading.settle_all_fees_due(db, fixed, FEE_PERIOD_DAYS).items():
                log.info("copy fee subscription %s: %s", sub_id, fee)

        def copy_reconcile(db) -> None:
            """Copy orders whose result MT5 did not confirm (dealer timeouts) are settled every minute."""
            from app.mt5 import routed
            from app.models import CopyExecution, CopyStrategy, CopySubscription

            waiting = (select(CopySubscription.strategy_id).join(CopyExecution, CopyExecution.subscription_id
                                                                 == CopySubscription.id)
                       .where(CopyExecution.status == "unknown"))
            for strategy in db.scalars(select(CopyStrategy).where(CopyStrategy.id.in_(waiting))).all():
                if copy_trading.reconcile(db, fixed or routed(), strategy):
                    log.info("copy strategy %s: unknown executions settled", strategy.id)

        return [
            ("PAMM", self._pamm_step),
            ("copy reconcile", copy_reconcile),
            ("copy fees", copy_fees),
            ("MT5 import", self._scheduled_import),
            ("client details retry", client_sync.retry_failed),  # client details MT5 could not take earlier
            ("IP rules expiry", ip_rules.expire),
            ("server health", lambda db: server_health.collect_due(db, fixed)),
            ("account activity", lambda db: activity.refresh_due(db, fixed)),
            ("demo clean-up", lambda db: demo_cleanup.run_due(db, fixed)),
            ("pricing", lambda db: pricing.run_due(db, fixed)),
            ("automation", lambda db: automation.run_due(db, fixed)),
        ]

    def _scheduler_step(self) -> float:
        """One scheduler round. Each step has its own session, so a failing step does not skip the rest."""
        for name, step in self._scheduler_steps():
            if self.stop.is_set():
                break
            self.beats["scheduler"] = time.monotonic()
            started = time.monotonic()
            with SessionLocal() as db:
                try:
                    step(db)
                except Exception:  # noqa: BLE001
                    db.rollback()
                    log.exception("scheduler step '%s' failed", name)
            took = time.monotonic() - started
            if took > SLOW_STEP_SECONDS:
                log.warning("scheduler step '%s' took %.1fs", name, took)
        return 60

    def _pamm_step(self, db) -> None:
        funds = list(db.scalars(select(PammFund).where(PammFund.status != StrategyStatus.closed)))
        snapshot_due = time.monotonic() - self._last_nav >= NAV_SNAPSHOT_SECONDS
        for fund in funds:
            try:
                gw = self._fixed_gw or for_login(db, fund.master_login)
                if snapshot_due:
                    pamm.snapshot(db, gw, fund)
                if pamm.due_for_fees(fund, FEE_PERIOD_DAYS):
                    log.info("PAMM fees fund %s: %s", fund.id, pamm.settle_fees(db, gw, fund))
            except Exception as exc:  # noqa: BLE001 - one fund must not hold up the others
                db.rollback()
                log.error("PAMM fund %s skipped: %s", fund.id, exc)
        if snapshot_due:
            self._last_nav = time.monotonic()

    def _scheduled_import(self, db) -> None:
        """Import MT5 groups and users every MT5_IMPORT_INTERVAL_MINUTES."""
        minutes = get_settings().mt5_import_interval_minutes
        if minutes <= 0:
            return
        due = utcnow() - timedelta(minutes=minutes)
        for server in db.scalars(select(MT5Server).where(MT5Server.enabled.is_(True))).all():
            if server.accounts_synced_at is not None and server.accounts_synced_at > due:
                continue
            name = server.name
            try:  # kept in the synchronization history as a scheduled run
                sync_runs.record(db, "trading_accounts", server,
                                 lambda: mt5_sync.sync_trading_accounts(db, server, None, self._fixed_gw))
            except (ServiceError, MT5Error) as exc:
                log.error("scheduled MT5 import failed for '%s': %s", name, getattr(exc, "message", None) or exc)

    def _jobs_step(self) -> float:
        """Run the next background sync task queued in the admin (MT5 > Import from MT5 > Trade sync)."""
        with SessionLocal() as db:
            try:
                job = sync_jobs.run_next(db)
            except Exception:
                db.rollback()
                raise
        return 0 if job is not None else 2

    def _recover_jobs(self) -> None:
        with SessionLocal() as db:
            try:
                if sync_jobs.recover_interrupted(db):
                    log.info("requeued interrupted sync tasks")
                if sync_runs.recover_interrupted(db, "schedule"):
                    log.info("scheduled synchronizations interrupted by the restart were marked as failed")
            except Exception:  # noqa: BLE001
                db.rollback()
                log.exception("could not requeue interrupted sync tasks")

    # ---------------------------------------------------------- lifecycle
    def start(self) -> "Worker":
        events.register_default_handlers()
        try:
            for server in self._enabled_servers():
                self._connect(server)
        except Exception:  # noqa: BLE001 - e.g. the database is still starting: the poller retries
            log.exception("could not reach the MT5 server at start-up; the poller will retry")
        self._recover_jobs()
        for name, step in (("deal-push", self._push_step), ("poller", self._poll_step),
                           ("scheduler", self._scheduler_step), ("sync-jobs", self._jobs_step)):
            t = threading.Thread(target=self._loop, args=(name, step), name=name, daemon=True)
            t.start()
            self.threads.append(t)
        log.info("worker started for %d MT5 server(s)", len(self._subs))
        return self

    def shutdown(self) -> None:
        self.stop.set()
        for server_id in list(self._subs):
            self._unsubscribe(server_id)
        for t in self.threads:
            t.join(timeout=10)
        self._poll_pool.shutdown(wait=False, cancel_futures=True)
        log.info("worker stopped")
