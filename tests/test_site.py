"""Public website and redesigned client area."""

import re
from decimal import Decimal

from sqlalchemy import select

from app.models import Campaign, Client, ClientNote, ClientStatus
from tests.conftest import STRONG, register
from tests.test_web import csrf_of, web_login

PAGES = ["/", "/accounts", "/markets", "/platforms", "/copy-trading", "/partners", "/promotions", "/about",
         "/contact", "/legal/risk-disclosure", "/legal/terms", "/legal/privacy", "/legal/aml"]


def test_public_pages_render(api, account_groups, db):
    db.add(Campaign(name="Welcome", code="WELCOME50", bonus_pct=Decimal("50"), max_bonus=Decimal("500")))
    db.commit()
    for url in PAGES:
        r = api.get(url)
        assert r.status_code == 200, f"{url}: {r.status_code}"
        assert "Risk warning" in r.text  # every page carries the regulatory footer
    home = api.get("/").text
    assert "Standard" in home and "WELCOME50" in home and "EURUSD" in home
    assert api.get("/legal/unknown").status_code == 404
    assert "Go to homepage" in api.get("/no-such-page").text


def test_live_quotes(api):
    for view in ("hero", "table", "mini", "ticker"):
        r = api.get(f"/quotes?view={view}")
        assert r.status_code == 200 and "EURUSD" in r.text
    assert "1.10000" in api.get("/quotes?view=table").text  # simulator bid
    assert 'data-quotes-view="table"' in api.get("/markets").text


def test_every_site_page_shows_prices_in_the_ticker(api):
    for url in ("/about", "/platforms", "/contact", "/legal/terms"):
        page = api.get(url).text
        assert 'data-q="EURUSD"' in page and "temporarily unavailable" not in page, url


def test_prices_stream_over_websocket(api, gw):
    with api.websocket_connect("/ws/quotes") as ws:
        first = ws.receive_json()
        eurusd = next(q for q in first["quotes"] if q["symbol"] == "EURUSD")
        assert eurusd["bid"] == "1.10000" and {"ask", "spread"} <= eurusd.keys()
    assert api.get("/quotes.json").json()["quotes"]  # fallback for browsers without WebSockets


def test_contact_form_creates_lead_with_note(api, db):
    page = api.get("/contact?topic=partnership")
    r = api.post("/contact", data={"name": "Ana Costa", "email": "ana@example.com", "phone": "+351 21 000",
                                   "country": "pt", "topic": "partnership", "message": "I run a trading community.",
                                   "_csrf": csrf_of(page)})
    assert "we have received your message" in r.text
    lead = db.scalar(select(Client).where(Client.email == "ana@example.com"))
    assert lead.status == ClientStatus.lead and lead.lead_source == "website-contact" and lead.country == "PT"
    note = db.scalar(select(ClientNote).where(ClientNote.client_id == lead.id))
    assert note.staff_id is None and "trading community" in note.body
    # a second message from the same person attaches to the same client
    api.post("/contact", data={"name": "Ana", "email": "ANA@example.com", "message": "Follow-up", "_csrf": csrf_of(r)})
    assert len(db.scalars(select(ClientNote).where(ClientNote.client_id == lead.id)).all()) == 2


def test_contact_form_rejects_bots_and_requires_csrf(api, db):
    assert api.post("/contact", data={"name": "x", "email": "bot@example.com", "message": "spam"}).status_code == 403
    page = api.get("/contact")
    r = api.post("/contact", data={"name": "x", "email": "bot@example.com", "message": "spam", "website": "http://spam",
                                   "_csrf": csrf_of(page)})
    assert r.status_code == 400
    assert db.scalar(select(Client).where(Client.email == "bot@example.com")) is None


def test_header_switches_when_signed_in(api):
    assert "Open account" in api.get("/").text
    register(api, "site@x.com")
    web_login(api, "site@x.com", STRONG, base="/portal")
    assert "My account" in api.get("/").text


def test_client_area_redesign(api, account_groups):
    register(api, "area@x.com")
    web_login(api, "area@x.com", STRONG, base="/portal")
    dash = api.get("/portal").text
    assert "Get ready to trade" in dash and "1 of 4 steps complete" in dash
    for tab in ("deposit", "withdraw", "transfer", "history"):
        assert api.get(f"/portal/wallet?tab={tab}").status_code == 200
    assert "Identity verification is required" in api.get("/portal/wallet?tab=deposit").text
    trade = api.get("/portal/platforms").text
    assert "web terminal" in trade.lower()
