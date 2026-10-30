"""One server, many users: the worker's tiered polling, server health, demo clean-up and trader styles."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import ServiceError
from app.db import utcnow
from app.models import (
    AccountGroup, AccountPurpose, AccountStatus, AuditLog, Deal, MT5Server, ServerHealthSample, ServerKind,
    TradingAccount,
)
from app.mt5 import BUY, MT5Error
from app.services import activity, demo_cleanup, mt5_sync, server_health, sync, trader_styles
from tests.test_web import admin_web, form_post  # noqa: F401


@pytest.fixture
def server(db):
    return db.get(MT5Server, 1)


def users(gw, group, n):
    return [gw.create_user(group=group, leverage=100, name=f"Trader {i}", email=f"t{i}-{group[-3:]}@example.com",
                           phone="", country="DE", master_password="Pass-12345", investor_password="Inv-12345")
            for i in range(n)]


def imported(db, gw, server, group, n):
    logins = users(gw, group, n)
    mt5_sync.sync_trading_accounts(db, server, None, gw)
    return logins


# ---------------------------------------------------------------- tiered polling
def test_idle_accounts_are_polled_in_turn_active_ones_every_pass(db, gw, server, monkeypatch):
    logins = imported(db, gw, server, "real\\standard", 12)
    monkeypatch.setattr(sync, "CHUNK", 2)  # 12 idle accounts, 10 s passes, 300 s cycle: 1 per pass, at least a chunk

    first = sync.poll(db, gw, 1)
    assert (first["accounts"], first["active"], first["idle"], first["idle_polled"]) == (12, 0, 12, 2)
    second = sync.poll(db, gw, 1)
    assert second["idle_polled"] == 2  # the next two in line, not the same ones again
    assert first["calls"] == 5 and first["seconds"] >= 0

    res = gw.open_position(logins[5], "EURUSD", BUY, Decimal("1"))  # a pushed deal makes the account active
    sync.ingest_deals(db, [d for d in gw.deals if d.login == logins[5]], server_id=1)
    third = sync.poll(db, gw, 1)
    assert third["active"] == 1
    pos = db.scalar(select(TradingAccount).where(TradingAccount.login == logins[5]))
    assert pos is not None and sync.active_logins(db, 1, utcnow() - timedelta(minutes=30)) == {logins[5]}
    gw.close_position(logins[5], res.position_id, "EURUSD", BUY, Decimal("1"))
    sync.poll(db, gw, 1)  # still active: it had a deal a moment ago, and the closed position is noticed
    assert not sync.active_logins(db, 1, utcnow() + timedelta(minutes=1))  # no open position left


def test_everything_is_polled_soon_after_the_idle_interval(db, gw, server, monkeypatch):
    imported(db, gw, server, "real\\standard", 4)
    assert sync.poll(db, gw, 1)["idle_polled"] == 4
    assert sync.poll(db, gw, 1)["idle_polled"] == 0  # all polled just now
    monkeypatch.setattr(sync.get_settings(), "sync_idle_interval_seconds", 0)
    assert sync.poll(db, gw, 1)["idle_polled"] == 4


# ---------------------------------------------------------------- server health
def test_health_is_sampled_checked_and_pruned(db, gw, server):
    sample = server_health.collect(db, server, gw)
    assert sample.error is None and [c["name"] for c in sample.components] == [
        "Main Trade Server", "Access Server Europe", "Access Server Asia", "History Server"]
    assert server_health.collect_due(db, gw) == 0  # just read

    old = ServerHealthSample(server_id=1, taken_at=utcnow() - timedelta(days=30), components=[], poll={})
    db.add(old)
    db.commit()
    server_health.collect(db, server, gw)
    assert db.get(ServerHealthSample, old.id) is None  # older than HEALTH_KEEP_DAYS

    r = server_health.report(db, server)
    assert r["shared"] == [("127.0.0.1", ["Main Trade Server", "Access Server Europe", "History Server"])]
    assert all(c["level"] == "ok" for c in r["components"]) and r["level"] == "ok"
    asia = next(c for c in r["components"] if c["name"] == "Access Server Asia")
    assert asia["trends"]["connections"][1] is not None and asia["trends"]["connections"][0]  # its own trend line


def test_disconnected_access_server_is_alerted_everywhere(db, gw, server, admin_web):  # noqa: F811
    server_health.collect(db, server, gw)
    gw.down_components.add(4)  # Access Server Asia goes down
    first = server_health.collect(db, server, gw)
    server_health.collect(db, server, gw)
    entry = db.scalar(select(AuditLog).where(AuditLog.action == "server_health.disconnected"))
    assert entry is not None and entry.details["component"] == "Access Server Asia"  # logged once, when it changed
    assert len(db.scalars(select(AuditLog).where(AuditLog.action == "server_health.disconnected")).all()) == 1

    found = server_health.alerts(db)
    assert [(a["name"], a["access"], a["since"]) for a in found] == [("Access Server Asia", True, first.taken_at)]
    r = server_health.report(db, server)
    asia = next(c for c in r["components"] if c["name"] == "Access Server Asia")
    assert (asia["level"], asia["down_since"], r["level"]) == ("danger", first.taken_at, "danger")

    page = admin_web.get("/admin/clients").text  # the banner is on every admin page
    assert 'Access server "Access Server Asia" is disconnected' in page and "Clients connected to it are cut off" in page

    gw.down_components.clear()
    server_health.collect(db, server, gw)
    assert db.scalar(select(AuditLog).where(AuditLog.action == "server_health.reconnected")) is not None
    assert server_health.alerts(db) == [] and "is disconnected" not in admin_web.get("/admin/clients").text


def test_health_issues_follow_mt5_warning_levels():
    base = dict(connected=True, cpu_max=20, cpu_critical=30, memory_total=8192, memory_free=4000, memory_free_min=3900,
                memory_free_critical=200, disk_total=160000, disk_free=120000, disk_free_critical=8192, disk_read=300,
                disk_read_critical=30, disk_write=300, disk_write_critical=15, connections_max=18,
                connections_critical=10000, network_max=900, network_critical=102400)
    assert server_health.issues(base) == []
    found = server_health.issues({**base, "cpu_max": 49, "memory_free_min": 150, "connections_max": 9000,
                                  "connected": False})
    levels = [lvl for lvl, _ in found]
    assert levels == ["danger", "warning", "danger", "warning"]
    assert "MT5's warning level of 30%" in found[1][1]
    assert server_health.poll_issues({"seconds": 14.2, "interval": 10, "ms_per_call": 800, "errors": 0})[0][0] == "warning"


def test_unreadable_health_is_kept_as_an_error(db, gw, server, monkeypatch):
    def boom():
        raise MT5Error("Administrator connection refused")
    monkeypatch.setattr(gw, "get_server_health", boom)
    sample = server_health.collect(db, server, gw)
    assert "refused" in sample.error and server_health.report(db, server)["components"] == []


# ---------------------------------------------------------------- account activity
def old_users(db, gw, server, group, n, days):
    """Imported accounts whose last connection on MT5 was `days` ago."""
    logins = users(gw, group, n)
    for login in logins:
        gw.users[login].last_access = utcnow() - timedelta(days=days)
    mt5_sync.sync_trading_accounts(db, server, None, gw)
    return logins


def acc_of(db, login):
    db.expire_all()
    return db.scalar(select(TradingAccount).where(TradingAccount.login == login))


def test_activity_follows_connections_and_trades(db, gw, server):
    a, b = old_users(db, gw, server, "real\\standard", 2, 50)
    assert acc_of(db, a).last_access_at == gw.users[a].last_access == acc_of(db, a).last_activity_at  # from the import
    assert activity.refresh_access(db, server, gw) == 0  # nothing changed on MT5

    gw.touch(a)  # connects from a terminal
    assert activity.refresh_access(db, server, gw) == 1
    acc = acc_of(db, a)
    assert acc.last_access_at == gw.users[a].last_access and acc.last_activity_at == acc.last_access_at

    gw.balance[b] = Decimal("1000")
    gw.open_position(b, "EURUSD", BUY, Decimal("0.1"))
    sync.ingest_deals(db, [d for d in gw.deals if d.login == b], server_id=1)  # a pushed trade
    acc = acc_of(db, b)
    assert acc.last_trade_at is not None and acc.last_activity_at == acc.last_trade_at > acc.last_access_at

    activity.forget()
    gw.touch(b)
    assert activity.refresh_due(db, gw) == 1  # the worker's turn
    gw.touch(a)
    assert activity.refresh_due(db, gw) == 0  # not again before ACTIVITY_REFRESH_MINUTES


# ---------------------------------------------------------------- inactive demo accounts
def test_inactive_list_comes_from_crm_data(db, gw, server, admin):
    old, recent, busy, copy = old_users(db, gw, server, "demo\\standard", 4, 200)
    live = old_users(db, gw, server, "real\\standard", 1, 200)[0]
    gw.touch(recent)
    activity.refresh_access(db, server, gw)
    gw.balance[busy] = Decimal("1000")
    gw.open_position(busy, "EURUSD", BUY, Decimal("0.1"))
    sync.full_sync(db, gw)  # the CRM learns about the open position
    long_ago = utcnow() - timedelta(days=200)
    acc = acc_of(db, busy)
    acc.last_trade_at = acc.last_activity_at = long_ago  # the position was opened long ago
    db.commit()
    acc_of(db, copy).purpose = AccountPurpose.copy_master
    db.commit()

    calls = []
    for name in ("get_users_in_group", "get_positions", "get_orders", "get_groups"):
        monkeypatch_attr = getattr(gw, name)
        setattr(gw, name, (lambda f, n: lambda *a, **k: calls.append(n) or f(*a, **k))(monkeypatch_attr, name))
    found = demo_cleanup.scan(db, server)
    assert calls == []  # the list costs MT5 nothing
    assert [c["login"] for c in found["candidates"]] == [old] and found["total"] == 1
    assert found["skipped"] == {"open positions or orders": 1, "copy trading or PAMM": 1}
    assert [(g["group"], g["accounts"], g["inactive"]) for g in found["groups"]] == [("demo\\standard", 4, 3)]

    demo_cleanup.save_settings(db, admin, server, enabled=False, days=90, action="archive", groups="*")
    assert [c["login"] for c in demo_cleanup.scan(db, server)["candidates"]] == [old]  # live accounts never

    r = demo_cleanup.run(db, admin, server, gw)
    assert (r["done"], r["failed"], r["active_again"], r["remaining"]) == (1, 0, 0, 0)
    assert old in gw.archived and recent in gw.users and live in gw.users
    acc = acc_of(db, old)
    assert acc.status == AccountStatus.archived and not acc.on_server
    assert db.scalar(select(AuditLog).where(AuditLog.action == "demo_cleanup.archive")) is not None


def test_cleanup_rechecks_mt5_delete_and_schedule(db, gw, server, admin):
    a, b, c = old_users(db, gw, server, "demo\\ecn", 3, 40)
    with pytest.raises(ServiceError):
        demo_cleanup.save_settings(db, admin, server, enabled=True, days=3, action="delete", groups="demo*")
    demo_cleanup.save_settings(db, admin, server, enabled=True, days=30, action="delete", groups="demo\\ecn")

    r = demo_cleanup.run(db, admin, server, gw, logins=[a])  # only the ticked one
    assert r["done"] == 1 and a not in gw.users and a not in gw.archived and b in gw.users

    gw.touch(c)  # connected after the CRM's last refresh: MT5 is asked again before anything changes
    assert demo_cleanup.run_due(db, gw) == 0  # ran less than a day ago
    server.demo_cleanup = {**server.demo_cleanup, "last_run": (utcnow() - timedelta(days=2)).isoformat()}
    db.commit()
    assert demo_cleanup.run_due(db, gw) == 1 and b not in gw.users and c in gw.users
    result = server.demo_cleanup["last_result"]
    assert (result["by"], result["done"], result["active_again"]) == ("schedule", 1, 1)
    assert acc_of(db, c).last_access_at == gw.users[c].last_access


def test_mt5_demo_inactivity_is_set_from_the_crm(db, gw, server, admin):
    assert demo_cleanup.mt5_demo_groups(db, server, gw) == [{"name": "demo\\ecn", "days": 90},
                                                            {"name": "demo\\standard", "days": 90}]
    done, failed = demo_cleanup.set_mt5_inactivity(db, admin, server, ["demo\\ecn", "real\\standard"], 30, gw)
    assert done == ["demo\\ecn"] and failed == ["real\\standard: not a demo group"]
    assert gw.groups["demo\\ecn"].demo_inactivity == 30 and gw.groups["real\\standard"].demo_inactivity == 0
    with pytest.raises(ServiceError):
        demo_cleanup.set_mt5_inactivity(db, admin, server, ["demo\\ecn"], 5000, gw)


# ---------------------------------------------------------------- trader styles
def _deals(db, login, n, reason, hold_seconds=3600, start=None):
    start = start or utcnow() - timedelta(days=2)
    ticket = db.scalar(select(Deal.ticket).order_by(Deal.ticket.desc()).limit(1)) or 1000
    for i in range(n):
        t = start + timedelta(minutes=i * 90)
        for entry, at in ((0, t), (1, t + timedelta(seconds=hold_seconds))):
            ticket += 1
            db.add(Deal(server_id=1, ticket=ticket, login=login, time=at, time_msc=int(at.timestamp() * 1000),
                        action=0 if entry == 0 else 1, entry=entry, symbol="EURUSD", volume=Decimal("1"),
                        price=Decimal("1.1"), profit=Decimal("0"), commission=Decimal("0"), swap=Decimal("0"),
                        fee=Decimal("0"), position_id=login * 1000 + i, order=0, reason=reason, comment="",
                        contract_size=Decimal("100000"), tick_value=Decimal("1"), tick_size=Decimal("0.00001"),
                        digits=5, synced_at=utcnow()))
    db.commit()


def test_styles_from_deal_reasons_and_moves(db, gw, server, admin):
    ea, manual, mobile, few = imported(db, gw, server, "real\\standard", 4)
    _deals(db, ea, 20, 1, hold_seconds=30)  # an EA closing within seconds
    _deals(db, manual, 6, 0)
    _deals(db, manual, 6, 16)
    _deals(db, mobile, 8, 16)
    _deals(db, few, 2, 0)
    rows = {r["account"].login: r for r in trader_styles.profiles(db, server, days=30, min_deals=10)}
    assert {k: r["style"] for k, r in rows.items()} == {ea: "ea", manual: "manual", mobile: "manual", few: "few"}
    assert rows[mobile]["tags"] == ["mobile"]  # every mobile deal: the Mobile tag
    assert rows[ea]["high_frequency"] and rows[ea]["ea_pct"] == 100 and rows[ea]["positions"] == 20
    assert rows[manual]["platforms"] == {"Desktop": 50, "Mobile": 50} and not rows[manual]["high_frequency"]
    assert rows[manual]["avg_hold_minutes"] == 60.0 and all(r["suggest"] is None for r in rows.values())

    groups = {g.mt5_group: g for g in db.scalars(select(AccountGroup))}
    trader_styles.save_style_groups(db, admin, server, {"ea": groups["real\\ecn"].id, "manual": None})
    rows = {r["account"].login: r for r in trader_styles.profiles(db, server)}
    assert rows[ea]["suggest"].mt5_group == "real\\ecn" and rows[manual]["suggest"] is None
    with pytest.raises(ServiceError):
        trader_styles.save_style_groups(db, admin, server, {"ea": groups["demo\\ecn"].id})

    done, failed = trader_styles.move(db, gw, admin, [ea, 999999], groups["real\\ecn"].id)
    assert done == [ea] and len(failed) == 1 and gw.users[ea].group == "real\\ecn"
    acc = db.scalar(select(TradingAccount).where(TradingAccount.login == ea))
    assert (acc.group, acc.account_group_id) == ("real\\ecn", groups["real\\ecn"].id)
    assert trader_styles.profiles(db, server)[0]["suggest"] is None  # in its group now
    assert [r["account"].login for r in trader_styles.profiles(db, None)] == [
        r["account"].login for r in trader_styles.profiles(db, server)]  # all servers


def test_operations_pages_post(admin_web, db, gw):  # noqa: F811
    old = old_users(db, gw, db.get(MT5Server, 1), "demo\\standard", 1, 400)[0]
    r = form_post(admin_web, "/admin/server-health", "/admin/server-health/refresh", {"server_id": 1})
    assert "Read from MT5 just now" in r.text and "Main Trade Server" in r.text and "Access Server Europe" in r.text
    assert "Nothing needs attention." in admin_web.get("/admin/server-health?only=warnings").text

    menu = admin_web.get("/admin/configuration").text  # MT5 > Server tools in the menu, not on Configuration
    assert 'href="/admin/server-health"' in menu and 'href="/admin/inactive-accounts"' in menu
    assert "card-header\">Server tools" not in menu
    assert admin_web.get("/admin/demo-cleanup").url.path == "/admin/inactive-accounts"
    assert admin_web.get("/admin/mt5-servers/1/inactive").url.path == "/admin/inactive-accounts"

    page = "/admin/mt5-servers/1/inactive"
    assert str(old) in admin_web.get(page).text
    r = form_post(admin_web, page, page + "/mt5-days", {"group": "demo\\standard", "days": "45"})
    assert "inactive for 45 days in: demo\\standard" in r.text and gw.groups["demo\\standard"].demo_inactivity == 45
    r = form_post(admin_web, page, page + "/run", {"everything": "1", "action": "archive"})
    assert "Archived 1 demo account(s) on MT5" in r.text and old in gw.archived
    r = form_post(admin_web, page, page + "/settings", {"days": "60", "action": "archive", "groups": "demo*",
                                                        "enabled": "1"})
    assert "runs once a day" in r.text
    r = form_post(admin_web, "/admin/trader-styles", "/admin/trader-styles/move", {"target": "1"})
    assert "Tick the accounts to move first" in r.text


def test_mt5_menu_is_nested(admin_web):  # noqa: F811
    page = admin_web.get("/admin/symbols").text
    nav = page[page.find('<nav class="nav'):page.find("</nav>")]
    assert nav.count('class="nav-group"') == 4 and "Demo clean-up" not in nav and ">Servers<" not in nav
    assert ">Import from MT5<" in nav and ">Server tools<" in nav and 'href="/admin/configuration"' in nav
    assert "Sync status" not in nav and ">Trade sync<" in nav
    accounts = nav[nav.find(">Accounts<"):nav.find(">Trading setup<")]
    assert "Trading accounts" in accounts and "Trader styles" in accounts
    setup = nav[nav.find("bi-diagram-3") - 120:nav.find(">Import from MT5<")]
    assert "nav-group-toggle active" in setup and "Liquidity providers" in setup  # on Symbols: its group is active
    assert nav.count("nav-group-toggle active") == 1


# ---------------------------------------------------------------- restarts
def test_restart_a_component_and_all_gateways(admin_web, db, gw):  # noqa: F811
    page = "/admin/restart"
    assert 'href="/admin/restart"' in admin_web.get("/admin/configuration").text  # in the menu
    text = admin_web.get(page).text
    assert "Access Server Asia" in text and "type Main Trade Server" in text

    post = "/admin/mt5-servers/1/restart"
    r = form_post(admin_web, page, post, {"component_id": "4"})
    assert "Access Server Asia is restarting on MT5" in r.text and gw.restarts == [("component", 4)]
    r = form_post(admin_web, page, post, {"component_id": "1", "confirm_name": "main"})  # the main one: typed name
    assert "Type the main trade server" in r.text and len(gw.restarts) == 1
    form_post(admin_web, page, post, {"component_id": "1", "confirm_name": "Main Trade Server"})
    assert gw.restarts[-1] == ("component", 1)
    assert "has no component 99" in form_post(admin_web, page, post, {"component_id": "99"}).text
    assert db.scalar(select(AuditLog).where(AuditLog.action == "mt5_server.restart")
                     .order_by(AuditLog.id.desc())).details["component"] == "Main Trade Server"

    lp = "/admin/liquidity?server=1&kind=feed"
    assert "Restart all data feeds" in admin_web.get(lp).text
    r = form_post(admin_web, lp, "/admin/liquidity/restart", {"server_id": "1", "kind": "feed"})
    assert "every data feed is restarting" in r.text and gw.restarts[-1] == ("links", "feed")


def test_symbol_sessions_picker_markup(admin_web, db, gw):  # noqa: F811
    from app.models import Symbol
    from app.services import symbols

    symbols.sync_symbols(db, db.get(MT5Server, 1), None, gw)
    s = db.scalar(select(Symbol).where(Symbol.name == "EURUSD"))
    page = admin_web.get(f"/admin/symbols/{s.id}").text
    assert 'data-sessions="quote" data-day="1"' in page and 'data-fill="quote"' in page
