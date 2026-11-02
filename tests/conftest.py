import os
import tempfile

from app.devdb import local_postgres

# Tests run on PostgreSQL only: TEST_DATABASE_URL if given (it is wiped), otherwise a
# throw-away local PostgreSQL started for this run and deleted afterwards.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or local_postgres(
    tempfile.mkdtemp(prefix="crm-pg-"), "crm_test", cleanup_mode="delete")

os.environ.update({
    "DATABASE_URL": TEST_DATABASE_URL,
    "MT5_GATEWAY": "mock",
    "EMBEDDED_WORKER": "false",
    "AUTO_MIGRATE": "false",
    "LOG_DIR": tempfile.mkdtemp(prefix="crm-logs-"),
    "STORAGE_DIR": tempfile.mkdtemp(prefix="crm-test-"),
    "SECRET_KEY": "test-secret-key-that-is-long-enough-for-hs256",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db as dbmod  # noqa: E402
from app.db import Base, SessionLocal  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import AccountGroup, KycStatus, MT5Server, ServerKind  # noqa: E402
from app.mt5 import set_gateway  # noqa: E402
from app.mt5.mock import MockGateway  # noqa: E402
from app.core.ratelimit import limiter  # noqa: E402
from app.services import activity, events, ip_rules, notifications, server_health, staff as staff_svc  # noqa: E402
from app.web import syncs  # noqa: E402

ADMIN = ("admin@broker.com", "Admin-Pass1")
STRONG = "Client-Pass1"


@pytest.fixture(autouse=True)
def fresh_db():
    import app.models  # noqa: F401

    set_gateway(None)  # fresh gateway registry and login->server cache for every test
    Base.metadata.drop_all(dbmod.engine)
    Base.metadata.create_all(dbmod.engine)
    dbmod.engine.dispose()  # pooled connections cache plans for the tables we just recreated
    with SessionLocal() as s:  # every test database starts with one (simulated) MT5 server, id 1
        s.add(MT5Server(name="Test server", kind=ServerKind.simulator, terminal_name="Test-Live",
                        is_price_feed=True))
        s.commit()
    events.register_default_handlers()
    limiter.reset()
    notifications.outbox.clear()
    ip_rules.invalidate()
    server_health._banner_cache.clear()
    activity.forget()
    syncs.WAIT_SECONDS = 60  # a sync started from a page finishes before the page answers (results are asserted)
    yield


@pytest.fixture
def gw():
    g = MockGateway()
    g.connect()
    set_gateway(g)
    yield g
    set_gateway(None)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture
def api(gw):
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def admin(db):
    return staff_svc.bootstrap_admin(db, *ADMIN)


def login_staff(api, email, password, totp=None):
    r = api.post("/api/auth/staff/login", json={"email": email, "password": password, "totp_code": totp})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def login_client(api, email, password=STRONG):
    r = api.post("/api/auth/client/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def admin_h(api, admin):
    return login_staff(api, *ADMIN)


@pytest.fixture
def account_groups(db):
    live = AccountGroup(name="Standard", server_id=1, mt5_group="real\\standard", leverage_options=[100, 200, 500],
                       default_leverage=100, spread_markup_points=10)
    demo = AccountGroup(name="Demo", server_id=1, mt5_group="demo\\standard", is_demo=True,
                       leverage_options=[100, 500])
    db.add_all([live, demo])
    db.commit()
    return {"live": live.id, "demo": demo.id}


def register(api, email, ref=None, **extra):
    body = {"email": email, "password": STRONG, "first_name": "Test", "last_name": email.split("@")[0],
            "referral_code": ref, **extra}
    r = api.post("/api/auth/client/register", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def approve_kyc(api, admin_h, client_id):
    r = api.put(f"/api/clients/{client_id}/kyc-status", json={"status": KycStatus.approved.value}, headers=admin_h)
    assert r.status_code == 200, r.text


def open_live(api, admin_h, client_id, group_id, leverage=None):
    r = api.post("/api/accounts", json={"client_id": client_id, "account_group_id": group_id, "leverage": leverage},
                 headers=admin_h)
    assert r.status_code == 201, r.text
    return r.json()["account"]["login"]


@pytest.fixture
def funded(api, admin_h, account_groups, gw):
    """Factory: registered, KYC-approved client with a live account holding `amount`."""
    def make(email, amount=10000, ref=None):
        c = register(api, email, ref=ref)
        approve_kyc(api, admin_h, c["id"])
        login = open_live(api, admin_h, c["id"], account_groups["live"])
        if amount:
            gw.balance_operation(login, amount, "test funding")
        return c, login

    return make
