"""Administration > Website prices: which symbols the website streams, in which order and name."""

import re

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


def test_own_scripts_and_styles_change_address_with_the_file(admin_web):  # noqa: F811
    page = admin_web.get("/admin/website-prices").text
    script = re.search(r'src="(/static/app\.js\?v=\d+)"', page).group(1)
    assert re.search(r'href="/static/app\.css\?v=\d+"', page) and admin_web.get(script).status_code == 200
    assert re.search(r'href="/static/site\.css\?v=\d+"', admin_web.get("/").text)


def test_validation(synced):
    with pytest.raises(ServiceError, match="not a symbol on the MT5 server"):
        market.save_website_symbols(synced, None, [{"symbol": "NOPE"}])
    with pytest.raises(ServiceError, match="listed twice"):
        market.save_website_symbols(synced, None, [{"symbol": "EURUSD"}, {"symbol": "EURUSD"}])
    with pytest.raises(ServiceError, match="at least one"):
        market.save_website_symbols(synced, None, [{"symbol": ""}])


def test_website_prices_page_saves_the_list(admin_web, synced):  # noqa: F811
    url = "/admin/website-prices"
    page = admin_web.get(url).text
    assert 'data-add-row="web-tpl"' in page and "data-sortable" in page
    assert '<select class="form-select form-select-sm mono" name="web_symbol" data-unique-from="web-symbols"' in page
    assert '<template id="web-symbols"><option value="' in page and "mt5-symbols" not in page
    nav = page[page.find('<nav class="nav'):page.find("</nav>")]
    assert nav.find(">Administration<") < nav.find('href="/admin/website-prices"')
    assert 'data-add-row="web-tpl"' not in admin_web.get("/admin/import/pricing").text  # no longer there

    twice = {"web_symbol": ["EURUSD", "EURUSD"], "web_name": ["", ""], "web_category": ["", ""]}
    assert "EURUSD is listed twice" in form_post(admin_web, url, url, twice).text
    r = form_post(admin_web, url, url, {"web_symbol": ["GBPUSD", "EURUSD"], "web_name": ["", ""], "web_category": ["", ""]})
    assert "Website prices saved: 2 symbol(s)" in r.text and r.url.path == url
    assert [q["symbol"] for q in admin_web.get("/quotes.json").json()["quotes"]] == ["GBPUSD", "EURUSD"]
