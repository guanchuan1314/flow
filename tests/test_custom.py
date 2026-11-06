"""Custom modules (app.custom): the sample in docs/custom, and a module that fails."""

import shutil
import subprocess
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import custom
from app.config import get_settings
from app.core.permissions import PERMISSIONS
from app.main import create_app
from app.models import Deal
from app.services import events, staff as staff_svc
from app.web.common import templates
from tests.conftest import ADMIN
from tests.test_web import form_post, web_login

SAMPLE = Path(__file__).parent.parent / "docs" / "custom" / "big_deals"
URL = "/admin/custom/big_deals"


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """An empty custom/ folder; afterwards the process is as if no module had ever loaded."""
    loader, perms = templates.env.loader, dict(PERMISSIONS)
    monkeypatch.setattr(get_settings(), "custom_dir", str(tmp_path))
    monkeypatch.setattr(custom, "_loaded", False)
    monkeypatch.setattr(custom, "modules", [])
    monkeypatch.setattr(custom, "failed", {})
    yield tmp_path
    templates.env.loader = loader
    templates.env.cache.clear()
    PERMISSIONS.clear()
    PERMISSIONS.update(perms)
    events.unregister("custom.big_deals.0")


@pytest.fixture
def sample(folder, db):
    shutil.copytree(SAMPLE, folder / "big_deals")
    db.execute(text("DROP TABLE IF EXISTS custom_big_deals, custom_migrations"))
    db.commit()
    yield folder
    db.rollback()
    db.execute(text("DROP TABLE IF EXISTS custom_big_deals, custom_migrations"))
    db.commit()


def client(admin=None):
    c = TestClient(create_app())
    if admin:
        web_login(c, *ADMIN)
    return c


def deal(ticket, lots):
    return Deal(server_id=1, ticket=ticket, login=500, time=datetime(2030, 1, 1), action=0, entry=0, symbol="EURUSD",
                volume=Decimal(lots))


def test_sample_module_plugs_in_everywhere(sample, gw, admin, db):
    custom.migrate()
    custom.migrate()  # each file runs once: the second time the table is already there and nothing fails
    c = client(admin)
    assert not custom.failed and db.scalar(text("SELECT count(*) FROM custom_migrations")) == 1

    page = c.get(URL)
    assert page.status_code == 200 and "No big deals yet" in page.text
    assert 'href="/admin/custom/big_deals"><i class="bi bi-trophy"></i>Big deals' in page.text
    assert ">Desk<" in page.text and "/static/custom/big_deals/big_deals.css" in page.text
    assert ".big-deal" in c.get("/static/custom/big_deals/big_deals.css").text
    assert ">Big deals<" not in c.get("/admin/custom/nothing").text  # 404: not swallowed by a module
    assert "big_deals.view" in c.get("/admin/staff/roles/new").text

    events.publish(db, [deal(1, "12"), deal(1, "12"), deal(2, "0.5")])  # a repeat, and one too small
    assert db.scalar(text("SELECT count(*) FROM custom_big_deals")) == 1
    assert ">12<" in c.get(URL).text

    labels = [label for label, _ in custom.steps()]
    assert labels == ["custom big_deals: forget_old"]
    step = custom.steps()[0][1]
    step(db)
    db.execute(text("INSERT INTO custom_big_deals VALUES (3, 500, 'EURUSD', 20, '2001-01-01')"))
    db.commit()
    step(db)  # not due again yet
    assert db.scalar(text("SELECT count(*) FROM custom_big_deals")) == 2


def test_module_pages_need_a_login_and_the_permission(sample, gw, admin, db):
    custom.migrate()
    c = client()
    r = c.get(URL, follow_redirects=False)
    assert r.status_code == 303 and "/admin/login" in r.headers["location"]

    role = staff_svc.create_role(db, admin, "No access", ["clients.view"])
    staff_svc.create_staff(db, admin, email="none@b.com", full_name="None", password="Staff-Pass1", role_ids=[role.id])
    web_login(c, "none@b.com", "Staff-Pass1")
    assert c.get(URL).status_code == 403
    assert "Big deals" not in c.get("/admin").text
    assert '>Desk<' not in c.get("/admin").text


def test_all_kinds_of_address_and_csrf(folder, gw, admin):
    module = folder / "acme"
    module.mkdir()
    (module / "__init__.py").write_text('''
from fastapi import APIRouter

admin, portal, public = APIRouter(), APIRouter(), APIRouter()

@admin.post("/save")
def save():
    return {"saved": True}

@portal.get("/me")
def me():
    return {"who": "client"}

@public.post("/callback")
def callback():
    return {"open": True}

def setup(crm):
    crm.add_admin_routes(admin)
    crm.add_portal_routes(portal)
    crm.add_public_routes(public)
''')
    c = client(admin)
    assert c.post("/custom/acme/callback").json() == {"open": True}
    assert c.post("/admin/custom/acme/save").status_code == 403  # no CSRF token
    assert form_post(c, "/admin", "/admin/custom/acme/save", {}).json() == {"saved": True}
    r = TestClient(c.app).get("/portal/custom/acme/me", follow_redirects=False)
    assert r.status_code == 303 and "/portal/login" in r.headers["location"]


def test_a_template_replaces_ours(folder, gw, admin):
    pages = folder / "acme" / "templates" / "admin"
    pages.mkdir(parents=True)
    (folder / "acme" / "__init__.py").write_text("def setup(crm):\n    pass\n")
    (pages / "website_prices.html").write_text("Acme prices page")
    assert "Acme prices page" in client(admin).get("/admin/website-prices").text


@pytest.mark.parametrize("name, code", [
    ("crash", "raise RuntimeError('boom')"),
    ("nosetup", "x = 1"),
    ("clash", "def setup(crm):\n    crm.add_permission('clients.view', 'mine')"),
    ("half", "def setup(crm):\n    crm.add_permission('half.view', 'mine')\n    crm.add_menu('Half', '/h')\n    1 / 0"),
    ("Bad-Name", "def setup(crm):\n    pass"),
])
def test_a_broken_module_is_left_out_and_shown(folder, gw, admin, name, code):
    (folder / name).mkdir()
    (folder / name / "__init__.py").write_text(code)
    c = client(admin)
    assert list(custom.failed) == [name] and not custom.modules
    page = c.get("/admin").text
    assert f"Custom module {name}" in page and "Half" not in page
    assert "half.view" not in PERMISSIONS and PERMISSIONS["clients.view"] != "mine"


def test_failed_migration_rolls_back_and_is_shown(folder, db, gw, admin):
    module = folder / "bad"
    (module / "migrations").mkdir(parents=True)
    (module / "__init__.py").write_text("def setup(crm):\n    pass\n")
    (module / "migrations" / "001_ok.sql").write_text("CREATE TABLE custom_half (id int);")
    (module / "migrations" / "002_bad.sql").write_text("CREATE TABLE nonsense (;")
    custom.migrate()
    assert "bad" in custom.failed
    assert db.scalar(text("SELECT to_regclass('custom_half')")) is None


def test_modules_can_be_switched_off(sample, gw, admin, monkeypatch):
    monkeypatch.setattr(get_settings(), "custom_enabled", False)
    assert client(admin).get(URL).status_code == 404 and not custom.modules


def test_git_leaves_custom_modules_alone():
    root = Path(__file__).parent.parent
    ignored = subprocess.run(["git", "check-ignore", "custom/acme/__init__.py"], cwd=root, capture_output=True)
    kept = subprocess.run(["git", "check-ignore", "custom/README.md"], cwd=root, capture_output=True)
    assert ignored.returncode == 0 and kept.returncode == 1
