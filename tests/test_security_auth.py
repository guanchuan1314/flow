"""Auth, sessions, staff privileges and configuration hardening."""

import logging
import time
from datetime import timedelta

import pyotp
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.config import ConfigError, Settings, get_settings
from app.core.errors import Forbidden
from app.core.security import decrypt_secret
from app.db import utcnow
from app.models import Client, KycStatus, Role, Staff
from app.mt5 import MT5Error
from app.services import accounts, auth, automation, clients, notifications, trader_styles
from app.services import staff as staff_svc
from tests.conftest import ADMIN, STRONG, login_client, login_staff, register
from tests.test_migrations import _run, scratch_engine  # noqa: F401
from tests.test_web import csrf_of, form_post, web_login


def make_staff(db, admin, email, perms, name=None):
    role = staff_svc.create_role(db, admin, name or f"Role {email}", perms)
    return staff_svc.create_staff(db, admin, email=email, full_name=email.split("@")[0], password="Staff-Pass1",
                                  role_ids=[role.id])


# ---------------------------------------------------------------- 1. no privilege escalation
def test_staff_manager_cannot_escalate(api, admin, db):
    mgr = make_staff(db, admin, "mgr@b.com", ["staff.view", "staff.manage", "clients.view"], "StaffMgr")
    h = login_staff(api, "mgr@b.com", "Staff-Pass1")
    admin_role = db.scalar(select(Role).where(Role.name == "Admin"))
    own_role = mgr.roles[0]

    assert api.post("/api/roles", json={"name": "Up", "permissions": ["accounts.manage"]}, headers=h).status_code == 403
    assert api.post("/api/roles", json={"name": "All", "permissions": ["*"]}, headers=h).status_code == 403
    assert api.post("/api/roles", json={"name": "Clients.*", "permissions": ["clients.*"]}, headers=h).status_code == 403
    assert api.post("/api/roles", json={"name": "Lesser", "permissions": ["clients.view"]}, headers=h).status_code == 201
    r = api.patch(f"/api/roles/{own_role.id}", json={"permissions": ["staff.view"]}, headers=h)
    assert r.status_code == 403 and "role you hold" in r.json()["detail"]
    assert api.post("/api/staff", json={"email": "evil@b.com", "full_name": "Evil", "password": "Evil-Pass1",
                                        "role_ids": [admin_role.id]}, headers=h).status_code == 403

    admin_id = admin.id
    for body in ({"password": "Hijack-Pass1"}, {"is_active": False}, {"role_ids": [own_role.id]},
                 {"allowed_ips": ["10.0.0.1"]}):
        assert api.patch(f"/api/staff/{admin_id}", json=body, headers=h).status_code == 403, body
    assert api.post(f"/api/staff/{admin_id}/reset-2fa", headers=h).status_code == 403
    # harmless edits to a stronger colleague are fine
    assert api.patch(f"/api/staff/{admin_id}", json={"full_name": "The Boss"}, headers=h).status_code == 200
    # a weaker colleague can be managed
    lesser = make_staff(db, admin, "lesser@b.com", ["clients.view"])
    assert api.patch(f"/api/staff/{lesser.id}", json={"password": "Reset-Pass1"}, headers=h).status_code == 200


def test_star_holder_grants_star(api, admin_h):
    assert api.post("/api/roles", json={"name": "Root2", "permissions": ["*"]}, headers=admin_h).status_code == 201


def test_web_staff_form_cannot_escalate(api, admin, db):
    make_staff(db, admin, "mgr@b.com", ["staff.view", "staff.manage"])
    admin_role = db.scalar(select(Role).where(Role.name == "Admin"))
    web_login(api, "mgr@b.com", "Staff-Pass1")
    r = form_post(api, "/admin/staff/new", "/admin/staff/new", {
        "email": "evil@b.com", "full_name": "Evil", "password": "Evil-Pass1", "role_ids": str(admin_role.id)})
    assert "grant permissions you don&#39;t hold" in r.text or "grant permissions you don't hold" in r.text
    assert db.scalar(select(Staff).where(Staff.email == "evil@b.com")) is None
    # saving an admin's page without changing anything sensitive works; changing the password doesn't
    page = f"/admin/staff/{admin.id}"
    base = {"full_name": "Boss", "is_active": "on", "role_ids": str(admin_role.id), "allowed_ips": ""}
    assert "Staff member saved" in form_post(api, page, page, base).text
    assert "permissions you don" in form_post(api, page, page, {**base, "password": "Hijack-Pass1"}).text


# ---------------------------------------------------------------- 2. token version
def test_password_change_and_disable_invalidate_tokens(api, admin, db):
    sales = make_staff(db, admin, "s@b.com", ["clients.view"])
    h = login_staff(api, "s@b.com", "Staff-Pass1")
    assert api.post("/api/auth/staff/password", json={"current_password": "Staff-Pass1",
                                                      "new_password": "Staff-Pass2"}, headers=h).status_code == 204
    assert api.get("/api/auth/staff/me", headers=h).status_code == 401
    h = login_staff(api, "s@b.com", "Staff-Pass2")
    db.expire_all()
    staff_svc.update_staff(db, admin, db.get(Staff, sales.id), is_active=False)
    staff_svc.update_staff(db, admin, db.get(Staff, sales.id), is_active=True)
    assert api.get("/api/auth/staff/me", headers=h).status_code == 401  # tokens from before stay dead


def test_reset_and_2fa_disable_invalidate_client_tokens(api, db):
    register(api, "c@x.com")
    h = login_client(api, "c@x.com")
    auth.request_password_reset(db, "c@x.com", None)
    token = notifications.outbox[-1].get_content().split("token=")[1].split()[0]
    auth.reset_password(db, token, "Client-Pass2", None)
    assert api.get("/api/portal/me", headers=h).status_code == 401
    h = login_client(api, "c@x.com", "Client-Pass2")
    secret = api.post("/api/auth/client/2fa/enroll", headers=h).json()["secret"]
    assert api.post("/api/auth/client/2fa/confirm", json={"code": pyotp.TOTP(secret).now()}, headers=h).status_code == 204
    code = pyotp.TOTP(secret).at(time.time() + 30)
    assert api.post("/api/auth/client/2fa/disable", json={"code": code}, headers=h).status_code == 204
    assert api.get("/api/portal/me", headers=h).status_code == 401


# ---------------------------------------------------------------- 3 + 11. failed 2FA counts; backoff
def test_wrong_totp_in_single_step_login_counts(api, admin, db):
    secret = auth.begin_totp_enrollment(db, admin)["secret"]
    auth.confirm_totp_enrollment(db, admin, pyotp.TOTP(secret).now())
    for _ in range(5):
        r = api.post("/api/auth/staff/login", json={"email": ADMIN[0], "password": ADMIN[1], "totp_code": "000000"})
        assert r.status_code == 401
    r = api.post("/api/auth/staff/login", json={"email": ADMIN[0], "password": ADMIN[1],
                                                "totp_code": pyotp.TOTP(secret).at(time.time() + 30)})
    assert r.status_code == 423


def test_backoff_doubles_and_resets(api, admin, db):
    for _ in range(5):
        api.post("/api/auth/staff/login", json={"email": ADMIN[0], "password": "wrong"})
    db.expire_all()
    s = db.get(Staff, admin.id)
    assert timedelta(seconds=50) < s.locked_until - utcnow() <= timedelta(minutes=1)
    s.locked_until = utcnow() - timedelta(seconds=1)  # waited
    db.commit()
    api.post("/api/auth/staff/login", json={"email": ADMIN[0], "password": "wrong"})
    db.expire_all()
    s = db.get(Staff, admin.id)
    assert timedelta(minutes=1) < s.locked_until - utcnow() <= timedelta(minutes=2)
    s.failed_logins, s.locked_until = 40, utcnow() - timedelta(seconds=1)
    db.commit()
    api.post("/api/auth/staff/login", json={"email": ADMIN[0], "password": "wrong"})
    db.expire_all()
    assert db.get(Staff, admin.id).locked_until - utcnow() <= timedelta(minutes=get_settings().lockout_minutes)
    s = db.get(Staff, admin.id)
    s.locked_until = utcnow() - timedelta(seconds=1)
    db.commit()
    login_staff(api, *ADMIN)
    db.expire_all()
    assert db.get(Staff, admin.id).failed_logins == 0


# ---------------------------------------------------------------- 4. web sessions
def test_logout_kills_copied_cookie(api, admin):
    web_login(api, *ADMIN)
    thief = TestClient(api.app)
    thief.cookies.update(api.cookies)
    assert thief.get("/admin", follow_redirects=False).status_code == 200
    form_post(api, "/admin", "/admin/logout", {})
    assert thief.get("/admin", follow_redirects=False).status_code == 303


def test_idle_timeout(api, admin, monkeypatch):
    web_login(api, *ADMIN)
    assert api.get("/admin", follow_redirects=False).status_code == 200
    monkeypatch.setattr(get_settings(), "staff_session_idle_minutes", -1)
    assert api.get("/admin", follow_redirects=False).status_code == 303


def test_web_2fa_disable_keeps_own_session(admin_web_2fa):
    api, secret = admin_web_2fa
    r = form_post(api, "/admin/me/security", "/admin/me/2fa/disable", {"code": pyotp.TOTP(secret).at(time.time() + 30)})
    assert "Two-factor authentication disabled" in r.text
    assert api.get("/admin", follow_redirects=False).status_code == 200


@pytest.fixture
def admin_web_2fa(api, admin, db):
    secret = auth.begin_totp_enrollment(db, admin)["secret"]
    auth.confirm_totp_enrollment(db, admin, pyotp.TOTP(secret).now())
    page = api.get("/admin/login")
    r = api.post("/admin/login", data={"email": ADMIN[0], "password": ADMIN[1], "_csrf": csrf_of(page)})
    api.post("/admin/login/2fa", data={"totp_code": pyotp.TOTP(secret).at(time.time() + 30), "_csrf": csrf_of(r)})
    db.expire_all()
    db.get(Staff, admin.id).totp_last_step = None  # let the test use a code again
    db.commit()
    return api, secret


# ---------------------------------------------------------------- 5. production settings
def test_production_settings_refused():
    url = get_settings().database_url
    with pytest.raises(ValueError) as exc:
        Settings(_env_file=None, app_env="prod", database_url=url, auto_migrate=True, mt5_gateway="mock",
                 allowed_hosts="*", credentials_key="", smtp_host="", staff_2fa_required=False)
    for name in ("PUBLIC_URL", "ALLOWED_HOSTS", "CREDENTIALS_KEY", "STAFF_2FA_REQUIRED", "SMTP_HOST",
                 "AUTO_MIGRATE", "MT5_GATEWAY"):
        assert name in str(exc.value)
    good = dict(_env_file=None, app_env="prod", database_url=url, secret_key="s" * 40, credentials_key="c" * 40,
                public_url="https://crm.example.com", allowed_hosts="crm.example.com", staff_2fa_required=True,
                smtp_host="smtp.example.com", auto_migrate=False, mt5_gateway="manager")
    assert Settings(**good).is_prod
    for bad in ({"embedded_worker": True}, {"mock_state_file": "x.json"}, {"allowed_hosts": "*"}):
        with pytest.raises(ValueError):
            Settings(**{**good, **bad})


def test_config_error_is_readable(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigError) as exc:
            get_settings()
        assert str(exc.value).startswith("APP_ENV=prod but the configuration is not safe")
    finally:
        monkeypatch.delenv("APP_ENV")
        get_settings.cache_clear()


# ---------------------------------------------------------------- 6. email bodies never logged
def test_email_body_not_logged():
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("app.services.notifications")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        notifications.send_email("a@x.com", "Reset", "secret-link-123")
    finally:
        logger.removeHandler(handler)
    text_ = " ".join(r.getMessage() for r in records)
    assert "a@x.com" in text_ and "secret-link-123" not in text_
    assert notifications.outbox[-1].get_content().strip() == "secret-link-123"


# ---------------------------------------------------------------- 7. client email changes
def test_change_email_needs_permission_and_tells_old_address(db, admin):
    c = clients.register_client(db, None, email="old@x.com", first_name="Ann", last_name="Lee", password=STRONG)
    editor = make_staff(db, admin, "ed@b.com", ["clients.view", "clients.view_all", "clients.manage"])
    with pytest.raises(Forbidden):
        clients.update_client(db, editor, c, {"email": "new@x.com"})
    clients.update_client(db, editor, c, {"email": "OLD@x.com", "city": "Leeds"})  # same address: fine
    clients.update_client(db, admin, c, {"email": "new@x.com"})
    assert db.get(Client, c.id).email == "new@x.com"
    msg = notifications.outbox[-1]
    assert msg["To"] == "old@x.com" and "new@x.com" in msg.get_content()
    manager = db.scalar(select(Role).where(Role.name == "Manager"))
    assert any(p in manager.permissions for p in ("clients.*", "clients.change_email"))


def test_admin_kyc_upload_size_capped(api, admin, db):
    c = clients.register_client(db, None, email="k@x.com", first_name="K", last_name="Y", password=STRONG)
    web_login(api, *ADMIN)
    page = api.get(f"/admin/clients/{c.id}?tab=kyc")
    big = b"%PDF-" + b"0" * (11 * 1024 * 1024)
    r = api.post(f"/admin/clients/{c.id}/kyc/upload", data={"doc_type": "passport", "_csrf": csrf_of(page)},
                 files={"file": ("a.pdf", big, "application/pdf")})
    assert "File too large" in r.text


# ---------------------------------------------------------------- 8. automation permissions
def test_automation_actions_need_their_permissions(db, admin, account_groups):
    only = make_staff(db, admin, "auto@b.com", ["automation.manage"])
    args = dict(trigger="inactive", params={"days": 30}, filters={"kind": "live"}, cooldown_hours=24)
    with pytest.raises(Forbidden):
        automation.save_rule(db, only, None, name="Lev", action="change_leverage", action_params={"leverage": 100}, **args)
    with pytest.raises(Forbidden):
        automation.save_rule(db, only, None, name="Mail", action="email_client",
                             action_params={"subject": "Hi", "message": "Hello"}, **args)
    note = automation.save_rule(db, only, None, name="Note", action="client_note", action_params={"message": "x"},
                                **args)
    automation.set_enabled(db, only, note, True)
    lev = automation.save_rule(db, admin, None, name="Lev", action="change_leverage", action_params={"leverage": 100},
                               **args)
    with pytest.raises(Forbidden):
        automation.set_enabled(db, only, lev, True)
    with pytest.raises(Forbidden):
        automation.run_rule(db, lev, only)
    dealer = make_staff(db, admin, "dealer@b.com", ["automation.manage", "accounts.manage", "clients.view_all"])
    automation.set_enabled(db, dealer, lev, True)


# ---------------------------------------------------------------- 9. trader styles move
def test_trader_styles_move_only_visible_accounts(db, gw, admin, account_groups):
    c = clients.register_client(db, None, email="t@x.com", first_name="T", last_name="S", password=STRONG)
    c.kyc_status = KycStatus.approved
    db.commit()
    acc = accounts.open_account(db, gw, None, c, account_groups["live"])["account"]
    dealer = make_staff(db, admin, "d@b.com", ["accounts.view", "accounts.manage", "clients.view"])
    done, failed = trader_styles.move(db, gw, dealer, [acc.login], account_groups["demo"])
    assert done == [] and "not assigned" in failed[0]


# ---------------------------------------------------------------- 10. TOTP at rest and replay
def test_totp_secret_encrypted_and_legacy_plaintext_upgraded(api, admin, db):
    secret = auth.begin_totp_enrollment(db, admin)["secret"]
    db.expire_all()
    stored = db.get(Staff, admin.id).totp_secret
    assert stored.startswith("gAAAA") and decrypt_secret(stored) == secret
    s = db.get(Staff, admin.id)
    s.totp_secret, s.totp_enabled = secret, True  # as saved before encryption
    db.commit()
    login_staff(api, *ADMIN, totp=pyotp.TOTP(secret).now())
    db.expire_all()
    assert decrypt_secret(db.get(Staff, admin.id).totp_secret) == secret
    r = api.post("/api/auth/staff/login", json={"email": ADMIN[0], "password": ADMIN[1],
                                                "totp_code": pyotp.TOTP(secret).now()})
    assert r.status_code == 401  # the same code twice


def test_migration_encrypts_existing_totp_secrets(scratch_engine):  # noqa: F811
    _run(scratch_engine, command.upgrade, "0014")
    with scratch_engine.begin() as conn:
        conn.execute(text("""insert into staff (email, full_name, password_hash, is_active, totp_secret, totp_enabled,
            allowed_ips, failed_logins, created_at, updated_at) values ('a@b.com', 'A', 'x', true,
            'JBSWY3DPEHPK3PXP', true, '[]', 0, now(), now())"""))
    _run(scratch_engine, command.upgrade, "head")
    with scratch_engine.connect() as conn:
        stored, version = conn.execute(text("select totp_secret, token_version from staff")).one()
    assert decrypt_secret(stored) == "JBSWY3DPEHPK3PXP" and version == 0
    _run(scratch_engine, command.downgrade, "0014")
    with scratch_engine.connect() as conn:
        assert conn.execute(text("select totp_secret from staff")).scalar() == "JBSWY3DPEHPK3PXP"


# ---------------------------------------------------------------- 12. enumeration
def test_registration_claims_website_lead_only(api, db):
    lead = clients.capture_website_lead(db, name="Web Lead", email="lead@x.com", phone=None, country=None,
                                        topic="accounts", message="Call me")
    claimed = register(api, "lead@x.com")
    assert claimed["id"] == lead.id and claimed["status"] == "active"
    login_client(api, "lead@x.com")
    r = api.post("/api/auth/client/register", json={"email": "lead@x.com", "password": STRONG, "first_name": "X",
                                                    "last_name": "Y"})
    assert r.status_code == 409  # it has a password now


def test_unknown_email_still_checks_a_hash(api, admin):
    auth._dummy_hash = None
    r = api.post("/api/auth/staff/login", json={"email": "nobody@b.com", "password": "whatever"})
    assert r.status_code == 401 and auth._dummy_hash is not None


# ---------------------------------------------------------------- 13 + 14. readiness, errors, request ids
def test_ready_hides_details_from_remote_callers(api):
    r = api.get("/ready")
    assert set(r.json()) == {"status"}


def test_mt5_error_detail_not_returned(api):
    def boom():
        raise MT5Error("manager 10.1.2.3:443 refused login 1000")

    api.app.add_api_route("/api/_boom", boom)
    r = api.get("/api/_boom", headers={"X-Request-ID": "trace-42"})
    assert r.status_code == 502 and r.json() == {"detail": "MT5 server error", "request_id": "trace-42"}
    assert r.headers["X-Request-ID"] == "trace-42"


def test_unsafe_request_id_replaced(api):
    r = api.get("/health", headers={"X-Request-ID": "x" * 65})
    assert r.headers["X-Request-ID"] != "x" * 65
    r = api.get("/health", headers={"X-Request-ID": "bad id<script>"})
    assert "<" not in r.headers["X-Request-ID"]

