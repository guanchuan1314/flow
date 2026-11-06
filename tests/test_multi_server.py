"""More than one MT5 server: each call reaches the right one, nothing lands on the wrong account, and every MT5 page can
choose between them. Also the Servers tab, closed positions, the real-time link and its catch-up, Workers and the logo."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

import app.mt5
from app.db import utcnow
from app.models import (
    AccountGroup, CompactSummary, Deal, MT5Server, Position, ServerKind, Symbol, SyncRun, TradingAccount,
)
from app.mt5 import BUY, MT5Error, routed
from app.mt5.gateway import MT5Deal
from app.mt5.mock import MockGateway
from app.services import (
    account_removal, app_settings, branding, closed_positions, compact, mt5_sync, servers, sync, trader_styles, workers,
)
from app.workers import runner
from app.workers.runner import Worker
from tests.test_compact import money, trade
from tests.test_trader_styles import accounts
from tests.test_web import admin_web, csrf_of, form_post  # noqa: F401

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
def server(db):
    return db.get(MT5Server, 1)


@pytest.fixture
def second(db):
    """Another server, with its own simulator."""
    return servers.create_server(db, None, name="Second", kind=ServerKind.simulator, terminal_name="Second-Live")


@pytest.fixture
def two(db, second, monkeypatch):
    """A gateway per server, as the running CRM has; accounts of both in Flow, with their own logins."""
    first, other = MockGateway(), MockGateway()
    first.connect()
    other.connect()
    other._counters = None
    gateways = {1: first, second.id: other}
    monkeypatch.setattr(app.mt5, "get_gateway", lambda server_id=None, connect=True: gateways[server_id or 1])
    for gw, srv, login in ((first, db.get(MT5Server, 1), 5001), (other, second, 7001)):
        gw.users[login] = gw.get_user(gw.create_user(group="real\\standard", leverage=100, name="A B", email=f"{login}@example.com",
                                                     phone="", country="DE", master_password="Pass-12345",
                                                     investor_password="Inv-12345"))
        gw.users[login].login = login
        gw.balance[login] = Decimal("0")
        gw.credit[login] = Decimal("0")
    return gateways


def row(db, login, server_id):
    from app.models import Client, ClientStatus

    client = Client(email=f"{login}@example.com", first_name="A", last_name="B", referral_code=f"r{login}",
                    status=ClientStatus.active)
    db.add(client)
    db.flush()
    group = AccountGroup(name=f"G{login}", server_id=server_id, mt5_group="real\\standard", currency="USD",
                         default_leverage=100, leverage_options=[100])
    db.add(group)
    db.flush()
    db.add(TradingAccount(login=login, server_id=server_id, client_id=client.id, account_group_id=group.id,
                          group="real\\standard", currency="USD", leverage=100, is_demo=False))
    db.commit()


def deal(ticket, login, entry=0, position=9, action=0, when=None, **kw):
    return MT5Deal(ticket=ticket, login=login, time=when or utcnow(), time_msc=0, action=action, entry=entry, symbol="EURUSD",
                   volume=Decimal(kw.pop("volume", "1")), price=Decimal(kw.pop("price", "1.1")), profit=Decimal(kw.pop("profit", "0")),
                   commission=Decimal(kw.pop("commission", "0")), swap=Decimal("0"), fee=Decimal("0"), position_id=position,
                   order=0, reason=0, comment="")


# ---------------------------------------------------------------- routing
def test_each_call_goes_to_the_accounts_server(db, second, two):
    row(db, 5001, 1)
    row(db, 7001, second.id)
    routed().balance_operation(7001, Decimal("50"), "Deposit")
    assert two[second.id].balance[7001] == 50 and two[1].balance[5001] == 0
    assert {a.login for a in routed().get_accounts([5001, 7001])} == {5001, 7001}


def test_an_unknown_login_is_not_guessed(db, second, two):
    with pytest.raises(MT5Error, match="not known"):
        routed().balance_operation(99999, Decimal("1"), "Deposit")
    with pytest.raises(MT5Error, match="per server"):
        routed().delete_deals([1])


# ---------------------------------------------------------------- the MT5 clock
def test_deals_dated_ahead_of_utc_are_found(db, gw, server):
    login, = accounts(db, gw, server, 1)
    gw.deals.append(deal(77, login, when=utcnow() + timedelta(hours=8)))  # a server on UTC+8
    assert sync.sync_deals(db, gw, [login], 1) == 1


# ---------------------------------------------------------------- real time and catching up
def test_position_and_order_events_read_the_account_again(db, gw, server):
    login, = accounts(db, gw, server, 1)
    w = Worker(gw)
    assert w._connect(server) is gw
    w._behind.clear()
    position = trade(gw, login, close=False)
    w._refresh_changed()
    assert db.scalar(select(Position.is_open).where(Position.position_id == position))
    w._refreshed = 0
    gw.close_position(login, position, "EURUSD", BUY, Decimal("0.1"))
    w._push_step()
    w._refreshed = 0
    w._refresh_changed()
    db.expire_all()
    assert db.scalar(select(Position.is_open).where(Position.position_id == position)) is False


def test_a_new_worker_catches_up_since_the_last_one(db, gw, server):
    login, = accounts(db, gw, server, 1)
    old = utcnow() - timedelta(days=3)
    gw.deals.append(deal(70, login, when=old + timedelta(hours=1)))  # while nothing was running
    sync.ingest_deals(db, [deal(71, login, when=utcnow())], publish=False, server_id=1)  # the account looks up to date
    server.poll_stats = {"worker_alive_at": old.isoformat()}
    db.commit()
    w = Worker(gw)
    w._poll_server(db.get(MT5Server, 1), 5)
    assert db.scalar(select(func.count()).select_from(Deal).where(Deal.ticket == 70)) == 1
    run = db.scalar(select(SyncRun).where(SyncRun.kind == "catch_up"))
    assert run.status == "done" and run.trigger == "schedule" and not w._behind
    w._poll_server(db.get(MT5Server, 1), 5)  # once
    assert db.scalar(select(func.count()).select_from(SyncRun).where(SyncRun.kind == "catch_up")) == 1


def test_a_dropped_link_catches_up_when_it_is_back(db, gw, server):
    accounts(db, gw, server, 1)
    w = Worker(gw)
    w._poll_server(server, 5)
    gw.disconnect()
    gw.connect()  # a new connection of the same gateway
    w._poll_server(server, 5)
    assert db.scalar(select(func.count()).select_from(SyncRun).where(SyncRun.kind == "catch_up")) == 2


def test_a_failed_catch_up_is_tried_again(db, gw, server, monkeypatch):
    accounts(db, gw, server, 1)
    w = Worker(gw)

    def down(*a):
        raise MT5Error("gone", 10)
    monkeypatch.setattr(sync, "catch_up", down)
    w._poll_server(server, 5)
    assert server.id in w._behind
    monkeypatch.undo()
    w._poll_server(server, 5)
    assert server.id not in w._behind


# ---------------------------------------------------------------- workers
def saved(db, **kw):
    now = utcnow()
    app_settings.save(db, workers.KEY, {"at": now.isoformat(), "started_at": now.isoformat(), "process": "here, process 1",
                                        "threads": {"deal-push": 1, "poller": 2, "scheduler": 3, "sync-jobs": 1},
                                        "servers": {"1": {"connected": True, "deals": True, "trades": True, "events": 4,
                                                          "last_event_at": now.isoformat()}},
                                        "steps": {"pricing": {"at": now.isoformat(), "seconds": 0.1, "failed": ""}},
                                        "queued": 0, "dropped": 0, **kw})
    db.commit()


def test_workers_status(db, server):
    assert not workers.status(db)["ever"]
    saved(db)
    w = workers.status(db)
    assert w["alive"] and [t["state"] for t in w["threads"]] == ["running"] * 4 and w["servers"][0]["state"] == "real time"
    saved(db, threads={"poller": 900, "deal-push": 1, "scheduler": 1, "sync-jobs": 1})
    assert next(t for t in workers.status(db)["threads"] if t["name"] == "Poller")["state"] == "stuck"
    saved(db, servers={"1": {"connected": True, "deals": True, "trades": False}})
    assert workers.status(db)["servers"][0]["state"] == "polling only"
    saved(db, at=(utcnow() - timedelta(minutes=5)).isoformat())
    w = workers.status(db)
    assert not w["alive"] and {t["state"] for t in w["threads"]} == {"stopped"} and w["servers"][0]["state"] == "stopped"


def test_the_worker_reports_itself(db, gw, server):
    w = Worker(gw)
    w.beats = {"poller": runner.time.monotonic()}
    w._poll_server(server, 5)
    w._heartbeat()
    status = workers.status(db)
    assert status["alive"] and status["servers"][0]["state"] == "real time" and status["process"]


def test_workers_page(admin_web, db):  # noqa: F811
    assert "has not reported yet" in admin_web.get("/admin/workers").text
    saved(db)
    page = admin_web.get("/admin/workers").text
    assert "Real-time events" in page and "Real time" in page and "Pricing" in page
    assert "<html" not in admin_web.get("/admin/workers?partial=1").text
    assert 'href="/admin/workers"' in page


# ---------------------------------------------------------------- closed positions
def closed(db, **kw):
    return db.execute(closed_positions.query(**kw)).all()


def test_closed_positions_come_from_their_deals(db, server):
    now = utcnow()
    sync.ingest_deals(db, [
        deal(1, 5001, entry=0, position=9, volume="1", price="1.10", commission="-2", when=now - timedelta(hours=2)),
        deal(2, 5001, entry=0, position=9, volume="1", price="1.20", commission="-2", when=now - timedelta(hours=1)),
        deal(3, 5001, entry=1, position=9, action=1, volume="0.5", price="1.30", profit="5", when=now - timedelta(minutes=30)),
        deal(4, 5001, entry=1, position=9, action=1, volume="1.5", price="1.40", profit="20", when=now),
        deal(5, 5001, entry=0, position=10, volume="1"),  # only opened
        deal(6, 5001, entry=1, position=11, action=1, volume="1", price="1.5", profit="3"),  # its opening deal is missing
        deal(7, 5001, entry=0, position=12, volume="2", when=now - timedelta(hours=1)),  # partly closed
        deal(8, 5001, entry=1, position=12, action=1, volume="1", when=now),
    ], publish=False, server_id=1)
    db.add(Position(server_id=1, position_id=12, login=5001, symbol="EURUSD", action=0, volume=Decimal("1"),
                    price_open=Decimal("1"), time_create=now, synced_at=now))
    db.commit()
    rows = {r.position_id: r for r in closed(db)}
    assert set(rows) == {9, 11}
    p = rows[9]
    assert p.volume == 2 and p.profit == 25 and p.commission == -4 and p.action == 0
    assert round(p.price_open, 2) == Decimal("1.15") and round(p.price_close, 3) == Decimal("1.375")
    assert p.opened_at == now - timedelta(hours=2) and p.closed_at == now
    assert rows[11].opened_at is None and rows[11].price_open is None and rows[11].action == 0
    assert closed_positions.totals(db, closed_positions.query())[3] == 28
    assert {r.position_id for r in closed(db, closed_from=now + timedelta(days=1))} == set()


def test_compacting_keeps_closed_positions(db, gw, server, admin):
    login, = accounts(db, gw, server, 1)
    money(gw, login, ("1000", "Deposit #1"))
    position = trade(gw, login)
    sync.sync_deals(db, gw, [login], 1)
    before = closed(db)
    assert [r.position_id for r in before] == [position]
    compact.run(db, admin, server, [login], gw=gw)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Deal).where(Deal.position_id == position)) == 0
    after = closed(db)
    assert [(r.position_id, r.volume, r.profit) for r in after] == [(position, before[0].volume, before[0].profit)]


def test_closed_positions_page(admin_web, db, second):  # noqa: F811
    row(db, 5001, 1)
    sync.ingest_deals(db, [deal(1, 5001), deal(2, 5001, entry=1, action=1, profit="7")], publish=False, server_id=1)
    page = admin_web.get("/admin/trades/positions/closed").text
    assert "Closed positions" in page and "/admin/trades/deals/data?position=9" in page and "Open positions" in page
    link = "/admin/accounts/trading-accounts/1:5001"
    assert link in page and link not in admin_web.get(f"/admin/trades/positions/closed?server={second.id}").text
    assert link not in admin_web.get("/admin/trades/positions/closed?login=6*").text
    assert 'value="1"' in admin_web.get("/admin/trades/deals/data?position=9").text


# ---------------------------------------------------------------- servers
def test_servers_tab_adds_edits_and_deletes(admin_web, db):  # noqa: F811
    page = admin_web.get("/admin/servers/list").text
    assert ">Servers</a>" in page and "Configuration" not in page.split("crm-topbar")[0] and "/admin/servers/list/new" in page
    r = form_post(admin_web, "/admin/servers/list/new", "/admin/servers/list/new",
                  {"name": "Asia", "kind": "simulator", "terminal_name": "Broker-Asia", "timeout_seconds": "10", "enabled": "on"})
    assert "saved. Connection test:" in r.text
    asia = db.scalar(select(MT5Server).where(MT5Server.name == "Asia"))
    assert not asia.is_price_feed and db.get(MT5Server, 1).is_price_feed
    form_post(admin_web, f"/admin/servers/list/{asia.id}", f"/admin/servers/list/{asia.id}",
              {"name": "Asia", "kind": "simulator", "terminal_name": "Broker-Asia", "timeout_seconds": "10", "enabled": "on",
               "price_feed": "on"})
    db.expire_all()
    assert db.get(MT5Server, asia.id).is_price_feed and not db.get(MT5Server, 1).is_price_feed
    r = form_post(admin_web, f"/admin/servers/list/{asia.id}", f"/admin/servers/list/{asia.id}/delete", {})
    assert "removed from the CRM" in r.text
    assert admin_web.get("/admin/configuration").url.path == "/admin/servers/list"
    assert admin_web.get("/admin/configuration/servers/new").url.path == "/admin/servers/list/new"
    assert admin_web.get("/admin/server-tools/health").url.path == "/admin/servers/health"


def test_deleting_a_server_takes_its_compacted_history(db, second):
    row(db, 7001, second.id)
    db.add(CompactSummary(server_id=second.id, login=7001, ticket=1, key="x", action=2, label="x", date_from=utcnow(),
                          date_to=utcnow(), amount=Decimal("1"), created_at=utcnow(), updated_at=utcnow()))
    db.commit()
    servers.delete_server(db, None, second, purge=True)
    assert db.scalar(select(func.count()).select_from(CompactSummary)) == 0


def test_removal_after_import_only_touches_its_server(db, gw, server, second):
    login, = accounts(db, gw, server, 1)
    row(db, 7001, second.id)
    db.execute(TradingAccount.__table__.update().where(TradingAccount.login == 7001).values(on_server=False))
    db.commit()
    assert account_removal.remove_gone(db, server, gw)["removed"] == 0
    assert db.scalar(select(func.count()).select_from(TradingAccount).where(TradingAccount.login == 7001)) == 1


# ---------------------------------------------------------------- the server filter
PAGES = ["/admin/trades/positions/data", "/admin/trades/positions/closed", "/admin/trades/positions/sync",
         "/admin/trades/positions/history", "/admin/trades/orders/data", "/admin/trades/orders/sync", "/admin/trades/orders/history",
         "/admin/trades/orders/controls", "/admin/trades/deals/data", "/admin/trades/deals/sync", "/admin/trades/deals/history",
         "/admin/trades/deals/controls", "/admin/accounts/trading-accounts", "/admin/accounts/account-groups",
         "/admin/accounts/trader-styles", "/admin/accounts/trader-styles/setup", "/admin/accounts/inactive-accounts",
         "/admin/accounts/compact", "/admin/symbols/data", "/admin/symbols/history", "/admin/trading-setup/overview",
         "/admin/trading-setup/liquidity", "/admin/trading-setup/routing", "/admin/import/account-groups",
         "/admin/import/trading-accounts", "/admin/import/pricing", "/admin/import/positions", "/admin/import/orders",
         "/admin/import/deals", "/admin/import/history", "/admin/servers/health", "/admin/servers/restart"]


@pytest.mark.parametrize("path", PAGES)
def test_every_mt5_page_has_the_server_filter(admin_web, second, path):  # noqa: F811
    r = admin_web.get(f"{path}?server={second.id}")
    assert r.status_code == 200, path
    assert 'name="server"' in r.text and "Second" in r.text, path
    assert f'<option value="{second.id}" selected>' in r.text, path


def test_a_page_shows_the_chosen_server(admin_web, db, second):  # noqa: F811
    db.add_all([Symbol(server_id=1, name="ONE", path="a"), Symbol(server_id=second.id, name="TWO", path="b")])
    db.commit()
    assert "ONE" in admin_web.get("/admin/symbols/data").text
    page = admin_web.get(f"/admin/symbols/data?server={second.id}").text
    assert "TWO" in page and ">ONE<" not in page


def test_a_sync_button_works_on_the_chosen_server(admin_web, db, second):  # noqa: F811
    form_post(admin_web, f"/admin/symbols/data?server={second.id}", "/admin/symbols/data/sync", {"server_id": second.id})
    assert db.scalar(select(SyncRun.server_id).where(SyncRun.kind == "symbols")) == second.id


def test_style_setup_stays_on_its_server(admin_web, db, second):  # noqa: F811
    url = "/admin/accounts/trader-styles/setup"
    r = form_post(admin_web, f"{url}?server={second.id}", f"{url}/scalper/delete?server={second.id}", {})
    assert f"server={second.id}" in str(r.url)
    db.expire_all()
    assert "scalper" not in [x["key"] for x in trader_styles.rules(db.get(MT5Server, second.id))]
    assert "scalper" in [x["key"] for x in trader_styles.rules(db.get(MT5Server, 1))]


# ---------------------------------------------------------------- logo
@pytest.fixture
def logo_cache():
    app_settings.clear()
    yield
    app_settings.clear()


def test_logo_shows_everywhere_and_can_go(admin_web, logo_cache):  # noqa: F811
    assert "brand-logo" not in admin_web.get("/admin/branding").text
    csrf = csrf_of(admin_web.get("/admin/branding"))
    r = admin_web.post("/admin/branding/logo", data={"_csrf": csrf}, files={"logo": ("logo.png", PNG, "image/png")})
    assert "Logo saved" in r.text and "brand-logo" in r.text
    version = branding.logo()["version"]
    for path in ("/", "/portal/login", "/admin/login"):
        assert f"/brand/logo?v={version}" in admin_web.get(path).text, path
    got = admin_web.get(f"/brand/logo?v={version}")
    assert got.content == PNG and got.headers["content-type"] == "image/png"
    r = form_post(admin_web, "/admin/branding", "/admin/branding/logo/remove", {})
    assert "Logo removed" in r.text and "brand-logo" not in r.text
    assert admin_web.get("/brand/logo").status_code == 404


@pytest.mark.parametrize("content,message", [
    (b"MZ not an image", "must be a PNG"),
    (b"<html><script>alert(1)</script></html>", "must be a PNG"),
    (b"", "Choose a logo file"),
    (PNG + b"\x00" * (2 * 1024 * 1024), "too big"),
], ids=["program", "web page", "empty", "too big"])
def test_logo_is_checked(db, content, message, logo_cache):
    from app.core.errors import ServiceError

    with pytest.raises(ServiceError, match=message):
        branding.save_logo(db, None, content)


def test_an_svg_logo_cannot_run_scripts(admin_web, db, logo_cache):  # noqa: F811
    branding.save_logo(db, None, b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    r = admin_web.get("/brand/logo")
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert "sandbox" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"


def test_the_brand_name_in_the_logo_is_escaped(db, logo_cache):
    branding.save_logo(db, None, PNG)
    branding.save(db, None, {"app_name": 'A"><b>'})
    assert '"><b>' not in branding.mark()
