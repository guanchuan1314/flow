"""MT5 > Symbols & pricing > Website prices: which symbols the website streams, in which order and name."""

import pytest

from app.core.errors import ServiceError
from app.models import MT5Server
from app.services import app_settings, market, symbols
from tests.test_web import admin_web, form_post  # noqa: F401


@pytest.fixture(autouse=True)
def fresh_cache():
    def clear():
        app_settings.clear()
        market._cache.pop("instruments", None)
        market._cache["at"] = 0.0
    clear()
    yield
    clear()


@pytest.fixture
def synced(db, gw):
    symbols.sync_symbols(db, db.get(MT5Server, 1), None, gw)
    return db


def test_defaults_to_public_symbols(db):
    assert market.symbols()[:2] == ["EURUSD", "GBPUSD"]
    assert market.website_form(db)["from_env"] is True


def test_saved_list_sets_order_names_and_mt5_digits(synced):
    market.save_website_symbols(synced, None, [{"symbol": "XAUUSD", "name": "Gold spot", "category": ""},
                                               {"symbol": "EURUSD", "name": "", "category": "FX majors"}])
    quotes = market.quotes()
    assert [q["symbol"] for q in quotes] == ["XAUUSD", "EURUSD"]
    assert quotes[0]["name"] == "Gold spot" and quotes[0]["category"] == "Metals"  # built-in category kept
    assert quotes[1]["name"] == "Euro / US Dollar" and quotes[1]["category"] == "FX majors"
    assert quotes[1]["bid"] == "1.10000"  # decimals from the MT5 symbol
    form = market.website_form(synced)
    assert form["from_env"] is False and "EURUSD" in form["available"]


def test_validation(synced):
    with pytest.raises(ServiceError, match="not a symbol on the MT5 server"):
        market.save_website_symbols(synced, None, [{"symbol": "NOPE"}])
    with pytest.raises(ServiceError, match="listed twice"):
        market.save_website_symbols(synced, None, [{"symbol": "EURUSD"}, {"symbol": "EURUSD"}])
    with pytest.raises(ServiceError, match="at least one"):
        market.save_website_symbols(synced, None, [{"symbol": ""}])


def test_pricing_page_saves_the_list(admin_web, synced):  # noqa: F811
    page = admin_web.get("/admin/server-tools/pricing").text
    assert "Website prices" in page and 'data-add-row="web-tpl"' in page and "data-sortable" in page
    r = form_post(admin_web, "/admin/server-tools/pricing", "/admin/server-tools/pricing/website",
                  {"web_symbol": ["GBPUSD", "EURUSD"], "web_name": ["", ""], "web_category": ["", ""]})
    assert "Website prices saved: 2 symbol(s)" in r.text
    assert [q["symbol"] for q in admin_web.get("/quotes.json").json()["quotes"]] == ["GBPUSD", "EURUSD"]
