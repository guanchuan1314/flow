"""Import of MT5 groups (account groups) and MT5 users (trading accounts)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app import mt5
from app.db import utcnow
from app.models import (
    AccountGroup, Client, ClientNote, Deal, MT5Server, Role, ServerKind, Staff, TradingAccount,
)
from app.mt5 import BUY
from app.services import accounts, clients, mt5_sync, servers
from tests.conftest import ADMIN, login_staff
from tests.test_web import form_post, web_login


@pytest.fixture
def server(db):
    return db.get(MT5Server, 1)


def _groups(db):
    return {t.mt5_group: t for t in db.scalars(select(AccountGroup))}


# ---------------------------------------------------------------- account groups
def test_groups_become_account_groups(db, gw, server, account_groups):
    summary = mt5_sync.sync_account_groups(db, server)
    groups = _groups(db)
    # the simulator has real\standard, real\ecn, real\pro, demo\standard, demo\ecn, managers\administrators
    assert set(groups) == {"real\\standard", "real\\ecn", "real\\pro", "demo\\standard", "demo\\ecn"}
    assert "managers\\administrators" not in groups  # manager groups are never offered
    assert sorted(summary["created"]) == sorted([groups[g].name for g in ("real\\ecn", "real\\pro", "demo\\ecn")])
    new = groups["real\\ecn"]
    assert (new.source, new.enabled, new.is_demo, new.name) == ("mt5", False, False, "Ecn (real)")
    assert groups["demo\\ecn"].is_demo and groups["demo\\ecn"].default_leverage == 500
    # the existing CRM groups were matched, not duplicated or disabled
    assert groups["real\\standard"].id == account_groups["live"] and groups["real\\standard"].enabled
    assert mt5_sync.sync_account_groups(db, server)["created"] == []  # idempotent


def test_group_removed_on_mt5_is_flagged(db, gw, server, account_groups):
    mt5_sync.sync_account_groups(db, server)
    del gw.groups["real\\pro"]
    summary = mt5_sync.sync_account_groups(db, server)
    db.expire_all()
    pro = _groups(db)["real\\pro"]
    assert summary["missing"] == [pro.name] and not pro.on_server and not pro.enabled


# ---------------------------------------------------------------- trading accounts
def test_mt5_users_become_trading_accounts(db, gw, server, account_groups):
    known = clients.register_client(db, None, email="known@example.com", first_name="Kim", last_name="Known",
                                    password="Client-Pass1")
    crm_acc = accounts.open_account(db, gw, None, known, account_groups["demo"])["account"]  # created by the CRM
    # accounts created directly in MT5 (manager terminal, another system...)
    a = gw.create_user(group="real\\standard", leverage=200, name="Kim Known", email="KNOWN@example.com",
                       phone="", country="GB", master_password="x", investor_password="y")
    b = gw.create_user(group="real\\ecn", leverage=100, name="Nora New", email="nora@example.com",
                       phone="+44 1", country="United Kingdom", master_password="x", investor_password="y")
    c = gw.create_user(group="demo\\standard", leverage=500, name="No Email", email="", phone="", country="",
                       master_password="x", investor_password="y")
    staff_login = gw.create_user(group="managers\\administrators", leverage=100, name="Admin", email="a@b.com",
                                 phone="", country="", master_password="x", investor_password="y")
    gw.balance_operation(b, Decimal("750"), "initial deposit")

    s = mt5_sync.sync_trading_accounts(db, server)
    assert (s["seen"], s["created"], s["clients_created"], s["clients_linked"]) == (4, 3, 2, 1)
    accs = {x.login: x for x in db.scalars(select(TradingAccount))}
    assert staff_login not in accs  # excluded group
    assert accs[a].client_id == known.id and accs[a].source == "mt5"  # matched by email, case-insensitive
    nora = db.get(Client, accs[b].client_id)
    assert (nora.first_name, nora.last_name, nora.lead_source, nora.country) == ("Nora", "New", "mt5-import", None)
    assert accs[b].balance == Decimal("750.00")  # balances imported straight away
    assert accs[b].account_group.mt5_group == "real\\ecn" and accs[b].leverage == 100
    note = db.scalar(select(ClientNote.body).where(ClientNote.client_id == nora.id))
    assert "MT5 country: United Kingdom" in note
    nomail = db.get(Client, accs[c].client_id)
    assert nomail.email.endswith("@import.invalid") and accs[c].is_demo
    assert accs[crm_acc.login].source == "crm"  # CRM accounts are updated, never duplicated

    again = mt5_sync.sync_trading_accounts(db, server)
    assert (again["created"], again["clients_created"]) == (0, 0)  # idempotent


def test_changes_and_deletions_on_mt5_are_applied(db, gw, server, account_groups):
    login = gw.create_user(group="real\\standard", leverage=100, name="Ann Change", email="ann@example.com",
                           phone="", country="", master_password="x", investor_password="y")
    mt5_sync.sync_trading_accounts(db, server)
    gw.update_user(login, leverage=500, group="real\\ecn", trading_enabled=False)
    s = mt5_sync.sync_trading_accounts(db, server)
    db.expire_all()
    acc = db.scalar(select(TradingAccount).where(TradingAccount.login == login))
    assert s["updated"] == 1 and (acc.leverage, acc.group, acc.trading_enabled) == (500, "real\\ecn", False)
    del gw.users[login]  # deleted in MT5
    assert mt5_sync.sync_trading_accounts(db, server)["missing"] == 1
    db.expire_all()
    assert db.scalar(select(TradingAccount.on_server).where(TradingAccount.login == login)) is False


def test_imported_account_gets_its_full_deal_history(db, gw, server, account_groups):
    from app.services import sync

    login = gw.create_user(group="real\\standard", leverage=100, name="Old Trader", email="old@example.com",
                           phone="", country="", master_password="x", investor_password="y")
    gw.users[login].registration = utcnow() - timedelta(days=400)
    gw.balance_operation(login, Decimal("1000"), "deposit")
    for deal in gw.deals:  # pretend the history is a year old
        deal.time -= timedelta(days=300)
    mt5_sync.sync_trading_accounts(db, server)
    sync.sync_deals(db, gw)
    assert db.scalar(select(func.count()).select_from(Deal).where(Deal.login == login)) == 1


# ---------------------------------------------------------------- admin & schedule
def test_admin_sync_buttons(api, admin_h, db, gw, account_groups):
    gw.create_user(group="real\\pro", leverage=100, name="Pat Pro", email="pat@example.com", phone="", country="",
                   master_password="x", investor_password="y")
    web_login(api, *ADMIN)
    r = form_post(api, "/admin/accounts/account-groups", "/admin/accounts/account-groups/sync", {})
    assert "MT5 groups" in r.text and "disabled until you review" in r.text and "from MT5" in r.text
    r = form_post(api, "/admin/accounts/trading-accounts", "/admin/accounts/trading-accounts/sync", {})
    assert "1 imported" in r.text and "imported</span>" in r.text
    r = form_post(api, "/admin/import/trading-accounts", "/admin/import/trading-accounts/sync", {})
    assert "0 imported" in r.text and "Last import:" in r.text


def test_sync_requires_permission(api, admin_h, db, gw):
    role = db.scalar(select(Role).where(Role.name == "Sales"))
    api.post("/api/staff", json={"email": "s@b.com", "full_name": "S", "password": "Sales-Pass1",
                                 "role_ids": [role.id]}, headers=admin_h)
    web_login(api, "s@b.com", "Sales-Pass1")
    assert form_post(api, "/admin", "/admin/accounts/trading-accounts/sync", {}).status_code == 403
    assert form_post(api, "/admin", "/admin/accounts/account-groups/sync", {}).status_code == 403


def test_worker_imports_on_schedule(db, gw, server, account_groups):
    from app.workers.runner import Worker

    gw.create_user(group="real\\standard", leverage=100, name="Sam Sched", email="sam@example.com", phone="",
                   country="", master_password="x", investor_password="y")
    worker = Worker(gw)
    worker._scheduled_import(db)
    assert db.scalar(select(func.count()).select_from(TradingAccount)) == 1
    db.expire_all()
    assert db.get(MT5Server, 1).accounts_synced_at is not None
    gw.create_user(group="real\\standard", leverage=100, name="Too Soon", email="soon@example.com", phone="",
                   country="", master_password="x", investor_password="y")
    worker._scheduled_import(db)  # not due again yet
    assert db.scalar(select(func.count()).select_from(TradingAccount)) == 1


@pytest.mark.parametrize("name,first,last,expected", [
    ("Tan Chee Hang ", "Tan Chee Hang ", "", ("Tan", "Chee Hang")),         # full name only in FirstName
    ("Dev Tester 001", "Dev Tester 001", "Tester 001", ("Dev", "Tester 001")),  # surname repeated
    ("Ann Lee", "Ann", "Lee", ("Ann", "Lee")),                              # properly filled
    ("Madonna", "", "", ("Madonna", "123")),                                # single word: login as surname
    ("", "", "", ("MT5", "123")),
])
def test_name_splitting(name, first, last, expected):
    from app.mt5.gateway import MT5User

    user = MT5User(login=123, group="real", leverage=100, name=name, first_name=first, last_name=last)
    assert mt5_sync.split_name(user) == expected


def test_empty_group_is_not_a_failure(db, gw, server, account_groups):
    s = mt5_sync.sync_trading_accounts(db, server)  # simulator groups exist but most are empty
    assert s["failed_groups"] == []


# ---------------------------------------------------------------- background syncs and their history
def test_sync_runs_in_the_background_and_is_kept_in_the_history(api, admin, db, gw, server):
    import threading

    from app.models import SyncRun
    from app.services import sync_runs
    from app.web import syncs

    web_login(api, *ADMIN)
    r = form_post(api, "/admin/import/account-groups", "/admin/import/account-groups/sync", {})
    assert "MT5 groups" in r.text  # quick: the result shows right away
    run = db.scalar(select(SyncRun))
    admin = db.scalar(select(Staff).where(Staff.email == ADMIN[0]))
    assert (run.kind, run.trigger, run.status, run.created_by_id, run.server_id) == \
        ("account_groups", "manual", "done", admin.id, 1)

    # a slow one carries on in the background, and the same sync isn't started twice meanwhile
    release = threading.Event()
    slow = gw.get_groups
    gw.get_groups = lambda *a, **kw: (release.wait(10), slow(*a, **kw))[1]
    syncs.WAIT_SECONDS = 0.2
    try:
        r = form_post(api, "/admin/import/trading-accounts", "/admin/import/trading-accounts/sync", {})
        assert "started in the background" in r.text and "sync-panel" in r.text and "progress-bar" in r.text
        r = form_post(api, "/admin/import/trading-accounts", "/admin/import/trading-accounts/sync", {})
        assert "already running" in r.text
        page = api.get("/admin/import/history").text
        assert 'hx-trigger="every 2s"' in page and "progress-bar" in page
    finally:
        release.set()
        gw.get_groups = slow
    sync_runs.pool().submit(lambda: None).result()  # the pool has moved on
    for _ in range(100):
        db.expire_all()
        if all(not x.active for x in db.scalars(select(SyncRun))):
            break
        threading.Event().wait(0.05)
    runs = db.scalars(select(SyncRun).order_by(SyncRun.id)).all()
    assert [x.kind for x in runs] == ["account_groups", "trading_accounts"] and runs[1].status == "done"

    # a scheduled one, and one cut short by a restart
    sync_runs.record(db, "trading_accounts", server, lambda: mt5_sync.sync_trading_accounts(db, server, None, gw))
    db.add(SyncRun(kind="symbols", server_id=1, trigger="manual", status="running", created_at=utcnow(), message=""))
    db.commit()
    assert sync_runs.recover_interrupted(db, "manual") == 1

    page = api.get("/admin/import/history").text
    assert "Schedule" in page and admin.full_name in page and "Interrupted: the CRM restarted" in page
    assert "Trading accounts" in page and "MT5 accounts" in page and 'data-details="sync-' in page
    assert "Schedule" not in api.get("/admin/import/history?trigger=manual").text.split("<tbody>")[1]
    assert "Interrupted" in api.get("/admin/import/history?status=failed").text
