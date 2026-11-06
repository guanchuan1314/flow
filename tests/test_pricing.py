"""MT5 servers > Symbols & pricing: auto symbol sync and spread, commission and swap markups written to MT5."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import ServiceError
from app.models import AccountGroup, MT5Server, Symbol
from app.services import pricing, symbols
from tests.test_web import admin_web, form_post  # noqa: F401


@pytest.fixture
def server(db, gw):
    srv = db.get(MT5Server, 1)
    symbols.sync_symbols(db, srv, None, gw)
    return srv


def sym(db, name):
    return db.scalar(select(Symbol).where(Symbol.name == name))


def rules(spread=(), commission=(), swap=()):
    return pricing.parse_rules(list(spread), list(commission), list(swap))


def test_masks():
    assert pricing.matches("real\\*, !real\\vip", "real\\standard")
    assert not pricing.matches("real\\*, !real\\vip", "real\\vip")
    assert not pricing.matches("demo\\*", "real\\standard") and pricing.matches("*", "anything")


def test_markups_are_written_to_mt5_and_taken_back(db, gw, server, account_groups):
    lp_long, lp_short = gw.symbols["EURUSD"].swap_long, gw.symbols["EURUSD"].swap_short
    r = pricing.save(db, None, server, rules([("real\\*", "*", "15"), ("real\\ecn", "Forex\\*", "2")],
                                             [("real\\*, !real\\ecn", "*", "3.5")], [("EUR*", "0.5", "0.25")]),
                     False, 60, gw)
    assert "real\\standard" in r["groups"] and "EURUSD" in r["swaps"] and not r["failed"]
    assert gw.groups["real\\standard"].spreads == {"*": 15}
    assert gw.groups["real\\ecn"].spreads == {"*": 15, "Forex\\*": 2}
    assert gw.groups["demo\\standard"].spreads == {} and gw.groups["managers\\administrators"].spreads == {}
    assert [(c["path"], c["value"]) for c in gw.groups["real\\standard"].commissions] == [("*", 3.5)]
    assert gw.groups["real\\ecn"].commissions == []
    assert gw.symbols["EURUSD"].swap_long == lp_long - Decimal("0.5")
    assert gw.symbols["EURUSD"].swap_short == lp_short - Decimal("0.25")
    assert gw.symbols["XAUUSD"].swap_long == gw.symbols["XAUUSD"].swap_long  # no rule: untouched
    assert db.get(AccountGroup, account_groups["live"]).spread_markup_points == 15  # IB rebates follow

    again = pricing.apply(db, None, server, gw)
    assert again["groups"] == [] and again["swaps"] == []  # nothing left to write

    gw.symbols["EURUSD"].swap_long = Decimal("-3")  # the LP sends a new swap
    symbols.sync_symbols(db, server, None, gw)
    pricing.apply(db, None, server, gw)
    assert gw.symbols["EURUSD"].swap_long == Decimal("-3.5")  # markup on the new LP swap, not on the old result

    pricing.save(db, None, server, rules(), False, 60, gw)  # every row removed: MT5 as it was
    assert gw.groups["real\\standard"].spreads == {} and gw.groups["real\\ecn"].spreads == {}
    assert gw.groups["real\\standard"].commissions == []
    assert gw.symbols["EURUSD"].swap_long == Decimal("-3") and gw.symbols["EURUSD"].swap_short == lp_short
    assert sym(db, "EURUSD").swap_markup is None


def test_commissions_from_mt5_administrator_are_kept(db, gw, server):
    gw.groups["real\\standard"].commissions = [{"name": "5dollar", "path": "*", "value": 5.0, "per": "deal", "entry": 0}]
    pricing.save(db, None, server, rules(commission=[("real\\standard", "*", "2")]), False, 60, gw)
    assert [c["name"] for c in gw.groups["real\\standard"].commissions] == ["5dollar", "CRM *"]
    assert pricing.preview(db, server, gw)["other_commissions"] == {"real\\standard": ["5dollar (5 per deal)"]}


def test_rule_validation():
    with pytest.raises(ServiceError, match="whole points"):
        rules(spread=[("real\\*", "*", "1.5")])
    with pytest.raises(ServiceError, match="needs account groups"):
        rules(commission=[("", "*", "2")])
    with pytest.raises(ServiceError, match="not a number"):
        rules(swap=[("*", "abc", "")])
    assert rules(spread=[("", "", "")]) == {"spread": [], "commission": [], "swap": []}  # blank rows ignored


def test_auto_sync_brings_in_new_symbols_and_applies_markups(db, gw, server):
    pricing.save(db, None, server, rules(swap=[("*", "1", "1")]), True, 30, gw)
    from dataclasses import replace

    gw.symbols["NEWLP"] = replace(gw.symbols["EURUSD"], name="NEWLP", path="Forex\\NEWLP",  # created by an LP import
                                  swap_long=Decimal("-2"))
    assert pricing.run_due(db, gw) == 1
    assert sym(db, "NEWLP") is not None and gw.symbols["NEWLP"].swap_long == Decimal("-3")
    db.expire_all()
    assert "1 new" in db.get(MT5Server, 1).pricing["auto_sync"]["last_result"]
    assert pricing.run_due(db, gw) == 0  # not again before 30 minutes


def test_pricing_page(admin_web, db, gw, server):  # noqa: F811
    page, post = "/admin/import/pricing", "/admin/import/pricing/1"
    assert 'href="/admin/import/pricing"' in admin_web.get("/admin/import/history").text  # an Imports tab
    assert "Not on MT5 yet" not in admin_web.get(page).text
    r = form_post(admin_web, page, post, {"spread_groups": "real\\standard", "spread_symbols": "*", "spread_points": "12",
                                          "comm_groups": "", "comm_symbols": "", "comm_per_lot": "",
                                          "swap_symbols": "", "swap_long": "", "swap_short": "",
                                          "auto_enabled": "on", "auto_minutes": "15"})
    assert "Saved. 1 account group(s)" in r.text and gw.groups["real\\standard"].spreads == {"*": 12}
    db.expire_all()
    assert db.get(MT5Server, 1).pricing["auto_sync"] == {"enabled": True, "minutes": 15}
    r = form_post(admin_web, page, post + "/apply", {})
    assert "MT5 already has these markups" in r.text

    # the same page under Symbols, which a form goes back to
    other = "/admin/symbols/sync"
    assert admin_web.get(other).text.count('action="/admin/import/pricing/1"') == 1
    r = form_post(admin_web, other, post + "/apply", {}, headers={"referer": f"http://testserver{other}"})
    assert r.url.path == other
    run = admin_web.get("/admin/symbols/history").text
    assert "Symbols and pricing<div" in run
