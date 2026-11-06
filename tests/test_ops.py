"""Orders sync, background history sync tasks, server removal, IP access rules, account groups naming."""

from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.core.errors import ServiceError
from app.db import utcnow
from app.main import create_app
from app.models import (
    AccountGroup, Deal, IpRule, MT5Server, Order, PaymentKind, PaymentRequest, ServerKind, SyncJob, TradingAccount,
)
from app.mt5 import BUY
from app.services import accounts, ip_rules, payments, servers, sync, sync_jobs
from tests.conftest import ADMIN
from tests.test_web import admin_web, approve_kyc_direct, csrf_of, form_post, web_login  # noqa: F401


@pytest.fixture
def trader(db, gw, account_groups):
    from app.services import clients
    c = clients.register_client(db, None, email="t@x.com", first_name="Tia", last_name="Trader", password="Client-Pass1")
    approve_kyc_direct(db, c.id)
    login = accounts.open_account(db, gw, None, c, account_groups["live"])["account"].login
    gw.balance_operation(login, Decimal("5000"), "funding")
    return login


# ---------------------------------------------------------------- orders
def test_orders_are_synced_open_and_history(db, gw, trader):
    gw.open_position(trader, "EURUSD", BUY, Decimal("1"))  # market order: filled at once
    ticket = gw.place_order(trader, "EURUSD", 2, Decimal("2"), Decimal("1.05000"))  # buy limit, pending
    totals = sync.full_sync(db, gw)
    assert totals["open_orders"] == 1
    pending = db.get(Order, (1, ticket))
    assert pending.is_open and pending.type == 2 and pending.volume_initial == Decimal("2")
    filled = db.scalar(select(Order).where(Order.is_open.is_(False)))
    assert filled.state == 4 and filled.position_id  # filled market order, from the history

    gw.cancel_order(ticket)
    sync.full_sync(db, gw)
    db.refresh(pending)
    assert not pending.is_open and pending.state == 2  # cancelled


def test_orders_page(admin_web, db, gw, trader):
    gw.place_order(trader, "XAUUSD", 3, Decimal("1"), Decimal("2500"))
    sync.full_sync(db, gw)
    r = admin_web.get("/admin/trades/orders/data")
    assert r.status_code == 200 and "Sell limit" in r.text
    assert "No orders in the history yet" in admin_web.get("/admin/trades/orders/data?status=history").text


# ---------------------------------------------------------------- background history sync
def test_sync_task_restores_missing_deals(db, gw, trader):
    for _ in range(3):
        gw.open_position(trader, "EURUSD", BUY, Decimal("1"))
    sync.full_sync(db, gw)
    stored = db.scalar(select(func.count()).select_from(Deal))
    db.execute(delete(Deal).where(Deal.action == 0))  # the CRM lost the trades
    db.execute(delete(Order))
    db.commit()

    job = sync_jobs.create_job(db, None, server_id=None, logins=[], date_from=utcnow() - timedelta(days=70),
                               date_to=utcnow() + timedelta(minutes=1))
    assert job.status == "queued"
    sync_jobs.run_next(db)
    db.refresh(job)
    assert job.status == "done" and job.done == job.total and job.percent == 100
    assert job.deals_added == 3 and job.orders_added == 3 and job.errors == 0
    assert db.scalar(select(func.count()).select_from(Deal)) == stored
    assert sync_jobs.run_next(db) is None  # nothing left to do


def test_sync_task_cancel_and_validation(db, gw, trader):
    with pytest.raises(ServiceError, match="Not CRM accounts"):
        sync_jobs.create_job(db, None, server_id=None, logins=[999], date_from=utcnow() - timedelta(days=1),
                             date_to=utcnow())
    job = sync_jobs.create_job(db, None, server_id=1, logins=[trader], date_from=utcnow() - timedelta(days=1),
                               date_to=utcnow())
    sync_jobs.cancel_job(db, None, job)
    assert job.status == "cancelled" and sync_jobs.run_next(db) is None

    stale = sync_jobs.create_job(db, None, server_id=None, logins=[], date_from=utcnow() - timedelta(days=1),
                                 date_to=utcnow())
    stale.status, stale.heartbeat_at = "running", utcnow() - timedelta(minutes=10)  # its worker died
    db.commit()
    assert sync_jobs.recover_interrupted(db) == 1 and stale.status == "queued"


def test_sync_task_screen_shows_progress(admin_web, db, gw, trader):
    r = form_post(admin_web, "/admin/trades/deals/sync", "/admin/trades/sync/jobs",
                  {"date_from": (utcnow() - timedelta(days=5)).date().isoformat(), "logins": str(trader),
                   "include_orders": "on"})
    assert "queued; progress is shown below" in r.text
    part = admin_web.get("/admin/trades/sync/jobs")
    assert 'hx-trigger="every 2s"' in part.text  # polls while the task is waiting / running
    sync_jobs.run_next(db)
    part = admin_web.get("/admin/trades/sync/jobs")
    assert "Done" in part.text and "hx-trigger" not in part.text


# ---------------------------------------------------------------- removing the server
@pytest.fixture
def second_server(db, gw):
    """The CRM's server (the only one) with an account, a position and deals on it."""
    from app.services import clients
    srv = db.get(MT5Server, 1)
    srv.name = "Old server"
    db.add(AccountGroup(name="Old std", server_id=srv.id, mt5_group="real\\standard", leverage_options=[100]))
    db.commit()
    group_id = db.scalar(select(AccountGroup.id).where(AccountGroup.server_id == srv.id))
    c = clients.register_client(db, None, email="o@x.com", first_name="O", last_name="Ld", password="Client-Pass1")
    approve_kyc_direct(db, c.id)
    login = accounts.open_account(db, gw, None, c, group_id)["account"].login
    gw.open_position(login, "EURUSD", BUY, Decimal("1"))
    sync.full_sync(db, gw, server_id=srv.id)
    return srv, login, c


def test_deleting_a_server_never_touches_mt5(admin_web, db, gw, second_server):
    srv, login, client = second_server
    sid = srv.id
    assert servers.crm_data(db, srv)["trading accounts"] == 1
    with pytest.raises(ServiceError, match="Disable it, or delete it together"):
        servers.delete_server(db, None, srv)
    req = payments.create_request(db, client, client, PaymentKind.deposit, 100, trading_login=login)
    with pytest.raises(ServiceError, match="pending deposits"):
        servers.delete_server(db, None, srv, purge=True)
    db.delete(db.get(PaymentRequest, req.id))
    db.commit()

    r = form_post(admin_web, f"/admin/configuration/servers/{srv.id}", f"/admin/configuration/servers/{srv.id}/delete",
                  {"purge": "on", "confirm_name": "wrong"})
    assert "Type the server name" in r.text
    r = form_post(admin_web, f"/admin/configuration/servers/{srv.id}", f"/admin/configuration/servers/{srv.id}/delete",
                  {"purge": "on", "confirm_name": "Old server"})
    assert "Nothing was changed on the MT5 server" in r.text
    db.expire_all()
    assert db.get(MT5Server, sid) is None and db.scalar(select(TradingAccount).where(TradingAccount.login == login)) is None
    assert db.scalar(select(func.count()).select_from(Deal).where(Deal.login == login)) == 0
    assert login in gw.users and gw.get_positions([login])  # still on MT5, untouched


# ---------------------------------------------------------------- IP access rules
def test_ip_rules_block_and_allow(db):
    ip_rules.create_rule(db, None, "10.0.0.5", network="198.51.100.7", action="block", scope="portal")
    assert ip_rules.check("198.51.100.7", "/portal/login") == "blocked"
    assert ip_rules.check("198.51.100.7", "/admin/login") is None  # other scope
    assert ip_rules.check("198.51.100.7", "/static/app.css") is None  # never filtered

    with pytest.raises(ServiceError, match="lock you out"):  # an admin allow list without my own address
        ip_rules.create_rule(db, None, "10.0.0.5", network="192.0.2.0/24", action="allow", scope="admin")
    ip_rules.create_rule(db, None, "10.0.0.5", network="10.0.0.0/8", action="allow", scope="admin")
    assert ip_rules.check("10.1.2.3", "/admin") is None
    assert ip_rules.check("203.0.113.9", "/admin") == "not on the allow list"
    assert ip_rules.check("203.0.113.9", "/api/clients") == "not on the allow list"  # staff API too
    assert ip_rules.check("203.0.113.9", "/") is None  # the website has no allow list

    expired = ip_rules.create_rule(db, None, "10.0.0.5", network="203.0.113.50", action="block",
                                   expires_at=utcnow() - timedelta(minutes=1))
    assert ip_rules.check("203.0.113.50", "/") is None
    assert ip_rules.expire(db) == 1 and not expired.enabled


def test_blocked_address_gets_403(gw, db):
    ip_rules.create_rule(db, None, None, network="198.51.100.0/24", action="block")
    with TestClient(create_app(), client=("198.51.100.20", 5000)) as c:
        r = c.get("/portal/login")
        assert r.status_code == 403 and "Access denied" in r.text
        assert c.get("/api/portal/me").status_code == 403
    with TestClient(create_app(), client=("192.0.2.1", 5000)) as c:
        assert c.get("/portal/login").status_code == 200


def test_ip_rule_is_mirrored_to_mt5(db, gw):
    rule = ip_rules.create_rule(db, None, None, network="203.0.113.0/30", action="block", description="scraper",
                                apply_mt5=True)
    fw = gw.get_firewall()
    assert [(f.from_ip, f.to_ip, f.action) for f in fw] == [("203.0.113.0", "203.0.113.3", "block")]
    assert fw[0].comment == f"{rule.mt5_tag} scraper" and rule.mt5_state["1"]["ok"]

    ip_rules.update_rule(db, None, None, rule, {"network": "203.0.113.9"})  # changed on MT5 too
    assert [(f.from_ip, f.to_ip) for f in gw.get_firewall()] == [("203.0.113.9", "203.0.113.9")]
    ip_rules.update_rule(db, None, None, rule, {"apply_mt5": False})  # no longer on MT5
    assert gw.get_firewall() == [] and rule.mt5_state == {}
    ip_rules.update_rule(db, None, None, rule, {"apply_mt5": True})
    ip_rules.delete_rule(db, None, None, rule)  # deleted on MT5 as well
    assert gw.get_firewall() == [] and db.scalar(select(IpRule)) is None


def test_ip_rules_admin_screens(admin_web, db, gw):
    r = form_post(admin_web, "/admin/security/ip-rules", "/admin/security/ip-rules",
                  {"network": "198.51.100.66", "action": "block", "scope": "all", "enabled": "on", "apply_mt5": "on",
                   "description": "bot"})
    assert "Applied on 1 MT5 server" in r.text
    rule = db.scalar(select(IpRule))
    assert "from CRM" in admin_web.get("/admin/security/ip-rules?firewall=1").text
    r = form_post(admin_web, f"/admin/security/ip-rules/{rule.id}", f"/admin/security/ip-rules/{rule.id}/delete", {})
    assert "IP rule deleted" in r.text and gw.get_firewall() == []


# ---------------------------------------------------------------- naming
def test_account_groups_naming(admin_web, account_groups):
    r = admin_web.get("/admin/accounts/account-groups")
    assert r.status_code == 200 and "Account groups" in r.text and "Account types" not in r.text
    assert admin_web.get("/admin/account-types").status_code == 404
