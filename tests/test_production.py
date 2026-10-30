"""Production hardening: idempotent deal ingest, worker robustness, capped counts, gateway locking, routing."""

import threading
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal, utcnow
from app.models import Deal, MT5Server, SyncState
from app.mt5.gateway import MT5Deal
from app.services import events, sync, trader_styles
from app.web import common
from app.web.admin.trading import table_rows
from app.workers import __main__ as worker_main
from app.workers import runner
from app.workers.runner import Worker


def _deal(ticket: int, login: int = 500001) -> MT5Deal:
    now = utcnow().replace(microsecond=0)
    return MT5Deal(ticket=ticket, login=login, time=now, time_msc=int(now.timestamp() * 1000), action=0, entry=0,
                   symbol="EURUSD", volume=Decimal("1"), price=Decimal("1.1"), profit=Decimal("0"))


# ---------------------------------------------------------------- deal ingest
def test_ingest_is_idempotent_across_sessions(db):
    first = sync.ingest_deals(db, [_deal(1), _deal(2), _deal(2)], publish=False, server_id=1)
    assert sorted(d.ticket for d in first) == [1, 2]
    with SessionLocal() as other:  # e.g. the API ingesting the same deals the worker just stored
        again = sync.ingest_deals(other, [_deal(2), _deal(3)], publish=False, server_id=1)
    assert [d.ticket for d in again] == [3]  # only the deal this call inserted is "new"
    assert db.scalar(select(func.count()).select_from(Deal)) == 3
    state = db.get(SyncState, 500001)
    assert state.last_deal_time is not None and state.server_id == 1


def test_concurrent_ingest_never_fails_and_publishes_each_deal_once(db):
    deals = [_deal(t) for t in range(100, 160)]
    results, errors = [], []

    def ingest():
        try:
            with SessionLocal() as s:
                results.append([d.ticket for d in sync.ingest_deals(s, deals, publish=False, server_id=1)])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=ingest) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(t for r in results for t in r) == list(range(100, 160))


def test_failing_event_handler_does_not_stop_the_others(db):
    seen = []
    events.register("boom", lambda s, d: (_ for _ in ()).throw(RuntimeError("boom")))
    events.register("after", lambda s, d: seen.extend(x.ticket for x in d))
    try:
        sync.ingest_deals(db, [_deal(7)], server_id=1)
    finally:
        events.unregister("boom")
        events.unregister("after")
    assert seen == [7]


# ---------------------------------------------------------------- worker
def test_push_queue_is_bounded(monkeypatch):
    w = Worker()
    w.queue.maxsize = 2
    for t in range(5):
        w._enqueue(1, _deal(t))
    assert w.queue.qsize() == 2 and w.dropped == 3


def test_loop_survives_errors_with_backoff(monkeypatch):
    w = Worker()
    calls = []

    def step():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database down")
        w.stop.set()
        return 0

    waits = []
    monkeypatch.setattr(w.stop, "wait", lambda s: waits.append(s))
    w._loop("test", step)
    assert len(calls) == 2 and waits == [1.0] and "test" in w.beats


def test_scheduler_step_failure_does_not_skip_the_rest(monkeypatch):
    w = Worker()
    ran = []

    def bad(db):
        raise RuntimeError("broken step")

    monkeypatch.setattr(w, "_scheduler_steps", lambda: [("bad", bad), ("good", lambda db: ran.append(db))])
    assert w._scheduler_step() == 60
    assert len(ran) == 1


def test_heartbeat_is_kept_by_poll_stats(db):
    w = Worker()
    w.beats["poller"] = 0.0
    w._heartbeat()
    w._record_poll(db, 1, {"seconds": 0.1, "new_deals": 0, "errors": []}, 10)
    db.expire_all()
    stats = db.get(MT5Server, 1).poll_stats
    assert stats["worker_alive_at"] and "poller" in stats["worker_threads"] and stats["seconds"] == 0.1


def test_supervisor_exits_non_zero_when_a_thread_dies():
    w = Worker()
    t = threading.Thread(target=lambda: None, name="poller")
    t.start()
    t.join()
    w.threads.append(t)
    assert worker_main.supervise(w, threading.Event(), every=0.01) == 1
    done = threading.Event()
    done.set()
    assert worker_main.supervise(Worker(), done, every=0.01) == 0


def test_worker_polls_with_the_mock_gateway(db, gw, monkeypatch):
    monkeypatch.setattr(runner, "get_settings", lambda: type("S", (), {"sync_interval_seconds": 5})())
    w = Worker(gw)
    assert w._poll_step() >= 1.0
    assert db.get(MT5Server, 1).poll_stats.get("worker_alive_at")


# ---------------------------------------------------------------- lists and counts
def test_page_count_is_capped(db, monkeypatch):
    sync.ingest_deals(db, [_deal(t) for t in range(1, 13)], publish=False, server_id=1)
    monkeypatch.setattr(common, "COUNT_CAP", 10)
    page = common.Page(db, select(Deal).order_by(Deal.ticket), 1, per_page=5)
    assert page.capped and str(page.total) == "10+" and page.total == 10 and len(page.items) == 5
    last = common.Page(db, select(Deal).order_by(Deal.ticket), 3, per_page=5)  # beyond the cap, still reachable
    assert [d.ticket for d in last.items] == [11, 12]
    small = common.Page(db, select(Deal).where(Deal.ticket < 4), 9, per_page=5)
    assert not small.capped and small.total == 3 and small.page == 1


def test_table_rows_counts_small_tables_exactly(db):
    sync.ingest_deals(db, [_deal(1), _deal(2)], publish=False, server_id=1)
    assert table_rows(db, Deal) == 2


def test_trader_styles_list_is_capped_to_the_busiest(db, gw, monkeypatch):
    from tests.test_trader_styles import START, Deals, accounts

    server = db.get(MT5Server, 1)
    quiet, busy = accounts(db, gw, server, 2)
    for login, n in ((quiet, 2), (busy, 4)):
        d = Deals(db, login)
        for i in range(n):
            d.position(START + timedelta(minutes=i * 10), 60)
    assert {r["account"].login for r in trader_styles.profiles(db, server, min_deals=1)} == {quiet, busy}
    monkeypatch.setattr(trader_styles, "MAX_PROFILES", 1)
    assert [r["account"].login for r in trader_styles.profiles(db, server, min_deals=1)] == [busy]


# ---------------------------------------------------------------- MT5 access
def test_routed_gateway_sends_everything_to_the_server(gw):
    from app.mt5 import routed

    assert routed().get_groups() == gw.get_groups()  # used to raise "groups belong to one server"
    login = gw.create_user(group="real\\standard", leverage=100, name="R", email="r@example.com", phone="",
                           country="DE", master_password="Pass-12345", investor_password="Inv-12345")
    assert routed().get_user(login).login == login


def test_manager_gateway_serializes_api_calls():
    pytest.importorskip("MT5Manager")
    from app.mt5.manager import ManagerGateway

    gw = object.__new__(ManagerGateway)  # no connection: only the locking is under test
    gw._lock, gw._connected = threading.RLock(), True
    owned = []

    class Api:
        def UserRequest(self, login):  # noqa: N802
            owned.append(gw._lock._is_owned())
            return None

    class M:
        @staticmethod
        def LastError():  # noqa: N802
            owned.append(gw._lock._is_owned())
            return (1, 13, "not found")

    gw.api, gw.M = Api(), M
    with pytest.raises(Exception):
        gw.get_user(1)
    assert owned and all(owned)
