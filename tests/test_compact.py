"""Trading accounts > Compact: summaries on MT5, exact copies in Flow, and a balance that never moves."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError

from app.core.errors import ServiceError
from app.models import CompactedRecord, CompactSummary, Deal, MT5Server, Order, Position
from app.models.trading import DEAL_BALANCE, DEAL_CORRECTION, DEAL_CREDIT
from app.mt5 import MT5Error
from app.mt5.gateway import BUY
from app.services import compact, sync
from tests.test_trader_styles import accounts
from tests.test_web import admin_web, form_post  # noqa: F401

URL = "/admin/accounts/compact"


@pytest.fixture
def server(db):
    return db.get(MT5Server, 1)


@pytest.fixture
def login(db, gw, server, admin):
    login, = accounts(db, gw, server, 1)
    return login


def money(gw, login, *rows):
    for value, comment in rows:
        gw.balance_operation(login, Decimal(value), comment)


def trade(gw, login, symbol="EURUSD", close=True):
    opened = gw.open_position(login, symbol, BUY, Decimal("0.1"))
    if close:
        gw.close_position(login, opened.position_id, symbol, BUY, Decimal("0.1"))
    return opened.position_id


def history(gw, login):
    return gw.get_deals([login], compact.BEGINNING, datetime(2100, 1, 1))


def test_comment_fits_mt5():
    text = compact.comment_for("Correction EURUSD.ecn", datetime(2024, 1, 1), datetime(2024, 12, 31))
    assert text == "Correction EURUSD 240101-241231" and len(text) == 31
    assert compact._money_comment("Deposit #12") == compact._money_comment("Deposit #13") == "Deposit"


def test_plan_groups_and_only_reads(db, gw, server, admin, login):
    money(gw, login, ("1000", "Deposit #1"), ("500", "Deposit #2"), ("-100", "Withdrawal #3"))
    gw.balance_operation(login, Decimal("50"), "Bonus", credit=True)
    trade(gw, login), trade(gw, login), trade(gw, login, "GBPUSD")
    before = list(history(gw, login))
    plan = compact.make_plan(db, gw, server, login)
    lines = {(x.action, x.label): x for x in plan.lines}
    assert lines[(DEAL_BALANCE, "Deposit")].total == 1500 and lines[(DEAL_BALANCE, "Withdrawal")].total == -100
    assert lines[(DEAL_CREDIT, "Bonus")].total == 50
    assert len(lines[(DEAL_CORRECTION, "Correction EURUSD")].rows) == 4 and (DEAL_CORRECTION, "Correction GBPUSD") in lines
    assert history(gw, login) == before

    merged = compact.make_plan(db, gw, server, login, by=[])
    assert sorted((x.action, x.label) for x in merged.lines) == [(2, "Balance"), (3, "Credit"), (5, "Correction")]


def test_compact_keeps_the_balance_and_copies_everything(db, gw, server, admin, login):
    money(gw, login, ("1000", "Deposit #1"), ("500", "Deposit #2"))
    position = trade(gw, login)
    trade(gw, login)
    balance, deals = gw.get_account(login).balance, len(history(gw, login))
    assert deals == 6

    result = compact.run(db, admin, server, [login], gw=gw)
    assert (result["accounts"], result["deals"], result["lines"], result["errors"]) == (1, 6, 2, [])
    assert gw.get_account(login).balance == balance and gw.check_balance(login)[0]

    rows = {d.comment.rsplit(" ", 1)[0]: d for d in history(gw, login)}
    assert set(rows) == {"Deposit", "Correction EURUSD"}
    assert rows["Deposit"].profit == 1500 and rows["Deposit"].action == DEAL_BALANCE
    correction = rows["Correction EURUSD"]
    assert correction.action == DEAL_CORRECTION and correction.symbol == "EURUSD" and correction.volume == 0
    assert correction.profit == balance - 1500

    copies = db.scalars(select(CompactedRecord).where(CompactedRecord.login == login, CompactedRecord.kind == "deal")).all()
    assert len(copies) == 6 and all(c.summary_id for c in copies)
    assert {c.data["position_id"] for c in copies} >= {position}
    assert not gw.get_order_history([login], compact.BEGINNING, datetime(2100, 1, 1))
    assert db.scalar(select(CompactedRecord).where(CompactedRecord.kind == "order", CompactedRecord.login == login))


def test_compacting_again_adds_to_the_summary(db, gw, server, admin, login):
    money(gw, login, ("1000", "Deposit #1"))
    compact.run(db, admin, server, [login], gw=gw)
    first = db.scalar(select(CompactSummary).where(CompactSummary.login == login))
    start, ticket = first.date_from, first.ticket
    db.expire_all()

    money(gw, login, ("250", "Deposit #2"), ("50", "Deposit #3"))
    plan = compact.make_plan(db, gw, server, login)
    assert plan.lines[0].summary.id == first.id and plan.lines[0].added == 300 and plan.lines[0].total == 1300

    result = compact.run(db, admin, server, [login], gw=gw)
    assert result["errors"] == []
    db.expire_all()
    summary = db.scalar(select(CompactSummary).where(CompactSummary.login == login))
    assert (summary.ticket, summary.amount, summary.deals, summary.date_from) == (ticket, 1300, 3, start)
    assert summary.date_to > start
    rows = history(gw, login)
    assert len(rows) == 1 and rows[0].profit == 1300 and gw.check_balance(login)[0]

    assert compact.run(db, admin, server, [login], gw=gw)["accounts"] == 0  # nothing new


def test_open_positions_pending_orders_and_cut_positions_stay(db, gw, server, admin, login):
    money(gw, login, ("1000", "Deposit #1"))
    closed = trade(gw, login)
    running = trade(gw, login, close=False)
    pending = gw.place_order(login, "EURUSD", 2, Decimal("0.1"), Decimal("1"))
    compact.run(db, admin, server, [login], gw=gw)
    assert [p.position_id for p in gw.get_positions([login])] == [running]
    assert [o.ticket for o in gw.get_orders([login])] == [pending]
    left = history(gw, login)
    assert [d.position_id for d in left if d.action in (0, 1)] == [running]
    assert closed not in {o.position_id for o in gw.get_order_history([login], compact.BEGINNING, datetime(2100, 1, 1))}
    assert running in {o.position_id for o in gw.get_order_history([login], compact.BEGINNING, datetime(2100, 1, 1))}
    assert gw.check_balance(login)[0]


def test_a_position_is_never_cut_by_the_dates(db, gw, server, admin, login):
    money(gw, login, ("1000", "Deposit #1"))
    position = trade(gw, login)
    rows = [d for d in history(gw, login) if d.position_id == position]
    rows[0].time -= timedelta(days=10)  # opened before the date chosen below
    plan = compact.make_plan(db, gw, server, login, date_from=rows[1].time.date().isoformat())
    assert not plan.positions and plan.kept_deals == 2
    assert [x.label for x in plan.lines] == ["Deposit"]


def test_flow_follows_mt5(db, gw, server, admin, login):
    money(gw, login, ("1000", "Deposit #1"), ("5", "Deposit #2"))
    trade(gw, login)
    sync.sync_deals(db, gw, [login])
    sync.sync_orders(db, gw, [login])
    assert db.scalars(select(Deal).where(Deal.login == login)).all()
    compact.run(db, admin, server, [login], gw=gw)
    db.expire_all()
    rows = db.scalars(select(Deal).where(Deal.login == login)).all()
    assert sorted(float(r.profit) for r in rows) == sorted(float(d.profit) for d in history(gw, login))
    assert not db.scalars(select(Position).where(Position.login == login, Position.is_open.is_(False))).all()
    assert not db.scalars(select(Order).where(Order.login == login, Order.is_open.is_(False))).all()


def test_an_account_that_already_differs_is_left_alone(db, gw, server, admin, login):
    money(gw, login, ("1000", "Deposit #1"), ("500", "Deposit #2"))
    gw.delete_deals([history(gw, login)[0].ticket])
    result = compact.run(db, admin, server, [login], gw=gw)
    assert result["skipped"] == 1 and "already differ" in result["errors"][0] and len(history(gw, login)) == 1


def test_a_lost_summary_stops_the_account(db, gw, server, admin, login):
    money(gw, login, ("1000", "Deposit #1"))
    compact.run(db, admin, server, [login], gw=gw)
    ticket = history(gw, login)[0].ticket
    gw.delete_deals([ticket])
    gw.balance[login] = Decimal("0")
    assert "no longer on MT5" in compact.make_plan(db, gw, server, login).problem


def test_a_refused_delete_stays_out_of_the_total(db, gw, server, admin, login, monkeypatch):
    money(gw, login, ("1000", "Deposit #1"), ("500", "Deposit #2"), ("20", "Deposit #3"))
    tickets = [d.ticket for d in history(gw, login)]
    real = gw.delete_deals
    monkeypatch.setattr(gw, "delete_deals", lambda rows: {**real([t for t in rows if t != tickets[0]]), tickets[0]: "busy"})
    result = compact.run(db, admin, server, [login], gw=gw)
    assert result["failed"] == 1 and "busy" in result["errors"][0]
    assert sorted(d.profit for d in history(gw, login)) == [520, 1000] and gw.check_balance(login)[0]
    assert not db.get(CompactedRecord, (1, "deal", tickets[0]))


def test_an_mt5_failure_is_reported_and_the_next_account_goes_on(db, gw, server, admin, monkeypatch):
    first, second = accounts(db, gw, server, 2)
    money(gw, first, ("10", "Deposit #1"))
    money(gw, second, ("20", "Deposit #2"))
    real = gw.rewrite_deal

    def rewrite(ticket, **kw):
        if kw["amount"] == 10:
            raise MT5Error("lost")
        return real(ticket, **kw)
    monkeypatch.setattr(gw, "rewrite_deal", rewrite)
    result = compact.run(db, admin, server, [first, second], gw=gw)
    assert result["accounts"] == 1 and result["failed"] == 1 and "lost" in result["errors"][0]


def test_choose(db, gw, server, admin, login):
    assert compact.choose(db, admin, server, f"{login}, 999999") == [login]
    with pytest.raises(ServiceError, match="at least one"):
        compact.choose(db, admin, server, "999999")


def test_pages(admin_web, db, gw, server, login):  # noqa: F811
    money(gw, login, ("1000", "Deposit #1"), ("500", "Deposit #2"))
    trade(gw, login)
    page = admin_web.get(URL).text
    assert 'active" href="/admin/accounts/compact">Compact<' in page and "Nothing compacted yet" in page

    preview = admin_web.get(f"{URL}?preview=1&logins={login}&by=comment&by=symbol").text
    assert "Compact on MT5" in preview and "Correction EURUSD" in preview and "Deposit" in preview
    assert "1,500" in preview or "1500" in preview
    assert len(history(gw, login)) == 4  # a preview changes nothing
    assert "Choose at least one" in admin_web.get(f"{URL}?preview=1&logins=1").text

    r = form_post(admin_web, URL, f"{URL}/run", {"logins": str(login), "by": ["comment", "symbol"], "date_from": "", "date_to": ""})
    assert r.status_code == 200
    assert len(history(gw, login)) == 2
    db.expire_all()
    summary = db.scalar(select(CompactSummary).where(CompactSummary.login == login, CompactSummary.action == DEAL_BALANCE))
    page = admin_web.get(URL).text
    assert "Exact deals" in page
    detail = admin_web.get(f"{URL}/{summary.id}").text
    assert "Deposit #1" in detail and "Deposit #2" in detail
    assert "Compact history" in admin_web.get(f"{URL}/history").text


def test_needs_the_permission(db, gw, server, admin):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.services import staff as staff_svc
    from tests.test_web import web_login

    role = staff_svc.create_role(db, admin, "No compact", ["accounts.view", "mt5_trades.delete"])
    staff_svc.create_staff(db, admin, email="nc@b.com", full_name="NC", password="Staff-Pass1", role_ids=[role.id])
    c = TestClient(create_app())
    web_login(c, "nc@b.com", "Staff-Pass1")
    assert c.get(URL).status_code == 403
    assert ">Compact<" not in c.get("/admin/accounts/trading-accounts").text


def test_closed_positions_move_from_flow_to_the_copies(db, gw, server, admin, login):
    from app.db import utcnow

    money(gw, login, ("1000", "Deposit #1"))
    position = trade(gw, login)
    db.add(Position(server_id=1, position_id=position, login=login, symbol="EURUSD", action=0, volume=Decimal("0.1"),
                    price_open=Decimal("1"), time_create=utcnow(), is_open=False, closed_at=utcnow(), synced_at=utcnow()))
    db.commit()
    compact.run(db, admin, server, [login], gw=gw)
    db.expire_all()
    assert not db.get(Position, (1, position))
    copy = db.get(CompactedRecord, (1, "position", position))
    assert copy.data["symbol"] == "EURUSD" and copy.login == login


def test_flow_failing_leaves_mt5_correct_and_the_next_account_goes_on(db, gw, server, admin, monkeypatch):
    first, second = accounts(db, gw, server, 2)
    money(gw, first, ("10", "Deposit #1"), ("5", "Deposit #2"))
    money(gw, second, ("20", "Deposit #3"), ("5", "Deposit #4"))
    real = compact._saved
    calls = []

    def failing(session, work):
        calls.append(1)
        if len(calls) == 2:  # the save after the first group's MT5 work
            raise RuntimeError("database gone")
        real(session, work)
    monkeypatch.setattr(compact, "_saved", failing)
    result = compact.run(db, admin, server, [first, second], gw=gw)
    assert result["failed"] == 1 and "database gone" in result["errors"][0] and result["accounts"] == 1
    assert [d.profit for d in history(gw, first)] == [15] and gw.check_balance(first)[0]  # MT5 finished its part


def test_a_collision_with_a_sync_is_tried_again(db, monkeypatch):
    monkeypatch.setattr(compact.time, "sleep", lambda s: None)
    tries = []

    def work():
        tries.append(1)
        if len(tries) < 3:
            raise OperationalError("delete", {}, Exception("deadlock detected"))
    compact._saved(db, work)
    assert len(tries) == 3

    def never():
        raise OperationalError("delete", {}, Exception("deadlock detected"))
    with pytest.raises(OperationalError):
        compact._saved(db, never)


def test_a_sync_does_not_bring_compacted_deals_back(db, gw, server, admin, login):
    money(gw, login, ("1000", "Deposit #1"), ("5", "Deposit #2"))
    stale = list(history(gw, login))  # what a sync that started earlier has read
    tickets = [d.ticket for d in stale]
    compact.run(db, admin, server, [login], gw=gw)
    db.execute(delete(Deal).where(Deal.ticket.in_(tickets)))
    db.commit()
    sync.ingest_deals(db, stale, publish=False, server_id=1)
    db.commit()
    assert not db.scalars(select(Deal).where(Deal.ticket.in_(tickets))).all()


def test_summaries_are_dated_today_and_their_comment_gives_the_period(db, gw, server, admin, login, monkeypatch):
    money(gw, login, ("1000", "Deposit #1"), ("5", "Deposit #2"))
    sync.sync_deals(db, gw, [login])
    old = datetime(2024, 3, 1, 12)
    for d in history(gw, login):
        d.time = old
    today = datetime(2026, 11, 6, 23, 30)  # the server's clock, not Flow's
    monkeypatch.setattr(gw, "server_time", lambda: today)
    compact.run(db, admin, server, [login], gw=gw)
    deal = history(gw, login)[0]
    assert deal.time == today and deal.comment == "Deposit 240301-240301"
    db.expire_all()
    summary = db.scalar(select(CompactSummary).where(CompactSummary.login == login))
    assert (summary.date_from, summary.date_to) == (old, old)  # the period stays the deals' own
    assert db.scalar(select(Deal.time).where(Deal.ticket == deal.ticket)) == today
