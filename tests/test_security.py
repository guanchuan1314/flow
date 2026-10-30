import time

import pyotp
from sqlalchemy import select

from app.models import AuditLog, Role
from tests.conftest import ADMIN, STRONG, login_staff, register


def test_login_and_me(api, admin_h):
    r = api.get("/api/auth/staff/me", headers=admin_h)
    assert r.status_code == 200
    assert r.json()["email"] == ADMIN[0]


def test_unauthenticated_and_wrong_audience(api, admin):
    assert api.get("/api/clients").status_code == 401
    register(api, "c1@x.com")
    r = api.post("/api/auth/client/login", json={"email": "c1@x.com", "password": STRONG})
    client_h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    # a client token must not open staff endpoints
    assert api.get("/api/clients", headers=client_h).status_code == 401


def test_lockout_after_failed_logins(api, admin):
    for _ in range(5):
        r = api.post("/api/auth/staff/login", json={"email": ADMIN[0], "password": "wrong"})
        assert r.status_code == 401
    r = api.post("/api/auth/staff/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    assert r.status_code == 423  # locked even with correct password


def test_logout_revokes_token(api, admin_h):
    assert api.post("/api/auth/staff/logout", headers=admin_h).status_code == 204
    assert api.get("/api/auth/staff/me", headers=admin_h).status_code == 401


def test_totp_flow(api, admin_h):
    secret = api.post("/api/auth/staff/2fa/enroll", headers=admin_h).json()["secret"]
    assert api.post("/api/auth/staff/2fa/confirm", json={"code": "000000"}, headers=admin_h).status_code == 400
    code = pyotp.TOTP(secret).now()
    assert api.post("/api/auth/staff/2fa/confirm", json={"code": code}, headers=admin_h).status_code == 204
    r = api.post("/api/auth/staff/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    assert r.status_code == 401 and "2FA" in r.json()["detail"]
    # the confirm code was used: logging in needs a newer one (the next 30 s step is accepted early)
    assert api.post("/api/auth/staff/login", json={"email": ADMIN[0], "password": ADMIN[1],
                                                   "totp_code": code}).status_code == 401
    login_staff(api, *ADMIN, totp=pyotp.TOTP(secret).at(time.time() + 30))


def test_rbac_sales_cannot_manage_staff(api, admin_h, db):
    sales_role = db.scalar(select(Role).where(Role.name == "Sales"))
    r = api.post("/api/staff", json={"email": "sales@b.com", "full_name": "Sam Sales", "password": "Sales-Pass1",
                                     "role_ids": [sales_role.id]}, headers=admin_h)
    assert r.status_code == 201
    sales_h = login_staff(api, "sales@b.com", "Sales-Pass1")
    assert api.get("/api/staff", headers=sales_h).status_code == 403
    assert api.get("/api/clients", headers=sales_h).status_code == 200


def test_ip_allowlist(api, admin_h, db):
    role = db.scalar(select(Role).where(Role.name == "Support"))
    api.post("/api/staff", json={"email": "ip@b.com", "full_name": "IP Locked", "password": "Ip-Locked1",
                                 "role_ids": [role.id], "allowed_ips": ["10.0.0.0/8"]}, headers=admin_h)
    r = api.post("/api/auth/staff/login", json={"email": "ip@b.com", "password": "Ip-Locked1"})
    assert r.status_code == 403  # TestClient connects from "testclient", not 10.x


def test_password_policy(api, admin_h):
    r = api.post("/api/staff", json={"email": "weak@b.com", "full_name": "Weak", "password": "weak"}, headers=admin_h)
    assert r.status_code == 400


def test_unknown_permission_rejected(api, admin_h):
    r = api.post("/api/roles", json={"name": "Bad", "permissions": ["nope.nothing"]}, headers=admin_h)
    assert r.status_code == 400


def test_audit_log_records_actions(api, admin_h, db):
    register(api, "audited@x.com")
    r = api.get("/api/security/audit", params={"action": "client.register"}, headers=admin_h)
    assert r.status_code == 200 and len(r.json()) == 1
    failed = api.get("/api/security/login-attempts", params={"failed_only": True}, headers=admin_h).json()
    assert isinstance(failed, list)
    assert db.scalar(select(AuditLog).where(AuditLog.action == "auth.login")) is not None
