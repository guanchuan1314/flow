"""Server-rendered UI: every page renders, key flows work through real form posts."""

import re
import time
from decimal import Decimal

import pyotp
import pytest
from sqlalchemy import select

from app.models import (
    Client, CommissionPlan, KycStatus, PaymentKind, PaymentRequest, Role, RuleKind, Wallet, WalletTxKind,
)
from app.mt5 import BUY
from app.services import accounts, commissions, copy_trading, pamm, payments, sync, wallet
from app.services import clients as client_svc
from tests.conftest import ADMIN, STRONG, register

CSRF_RE = re.compile(r'name="_csrf" value="([^"]+)"')


def csrf_of(resp) -> str:
    m = CSRF_RE.search(resp.text)
    assert m, "no CSRF token on page"
    return m.group(1)


def web_login(api, email, password, base="/admin"):
    page = api.get(f"{base}/login")
    r = api.post(f"{base}/login", data={"email": email, "password": password, "_csrf": csrf_of(page)},
                 follow_redirects=False)
    assert r.status_code == 303, r.text[:500]
    return r


def form_post(api, page_url, action, data, **kw):
    """GET a page to obtain its CSRF token, then POST a form."""
    token = csrf_of(api.get(page_url))
    return api.post(action, data={**data, "_csrf": token}, follow_redirects=kw.pop("follow", True), **kw)


@pytest.fixture
def admin_web(api, admin):
    web_login(api, *ADMIN)
    return api


# ---------------------------------------------------------------- auth & security
def test_admin_requires_login_and_redirects_back(api, admin):
    r = api.get("/admin/clients", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/login?next=")
    page = api.get("/admin/login")
    r = api.post("/admin/login", data={"email": ADMIN[0], "password": ADMIN[1], "next": "/admin/clients",
                                       "_csrf": csrf_of(page)}, follow_redirects=False)
    assert r.headers["location"] == "/admin/clients"


def test_open_redirect_blocked(api, admin):
    page = api.get("/admin/login")
    r = api.post("/admin/login", data={"email": ADMIN[0], "password": ADMIN[1], "next": "//evil.com/x",
                                       "_csrf": csrf_of(page)}, follow_redirects=False)
    assert r.headers["location"] == "/admin"


def test_bad_password_shows_error(api, admin):
    page = api.get("/admin/login")
    r = api.post("/admin/login", data={"email": ADMIN[0], "password": "nope", "_csrf": csrf_of(page)})
    assert r.status_code == 401 and "Invalid email or password" in r.text


def test_csrf_required(admin_web):
    r = admin_web.post("/admin/staff/departments/new", data={"name": "Sales"})
    assert r.status_code == 403 and "session expired" in r.text
    r = form_post(admin_web, "/admin/staff/departments/new", "/admin/staff/departments/new", {"name": "Sales"})
    assert r.status_code == 200 and "Department created" in r.text


def test_security_headers(api):
    r = api.get("/admin/login")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Cache-Control"] == "no-store"
    assert "X-Request-ID" in r.headers


def test_login_rate_limited(api, admin):
    page = api.get("/admin/login")
    token = csrf_of(page)
    codes = [api.post("/admin/login", data={"email": "x@y.com", "password": "bad", "_csrf": token}).status_code
             for _ in range(22)]
    assert 429 in codes


def test_two_step_totp_login(admin_web, api):
    page = admin_web.get("/admin/me/security")
    r = admin_web.post("/admin/me/2fa/start", data={"_csrf": csrf_of(page)})
    secret = re.search(r'class="secret-box flex-grow-1">([A-Z2-7]+)<', r.text).group(1)
    assert "data:image/svg+xml;base64," in r.text  # QR code rendered
    form_post(admin_web, "/admin/me/security", "/admin/me/2fa/confirm", {"code": pyotp.TOTP(secret).now()})
    form_post(admin_web, "/admin", "/admin/logout", {})

    page = api.get("/admin/login")
    r = api.post("/admin/login", data={"email": ADMIN[0], "password": ADMIN[1], "_csrf": csrf_of(page)})
    assert "Authenticator code" in r.text and 'name="password"' not in r.text
    bad = api.post("/admin/login/2fa", data={"totp_code": "000000", "_csrf": csrf_of(r)})
    assert bad.status_code == 401 and "Invalid 2FA code" in bad.text
    fresh = pyotp.TOTP(secret).at(time.time() + 30)  # the enrolment code can't be used again
    ok = api.post("/admin/login/2fa", data={"totp_code": fresh, "_csrf": csrf_of(bad)}, follow_redirects=False)
    assert ok.status_code == 303
    assert api.get("/admin").status_code == 200


def test_password_change_keeps_session_but_kills_others(api, admin):
    from fastapi.testclient import TestClient
    web_login(api, *ADMIN)
    other = TestClient(api.app)
    web_login(other, *ADMIN)
    r = form_post(api, "/admin/me/security", "/admin/me/password",
                  {"current_password": ADMIN[1], "new_password": "Brand-New-Pass9"})
    assert "Password updated" in r.text
    assert api.get("/admin", follow_redirects=False).status_code == 200
    assert other.get("/admin", follow_redirects=False).status_code == 303  # old session invalidated


def test_permission_denied_page(api, admin_web, db):
    role = db.scalar(select(Role).where(Role.name == "Sales"))
    form_post(admin_web, "/admin/staff/members/new", "/admin/staff/members/new", {
        "email": "sales@b.com", "full_name": "Sam", "password": "Sales-Pass1", "role_ids": str(role.id)})
    form_post(admin_web, "/admin", "/admin/logout", {})
    web_login(api, "sales@b.com", "Sales-Pass1")
    r = api.get("/admin/staff/members")
    assert r.status_code == 403 and "Access denied" in r.text
    assert "/admin/staff/members" not in api.get("/admin").text  # menu hides it


# ---------------------------------------------------------------- every page renders
def _seed_everything(api, db, gw, account_groups):
    """Create one of everything so list and detail pages have data to render."""
    c = register(api, "trader@x.com")
    approve_kyc_direct(db, c["id"])
    login = open_live_direct(db, gw, c["id"], account_groups["live"])
    gw.balance_operation(login, 5000, "seed")
    pos = gw.open_position(login, "EURUSD", BUY, Decimal("1"))
    gw.close_position(login, pos.position_id, "EURUSD", BUY, Decimal("0.5"))
    sync.full_sync(db, gw)

    m = register(api, "master@x.com")
    approve_kyc_direct(db, m["id"])
    master = open_live_direct(db, gw, m["id"], account_groups["live"])
    gw.balance_operation(master, 10000, "seed")
    strat = copy_trading.create_strategy(db, None, db.get(Client, m["id"]), master, "Alpha")
    copy_trading.subscribe(db, gw, None, db.get(Client, c["id"]), strat, login)

    pm = register(api, "pm@x.com")
    approve_kyc_direct(db, pm["id"])
    pm_login = open_live_direct(db, gw, pm["id"], account_groups["live"])
    fund = pamm.create_fund(db, gw, None, db.get(Client, pm["id"]), pm_login, "Fund One")
    wallet.post(db, c["id"], 1000, WalletTxKind.deposit)
    db.commit()
    pamm.invest(db, gw, None, fund, db.get(Client, c["id"]), 500)
    pamm.snapshot(db, gw, fund)

    plan = commissions.create_plan(db, None, "Plan", [0.8, 0.2], [
        {"symbol_pattern": "*", "kind": RuleKind.per_lot, "value": Decimal("5")}])
    ib = commissions.make_ib(db, None, db.get(Client, m["id"]), plan.id)
    client_svc.upload_kyc_document(db, None, db.get(Client, c["id"]), "passport", "p.pdf", b"%PDF-1.4 x")
    payments.create_request(db, db.get(Client, c["id"]), db.get(Client, c["id"]), PaymentKind.deposit, 100)
    return {"client": c["id"], "login": login, "strategy": strat.id, "fund": fund.id, "ib": ib.id,
            "plan": plan.id, "type": account_groups["live"]}


def approve_kyc_direct(db, client_id):
    c = db.get(Client, client_id)
    c.kyc_status = KycStatus.approved
    db.commit()


def open_live_direct(db, gw, client_id, group_id):
    return accounts.open_account(db, gw, None, db.get(Client, client_id), group_id)["account"].login


def test_every_admin_page_renders(admin_web, db, gw, account_groups):
    ids = _seed_everything(admin_web, db, gw, account_groups)
    payment_id = db.scalar(select(PaymentRequest.id))
    pages = [
        "/admin", "/admin/clients", "/admin/clients?q=trader", "/admin/clients/new", "/admin/kyc",
        *[f"/admin/clients/{ids['client']}?tab={t}" for t in ("overview", "accounts", "kyc", "finance", "notes", "activity")],
        "/admin/accounts/trading-accounts", f"/admin/accounts/trading-accounts/{ids['login']}", "/admin/accounts/account-groups", "/admin/accounts/account-groups/new",
        "/admin/payment-methods", "/admin/payment-methods/new", "/admin/kyc-methods", "/admin/kyc-methods/new",
        "/admin/orders", "/admin/orders?status=history", "/admin/security/ip-rules", "/admin/import/trades/jobs",
        "/admin/trading-setup/symbols", "/admin/trading-setup/symbols/new", "/admin/trading-setup/liquidity", "/admin/trading-setup/liquidity?kind=feed",
        "/admin/trading-setup/liquidity/new?kind=gateway", "/admin/trading-setup/liquidity/new?kind=feed&module=DXFeeder64.exe",
        "/admin/trading-setup/liquidity/new?kind=gateway&module=LMAXGateway64.exe", "/admin/trading-setup/routing", "/admin/trading-setup/routing/new",
        "/admin/trading-setup/overview", "/admin/server-tools/health", "/admin/accounts/trader-styles", "/admin/accounts/trader-styles?style=suggested&demo=1",
        "/admin/configuration", "/admin/accounts/inactive-accounts", "/admin/server-tools/restart", "/admin/server-tools/pricing",
        "/admin/import/account-groups", "/admin/import/trading-accounts", "/admin/automation", "/admin/automation/new",
        "/admin/accounts/trader-styles/setup", "/admin/server-tools/health?only=warnings", "/admin/server-tools/health?server=1",
        "/admin/accounts/trader-styles?server=1",
        f"/admin/accounts/account-groups/{ids['type']}", "/admin/payments", "/admin/payments?status=", f"/admin/payments/{payment_id}",
        f"/admin/payments/new?client_id={ids['client']}", "/admin/positions", "/admin/positions?partial=1",
        "/admin/deals", "/admin/deals?kind=trades&date_from=2020-01-01", "/admin/import/trades",
        "/admin/copy", f"/admin/copy/{ids['strategy']}", "/admin/pamm", f"/admin/pamm/{ids['fund']}",
        "/admin/ibs", f"/admin/ibs/{ids['ib']}", "/admin/plans", "/admin/plans/new", "/admin/commissions",
        "/admin/commissions?status=", "/admin/campaigns", "/admin/sources", "/admin/staff/members", "/admin/staff/members/new",
        "/admin/staff/members/1", "/admin/staff/roles", "/admin/staff/roles/new", "/admin/staff/roles/1", "/admin/staff/departments", "/admin/security/audit",
        "/admin/staff/departments/new", "/admin/referral-links", "/admin/import/history", "/admin/import/history?partial=1", "/admin/security/login-attempts", "/admin/me/security",
    ]
    for url in pages:
        r = admin_web.get(url)
        assert r.status_code == 200, f"{url}: {r.status_code}\n{r.text[:800]}"
    assert admin_web.get("/admin/clients/99999").status_code == 404


def test_every_portal_page_renders(api, db, gw, account_groups, admin):
    ids = _seed_everything(api, db, gw, account_groups)
    web_login(api, "trader@x.com", STRONG, base="/portal")
    for url in ("/portal", "/portal/accounts", "/portal/wallet", "/portal/copy", "/portal/pamm", "/portal/referral",
                "/portal/verification", "/portal/profile"):
        r = api.get(url)
        assert r.status_code == 200, f"{url}: {r.status_code}\n{r.text[:800]}"
    assert "Alpha" in api.get("/portal/copy").text
    assert "Fund One" in api.get("/portal/pamm").text
    for url in ("/portal/login", "/portal/register", "/portal/forgot-password", "/portal/reset-password?token=x"):
        assert api.get(url).status_code == 200


# ---------------------------------------------------------------- admin flows
def test_admin_open_account_shows_credentials_once(admin_web, db, gw, account_groups):
    c = register(admin_web, "open@x.com")
    approve_kyc_direct(db, c["id"])
    r = form_post(admin_web, f"/admin/clients/{c['id']}?tab=accounts", f"/admin/clients/{c['id']}/accounts",
                  {"account_group_id": account_groups["live"], "leverage": "200"})
    assert "shown <strong>once</strong>" in r.text
    login = int(re.search(r'secret-box flex-grow-1">(\d+)<', r.text).group(1))
    pwd = re.findall(r'secret-box flex-grow-1">([^<]+)<', r.text)[1]
    assert gw.check_password(login, pwd.replace("&amp;", "&").replace("&#34;", '"'))
    assert pwd not in str(admin_web.cookies.get("crm_session"))


def test_payment_approval_via_ui(api, admin_web, db, gw, account_groups):
    role = db.scalar(select(Role).where(Role.name == "Finance"))
    form_post(admin_web, "/admin/staff/members/new", "/admin/staff/members/new", {
        "email": "fin@b.com", "full_name": "Fin", "password": "Finance-Pass1", "role_ids": str(role.id)})
    c = register(admin_web, "payer@x.com")
    approve_kyc_direct(db, c["id"])
    login = open_live_direct(db, gw, c["id"], account_groups["live"])
    r = form_post(admin_web, f"/admin/payments/new?client_id={c['id']}", "/admin/payments/new",
                  {"client_id": c["id"], "kind": "deposit", "amount": "250", "method": "bank", "trading_login": login})
    assert "Another staff member must approve" in r.text
    pid = db.scalar(select(PaymentRequest.id))
    r = form_post(admin_web, f"/admin/payments/{pid}", f"/admin/payments/{pid}/approve", {})
    assert "cannot approve a request you created" in r.text
    form_post(admin_web, "/admin", "/admin/logout", {})
    web_login(api, "fin@b.com", "Finance-Pass1")
    r = form_post(api, f"/admin/payments/{pid}", f"/admin/payments/{pid}/approve", {"comment": "received"})
    assert f"Payment #{pid} approved" in r.text
    assert gw.balance[login] == Decimal("250")


def test_client_crud_and_notes_via_ui(admin_web, db):
    r = form_post(admin_web, "/admin/clients/new", "/admin/clients/new",
                  {"email": "lead@x.com", "first_name": "Lee", "last_name": "Lead", "country": "de"})
    assert "Client created" in r.text
    cid = db.scalar(select(Client.id).where(Client.email == "lead@x.com"))
    r = form_post(admin_web, f"/admin/clients/{cid}", f"/admin/clients/{cid}/edit",
                  {"first_name": "Lee", "last_name": "Lead", "country": "FR", "status": "active", "date_of_birth": "1990-02-03"})
    assert "Client updated" in r.text
    r = form_post(admin_web, f"/admin/clients/{cid}?tab=notes", f"/admin/clients/{cid}/notes", {"body": "Called, interested in gold"})
    assert "interested in gold" in r.text
    r = form_post(admin_web, f"/admin/clients/{cid}", f"/admin/clients/{cid}/edit",
                  {"first_name": "Lee", "last_name": "Lead", "country": "FRA", "status": "active"})
    assert "2-letter ISO code" in r.text


def test_plan_and_campaign_creation_via_ui(admin_web, db):
    r = form_post(admin_web, "/admin/plans/new", "/admin/plans/new", {
        "name": "Gold plan", "level_shares": "70, 30", "trigger": "close", "min_hold_seconds": "60",
        "rule_pattern": ["XAU*", "*"], "rule_type": ["", ""], "rule_kind": ["per_lot", "spread_share"],
        "rule_value": ["8", "40"], "rule_priority": ["10", "100"]})
    assert "Commission plan created" in r.text
    plan = db.scalar(select(CommissionPlan).where(CommissionPlan.name == "Gold plan"))
    assert plan.level_shares == [0.7, 0.3]
    assert [(x.symbol_pattern, float(x.value)) for x in plan.rules] == [("XAU*", 8.0), ("*", 0.4)]
    r = form_post(admin_web, "/admin/campaigns", "/admin/campaigns", {"code": "SPRING", "bonus_pct": "20", "max_bonus": "200"})
    assert "Campaign created" in r.text and "SPRING" in r.text


# ---------------------------------------------------------------- portal flows
def test_portal_register_open_demo_and_wallet(api, gw, account_groups):
    page = api.get("/portal/register?ref=&utm_campaign=launch")
    r = api.post("/portal/register", data={
        "email": "new@x.com", "password": STRONG, "first_name": "Nia", "last_name": "New", "country": "GB",
        "accept_terms": "on", "utm_campaign": "launch", "_csrf": csrf_of(page)})
    assert r.status_code == 200 and "Welcome, Nia" in r.text
    # live is refused before KYC, demo works and shows credentials
    r = form_post(api, "/portal/accounts", "/portal/accounts/open", {"account_group_id": account_groups["live"]})
    assert "KYC must be approved" in r.text
    r = form_post(api, "/portal/accounts", "/portal/accounts/open", {"account_group_id": account_groups["demo"]})
    assert "Trading password" in r.text
    r = form_post(api, "/portal/wallet", "/portal/wallet/deposit", {"amount": "100"})
    assert "Identity verification" in r.text  # deposits need KYC, enforced server-side


def test_portal_kyc_upload_checks_content(api, db):
    register(api, "doc@x.com")
    web_login(api, "doc@x.com", STRONG, base="/portal")
    token = csrf_of(api.get("/portal/verification"))
    r = api.post("/portal/verification", data={"doc_type": "passport", "_csrf": token},
                 files={"file": ("fake.pdf", b"MZ not a pdf", "application/pdf")})
    assert "does not match its type" in r.text
    r = api.post("/portal/verification", data={"doc_type": "passport", "_csrf": csrf_of(r)},
                 files={"file": ("real.pdf", b"%PDF-1.7 ok", "application/pdf")})
    assert "Document uploaded" in r.text


def test_forgot_and_reset_password(api):
    from app.services import notifications
    register(api, "forgot@x.com")
    r = form_post(api, "/portal/forgot-password", "/portal/forgot-password", {"email": "forgot@x.com"})
    assert "If that email is registered" in r.text
    r2 = form_post(api, "/portal/forgot-password", "/portal/forgot-password", {"email": "nobody@x.com"})
    assert "If that email is registered" in r2.text  # no account enumeration
    body = notifications.outbox[-1].get_content()
    token = re.search(r"token=([\w.\-]+)", body).group(1)
    r = form_post(api, f"/portal/reset-password?token={token}", "/portal/reset-password",
                  {"token": token, "new_password": "Reset-Pass77"})
    assert "Your password has been changed" in r.text
    r = form_post(api, f"/portal/reset-password?token={token}", "/portal/reset-password",
                  {"token": token, "new_password": "Again-Pass77"})
    assert "already been used" in r.text  # single use
    web_login(api, "forgot@x.com", "Reset-Pass77", base="/portal")


def test_portal_wallet_transfer_and_pamm(api, admin_web, db, gw, account_groups):
    c = register(api, "inv@x.com")
    approve_kyc_direct(db, c["id"])
    login = open_live_direct(db, gw, c["id"], account_groups["live"])
    wallet.post(db, c["id"], 2000, WalletTxKind.deposit)
    db.commit()
    pm = register(api, "mgr@x.com")
    approve_kyc_direct(db, pm["id"])
    fund = pamm.create_fund(db, gw, None, db.get(Client, pm["id"]), open_live_direct(db, gw, pm["id"], account_groups["live"]), "Growth")

    web_login(api, "inv@x.com", STRONG, base="/portal")
    r = form_post(api, "/portal/wallet", "/portal/wallet/transfer", {"login": login, "direction": "to_mt5", "amount": "500"})
    assert "Transfer completed" in r.text and gw.balance[login] == Decimal("500")
    r = form_post(api, "/portal/pamm", f"/portal/pamm/{fund.id}/invest", {"amount": "1000"})
    assert "Invested in Growth" in r.text
    r = form_post(api, "/portal/pamm", f"/portal/pamm/{fund.id}/withdraw", {"all_units": "on"})
    assert "Withdrawal paid to your wallet" in r.text
    assert db.scalar(select(Wallet.balance).where(Wallet.client_id == c["id"])) == Decimal("1500")


def test_portal_cannot_touch_other_clients_account(api, db, gw, account_groups):
    a = register(api, "a@x.com")
    b = register(api, "b@x.com")
    approve_kyc_direct(db, b["id"])
    b_login = open_live_direct(db, gw, b["id"], account_groups["live"])
    web_login(api, "a@x.com", STRONG, base="/portal")
    r = form_post(api, "/portal/accounts", f"/portal/accounts/{b_login}/password", {"which": "master"})
    assert r.status_code in (200, 403) and "does not belong" in r.text



def test_audit_log_names_actions_and_shows_details_in_a_popup(admin_web, admin, db):
    from app.core.audit import audit

    c = register(admin_web, "audited@x.com")
    audit(db, admin, "account.status", "trading_account", 500123,
          {"before": "active", "after": "disabled", "logins": [1, 2], "sales": admin.id, "ok": True})
    db.commit()
    page = admin_web.get("/admin/security/audit").text
    assert "Account status changed" in page and "Client registered" in page
    assert "account.status" not in page and "client.register" not in page  # no raw codes
    assert "Trading account 500123" in page
    m = re.search(r'<template id="audit-(\d+)">(.*?)</template>', page, re.S)
    assert m and 'data-details="audit-' in page
    details = m.group(2)
    assert "Before</dt>" in details and ">Active<" in details and ">Disabled<" in details
    assert ">1, 2<" in details and ">Yes<" in details and admin.full_name in details
    assert "{" not in details  # never JSON
    # filter by area: only payment events
    assert "Account status changed" not in admin_web.get("/admin/security/audit?action=payment").text
    assert "Account status changed" in admin_web.get("/admin/security/audit?action=account").text
    assert c["id"]


def test_departments_create_rename_delete(admin_web, db):
    from app.models import Department, Staff

    page = admin_web.get("/admin/staff/departments").text
    assert 'href="/admin/staff/departments/new"' in page
    form_post(admin_web, "/admin/staff/departments/new", "/admin/staff/departments/new", {"name": "  Business   development "})
    dep = db.scalar(select(Department).where(Department.name == "Business development"))
    assert dep
    admin = db.scalar(select(Staff).where(Staff.email == ADMIN[0]))
    admin.department_id = dep.id
    db.commit()
    dep_id, admin_id = dep.id, admin.id
    assert admin.full_name in admin_web.get(f"/admin/staff/departments/{dep.id}").text
    form_post(admin_web, f"/admin/staff/departments/{dep.id}", f"/admin/staff/departments/{dep.id}", {"name": "BD"})
    db.expire_all()
    assert db.get(Department, dep.id).name == "BD"
    r = form_post(admin_web, f"/admin/staff/departments/{dep.id}", f"/admin/staff/departments/{dep.id}/delete", {})
    assert "BD" in r.text and "deleted." in r.text
    db.expire_all()
    assert db.get(Department, dep_id) is None and db.get(Staff, admin_id).department_id is None
    page = admin_web.get("/admin/security/audit").text
    assert "Department renamed" in page and "Department deleted" in page


def test_staff_and_security_pages_are_grouped_in_the_menu(admin_web):
    page = admin_web.get("/admin/staff/roles").text
    tabs = page[page.index("crm-hub-tabs"):]
    assert all(f'href="{u}"' in tabs[:600] for u in ("/admin/staff/members", "/admin/staff/roles", "/admin/staff/departments"))
    page = admin_web.get("/admin/security/login-attempts").text
    tabs = page[page.index("crm-hub-tabs"):]
    assert all(f'href="{u}"' in tabs[:600] for u in ("/admin/security/audit", "/admin/security/login-attempts", "/admin/security/ip-rules"))


def test_account_group_can_be_renamed(admin_web, db, account_groups):
    from app.models import AccountGroup

    url = f"/admin/accounts/account-groups/{account_groups['live']}"
    fields = {"mt5_group": "real\\standard", "leverage_options": "100, 200, 500", "default_leverage": "100",
              "min_deposit": "0", "spread_markup_points": "10", "max_accounts_per_client": "5", "enabled": "on",
              "server_id": "1"}
    r = form_post(admin_web, url, url, {**fields, "name": "  Standard   Plus "})
    assert "Account group saved" in r.text
    db.expire_all()
    assert db.get(AccountGroup, account_groups["live"]).name == "Standard Plus"
    r = form_post(admin_web, url, url, {**fields, "name": "Demo"})
    assert "name already exists" in r.text


def test_pages_live_under_their_menu_and_old_addresses_redirect(admin_web):
    for old, new in (("/admin/symbols", "/admin/trading-setup/symbols"), ("/admin/trading-setup", "/admin/trading-setup/overview"),
                     ("/admin/staff/1", "/admin/staff/members/1"), ("/admin/audit?action=client", "/admin/security/audit"),
                     ("/admin/accounts", "/admin/accounts/trading-accounts"), ("/admin/sync", "/admin/import/trades"),
                     ("/admin/server-health", "/admin/server-tools/health"), ("/admin/mt5-servers/1/pricing", "/admin/server-tools/pricing")):
        r = admin_web.get(old)
        assert r.status_code == 200 and r.url.path == new, (old, r.url)
    assert admin_web.get("/admin/audit?action=client").url.query == b"action=client"
    assert admin_web.get("/admin/staff/members/1/nothing-here").status_code == 404
    assert admin_web.get("/admin/no-such-page").status_code == 404
    menu = admin_web.get("/admin").text
    assert 'href="/admin/trading-setup/overview"' in menu and 'href="/admin/staff/members"' in menu
    assert 'href="/admin/security/audit"' in menu and 'href="/admin/server-tools/health"' in menu
