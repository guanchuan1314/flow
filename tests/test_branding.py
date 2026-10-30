"""Administration > Branding: the broker name and colour come from .env and can be changed in the back-office."""

import pytest

from app.core.errors import ServiceError
from app.services import branding
from tests.test_web import admin_web, form_post  # noqa: F401


@pytest.fixture(autouse=True)
def no_cache():
    branding.clear_cache()
    yield
    branding.clear_cache()


def test_defaults_come_from_settings():
    b = branding.current()
    assert b.app_name and b.accent_color.startswith("#")
    assert branding.Brand(**{**b.__dict__, "app_name": "Blue Ocean Markets"}).initials == "BO"


def test_validation(db):
    with pytest.raises(ServiceError, match="#2563eb"):
        branding.save(db, None, {"accent_color": "blue"})
    with pytest.raises(ServiceError, match="not an email"):
        branding.save(db, None, {"support_email": "nope"})


def test_save_shows_everywhere(admin_web, db):  # noqa: F811
    r = form_post(admin_web, "/admin/branding", "/admin/branding",
                  {"app_name": "Northwind FX", "accent_color": "#0F766E", "brand_tagline": ""})
    assert "Branding saved." in r.text and "Northwind FX" in r.text
    assert branding.current().accent_color == "#0f766e"
    css = admin_web.get("/brand.css")
    assert css.headers["content-type"].startswith("text/css") and "--brand:#0f766e" in css.text
    assert "Northwind FX" in admin_web.get("/").text  # the public website
    branding.save(db, None, {})  # empty fields: back to the .env defaults
    assert branding.current().app_name != "Northwind FX"
