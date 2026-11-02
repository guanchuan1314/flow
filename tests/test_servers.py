"""The CRM's one MT5 server, managed from the admin dashboard; live and demo accounts both live on it.

These tests use the real gateway registry (no pinned test gateway), as the running CRM does.
"""

import importlib.util
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import mt5
from app.core.errors import Conflict, ServiceError
from app.core.security import decrypt_secret
from app.models import AccountGroup, MT5Server, Role, ServerKind
from app.services import accounts, servers, sync
from tests.conftest import ADMIN, login_staff
from tests.test_web import form_post, web_login


@pytest.fixture
def web():
    """Test client WITHOUT a pinned gateway: the real gateway registry is used."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def no_server(db):
    """The CRM before its MT5 server is set up."""
    db.delete(db.get(MT5Server, 1))
    db.commit()


@pytest.fixture
def groups(db):
    """A live and a demo account group, both on the one server."""
    live = AccountGroup(name="Standard", server_id=1, mt5_group="real\\standard", leverage_options=[100, 500])
    demo = AccountGroup(name="Practice", server_id=1, mt5_group="demo\\standard", is_demo=True, leverage_options=[100])
    db.add_all([live, demo])
    db.commit()
    return {"live": live.id, "demo": demo.id}


def _client(db, email, kyc=True):
    from app.models import KycStatus
    from app.services import clients

    c = clients.register_client(db, None, email=email, first_name="T", last_name="Est", password="Client-Pass1")
    if kyc:
        c.kyc_status = KycStatus.approved
        db.commit()
    return c


def _update(db, server, **changes):
    values = dict(name=server.name, kind=server.kind, address=server.address, manager_login=server.manager_login,
                  password=None, terminal_name=server.terminal_name, timeout_ms=server.timeout_ms,
                  enabled=server.enabled, notes=server.notes or "")
    return servers.update_server(db, None, server, **{**values, **changes})


# ---------------------------------------------------------------- admin screens
def test_admin_adds_edits_and_tests_the_server(web, admin, db, no_server):
    web_login(web, *ADMIN)
    assert web.get("/admin/configuration/servers").url.path == "/admin/configuration"  # the old list is gone
    assert "Save &amp; test connection" in web.get("/admin/configuration").text  # not set up yet: add it here
    r = form_post(web, "/admin/configuration/servers/new", "/admin/configuration/servers/new", {
        "name": "Main", "kind": "simulator", "terminal_name": "Broker-Server", "timeout_seconds": "10", "enabled": "on"})
    assert "saved. Connection test: Connected successfully" in r.text
    server = db.scalar(select(MT5Server).where(MT5Server.name == "Main"))
    assert server.last_status == "ok" and server.timeout_ms == 10000 and server.is_price_feed and not server.is_demo
    config = web.get("/admin/configuration").text
    assert "Broker-Server" in config and "Test connection" in config

    r = form_post(web, f"/admin/configuration/servers/{server.id}", f"/admin/configuration/servers/{server.id}", {
        "name": "Main 2", "kind": "simulator", "terminal_name": "Broker-Server2", "timeout_seconds": "10",
        "enabled": "on"})
    assert "Server settings saved" in r.text
    db.expire_all()
    assert db.get(MT5Server, server.id).terminal_name == "Broker-Server2"
    r = form_post(web, "/admin/configuration", f"/admin/configuration/servers/{server.id}/test", {})
    assert "Connected successfully" in r.text


def test_a_second_server_is_refused(web, admin, db):
    with pytest.raises(Conflict, match="one MT5 server"):
        servers.create_server(db, None, name="Another", kind=ServerKind.simulator)
    web_login(web, *ADMIN)
    r = web.get("/admin/configuration/servers/new")  # the form sends you to the existing server
    assert r.url.path == "/admin/configuration" and "instead of adding another" in r.text


def test_manager_password_is_encrypted_and_never_shown(web, admin, db, no_server):
    web_login(web, *ADMIN)
    form_post(web, "/admin/configuration/servers/new", "/admin/configuration/servers/new", {
        "name": "Real", "kind": "manager", "address": "127.0.0.1:1", "manager_login": "1001",
        "password": "Sup3r-Secret!", "timeout_seconds": "2", "enabled": "on"})
    server = db.scalar(select(MT5Server).where(MT5Server.name == "Real"))
    assert server.manager_password_enc and "Sup3r-Secret!" not in server.manager_password_enc
    assert decrypt_secret(server.manager_password_enc) == "Sup3r-Secret!"
    page = web.get(f"/admin/configuration/servers/{server.id}").text
    assert "Sup3r-Secret!" not in page and "unchanged" in page
    # saving without a password keeps the stored one
    form_post(web, f"/admin/configuration/servers/{server.id}", f"/admin/configuration/servers/{server.id}", {
        "name": "Real", "kind": "manager", "address": "127.0.0.1:1", "manager_login": "1001", "password": "",
        "timeout_seconds": "2", "enabled": "on"})
    db.expire_all()
    assert decrypt_secret(db.get(MT5Server, server.id).manager_password_enc) == "Sup3r-Secret!"


@pytest.mark.skipif(importlib.util.find_spec("MT5Manager") is None, reason="MT5Manager not installed")
def test_unreachable_real_server_reports_the_error(web, admin, db, no_server):
    web_login(web, *ADMIN)
    r = form_post(web, "/admin/configuration/servers/new", "/admin/configuration/servers/new", {
        "name": "Offline", "kind": "manager", "address": "127.0.0.1:1", "manager_login": "1001", "password": "x",
        "timeout_seconds": "2", "enabled": "on"})
    assert "Connection test:" in r.text and "Network error" in r.text
    assert db.scalar(select(MT5Server.last_status).where(MT5Server.name == "Offline")) == "error"


def test_server_form_validation(web, admin, db, no_server):
    web_login(web, *ADMIN)
    r = form_post(web, "/admin/configuration/servers/new", "/admin/configuration/servers/new", {
        "name": "Bad", "kind": "manager", "address": "not a host!", "manager_login": "1", "password": "x"})
    assert "Address must look like" in r.text
    r = form_post(web, "/admin/configuration/servers/new", "/admin/configuration/servers/new", {"name": "", "kind": "simulator"})
    assert "Name is required" in r.text


def test_only_authorised_staff_manage_servers(web, admin, db):
    admin_h = login_staff(web, *ADMIN)
    role = db.scalar(select(Role).where(Role.name == "Manager"))
    web.post("/api/staff", json={"email": "m@b.com", "full_name": "M", "password": "Manager-Pass1",
                                 "role_ids": [role.id]}, headers=admin_h)
    web_login(web, "m@b.com", "Manager-Pass1")
    assert web.get("/admin/configuration").status_code == 403


# ---------------------------------------------------------------- live and demo on one server
def test_live_and_demo_accounts_open_on_the_one_server(web, db, groups):
    c = _client(db, "both@x.com")
    live = accounts.open_account(db, mt5.for_account_group(db, groups["live"]), None, c, groups["live"])["account"]
    demo = accounts.open_account(db, mt5.for_account_group(db, groups["demo"]), None, c, groups["demo"])["account"]
    assert (live.server_id, demo.server_id) == (1, 1) and (live.is_demo, demo.is_demo) == (False, True)
    gw = mt5.get_gateway(1)
    assert gw.users[live.login].group == "real\\standard" and gw.users[demo.login].group == "demo\\standard"
    assert gw.balance[demo.login] == Decimal("10000")
    mt5.routed().balance_operation(live.login, Decimal("250"), "test")
    assert gw.balance[live.login] == Decimal("250")
    accounts.change_leverage(db, mt5.routed(), None, live, 500)
    assert gw.users[live.login].leverage == 500


def test_new_account_group_can_create_its_group_on_mt5(db):
    accounts.create_account_group(db, None, mt5_template="demo\\standard", name="Demo Cent", mt5_group="demo\\cent",
                                 server_id=1, is_demo=True, currency="EUR", leverage_options=[100],
                                 default_leverage=100)
    assert [(g.name, g.currency) for g in mt5.get_gateway(1).get_groups("demo\\cent")] == [("demo\\cent", "EUR")]

    with pytest.raises(ServiceError, match="not found"):  # MT5 refuses: nothing saved in the CRM
        accounts.create_account_group(db, None, mt5_template="demo\\nope", name="Bad", mt5_group="demo\\bad",
                                     server_id=1, is_demo=True, leverage_options=[100], default_leverage=100)
    db.rollback()
    assert db.scalar(select(AccountGroup).where(AccountGroup.name == "Bad")) is None
    with pytest.raises(ServiceError, match="already exists"):
        accounts.create_account_group(db, None, mt5_template="demo\\standard", name="Twin", mt5_group="demo\\ecn",
                                     server_id=1, is_demo=True, leverage_options=[100], default_leverage=100)


def test_account_group_changes_are_written_to_the_mt5_group(db, groups):
    gw = mt5.get_gateway(1)
    ag = accounts.create_account_group(db, None, mt5_template="real\\standard", name="Pro+", mt5_group="real\\proplus",
                                      server_id=1, leverage_options=[100], default_leverage=100, stop_out=Decimal("30"))
    assert (ag.margin_call, ag.stop_out) == (Decimal("100"), Decimal("30"))  # blank -> group's value; set -> MT5
    assert gw.get_group("real\\proplus").stop_out == Decimal("30")

    accounts.update_account_group(db, None, ag, {"margin_call": Decimal("120"), "min_deposit": Decimal("50")})
    g = gw.get_group("real\\proplus")
    assert (g.margin_call, g.stop_out) == (Decimal("120"), Decimal("30"))

    with pytest.raises(ServiceError, match="not found"):  # MT5 refuses -> the CRM keeps its old values
        accounts.update_account_group(db, None, ag, {"mt5_group": "real\\missing", "margin_call": Decimal("90")})
    db.refresh(ag)
    assert (ag.mt5_group, ag.margin_call) == ("real\\proplus", Decimal("120"))

    # a demo type also mirrors MT5's demo leverage and deposit
    demo = accounts.create_account_group(db, None, name="Demo 2", mt5_group="demo\\ecn", server_id=1, is_demo=True,
                                        leverage_options=[100, 200], default_leverage=200,
                                        demo_initial_balance=Decimal("5000"))
    g = gw.get_group("demo\\ecn")
    assert (g.demo_leverage, g.demo_deposit) == (200, Decimal("5000"))
    assert demo.id

    with pytest.raises(ServiceError, match="doesn't exist"):  # existing group required unless creating it
        accounts.create_account_group(db, None, name="Ghost", mt5_group="real\\ghost", server_id=1,
                                     leverage_options=[100], default_leverage=100)


def test_changes_made_in_mt5_show_up_in_the_crm(db, groups):
    from app.services import mt5_sync
    ag = db.get(AccountGroup, groups["live"])
    mt5.get_gateway(1).update_group("real\\standard", margin_call=Decimal("80"), stop_out=Decimal("40"))
    assert mt5_sync.refresh_group(db, ag) is None  # what the edit page does
    assert (ag.margin_call, ag.stop_out) == (Decimal("80"), Decimal("40"))
    mt5.get_gateway(1).update_group("real\\standard", stop_out=Decimal("20"))
    summary = mt5_sync.sync_account_groups(db, db.get(MT5Server, 1))  # the scheduled import
    assert ag.name in summary["updated"] and ag.stop_out == Decimal("20")


def test_imported_demo_groups_are_demo_on_the_live_server(db):
    from app.services import mt5_sync
    mt5_sync.sync_account_groups(db, db.get(MT5Server, 1))
    kinds = {g.mt5_group: g.is_demo for g in db.scalars(select(AccountGroup))}
    assert kinds["demo\\standard"] is True and kinds["real\\standard"] is False


def test_deleting_an_account_group_deletes_its_mt5_group(db, groups):
    gw = mt5.get_gateway(1)
    ag = accounts.create_account_group(db, None, mt5_template="real\\standard", name="Temp", mt5_group="real\\temp",
                                      server_id=1, leverage_options=[100], default_leverage=100)
    login = gw.create_user(group="real\\temp", leverage=100, name="Someone", email="", phone="", country="",
                           master_password="Pw-123456", investor_password="Pw-654321")
    with pytest.raises(ServiceError, match=r"1 account\(s\) on MT5 that are not in the CRM"):  # opened on MT5
        accounts.delete_account_group(db, None, ag)
    gw.update_user(login, group="real\\ecn")
    accounts.delete_account_group(db, None, ag)
    assert gw.get_group("real\\temp") is None
    assert db.scalar(select(AccountGroup).where(AccountGroup.name == "Temp")) is None

    c = _client(db, "used@x.com")  # a type with CRM accounts is never deleted
    accounts.open_account(db, mt5.for_account_group(db, groups["live"]), None, c, groups["live"])
    with pytest.raises(ServiceError, match="Move them to another type"):
        accounts.delete_account_group(db, None, db.get(AccountGroup, groups["live"]))
    assert gw.get_group("real\\standard") is not None


def test_account_group_form_creates_the_group_on_mt5(web, admin, db):
    web_login(web, *ADMIN)
    assert 'name="server_id" value="1"' in web.get("/admin/accounts/account-groups/new").text  # no server to choose
    form = {"name": "Raw", "mt5_group": "real\\raw", "server_id": "1", "currency": "USD",
            "leverage_options": "100, 200", "default_leverage": "100", "enabled": "on"}
    r = form_post(web, "/admin/accounts/account-groups/new", "/admin/accounts/account-groups/new", {**form, "create_on_mt5": "on"})
    assert "copy settings from" in r.text  # template missing: refused
    r = form_post(web, "/admin/accounts/account-groups/new", "/admin/accounts/account-groups/new",
                  {**form, "create_on_mt5": "on", "template_group": "real\\ecn"})
    assert "group created on MT5" in r.text
    assert mt5.get_gateway(1).get_groups("real\\raw")

    raw = db.scalar(select(AccountGroup).where(AccountGroup.name == "Raw"))
    page = web.get(f"/admin/accounts/account-groups/{raw.id}")
    assert "Stop-out level" in page.text and "Delete account group" in page.text
    r = form_post(web, f"/admin/accounts/account-groups/{raw.id}", f"/admin/accounts/account-groups/{raw.id}",
                  {**form, "stop_out": "35", "margin_call": ""})
    assert "MT5 group updated" in r.text
    assert mt5.get_gateway(1).get_group("real\\raw").stop_out == Decimal("35")
    r = form_post(web, f"/admin/accounts/account-groups/{raw.id}", f"/admin/accounts/account-groups/{raw.id}/delete", {})
    assert "deleted, with its MT5 group" in r.text
    assert mt5.get_gateway(1).get_group("real\\raw") is None


def test_editing_the_server_rebuilds_its_connection(db):
    server = db.get(MT5Server, 1)
    before = mt5.get_gateway(1)
    _update(db, server, terminal_name="Renamed", timeout_ms=5000)
    assert mt5.get_gateway(1) is not before  # timeout changed -> new connection


def test_disabled_server_is_skipped_and_in_use_server_cannot_be_deleted(web, db, groups):
    c = _client(db, "d@x.com")
    accounts.open_account(db, mt5.for_account_group(db, groups["demo"]), None, c, groups["demo"])
    server = db.get(MT5Server, 1)
    with pytest.raises(ServiceError, match="Disable it, or delete it together with its CRM data"):
        servers.delete_server(db, None, server)
    _update(db, server, enabled=False)
    with pytest.raises(mt5.MT5Error, match="disabled"):
        mt5.get_gateway(1)
    assert sync.full_sync(db)["errors"] == []  # a disabled server is simply not synced


def test_client_sees_server_name_for_terminal(web, db, groups):
    c = _client(db, "term@x.com")
    accounts.open_account(db, mt5.for_account_group(db, groups["demo"]), None, c, groups["demo"])
    web_login(web, "term@x.com", "Client-Pass1", base="/portal")
    assert "Test-Live" in web.get("/portal/platforms").text
