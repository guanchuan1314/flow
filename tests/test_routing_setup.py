"""Routing rules, symbol sessions, bulk symbol edits, LP secrets and the trading setup page."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import ServiceError
from app.models import AuditLog, LiquidityConnection, MT5Server, RoutingRule, Symbol
from app.mt5.gateway import MT5Route
from app.services import liquidity, mt5_setup, routing_rules as rr, symbols
from tests.test_web import admin_web, form_post  # noqa: F401


@pytest.fixture
def server(db):
    return db.get(MT5Server, 1)


def names(gw):
    return [r.name for r in gw.routes]


# ---------------------------------------------------------------- routing rules
def test_routes_sync_from_mt5_both_ways_in_order(db, gw, server):
    r = rr.sync_routes(db, server, gw=gw)
    assert r["routes"] == 1 and r["created"] == ["Auto Execution"]

    gw.routes.insert(0, MT5Route(name="Reject nights", action=1003, types=3, requests=4, conditions=[
        {"condition": 4, "rule": 3, "type": 11, "value": "04:00"}]))  # added in MT5 Administrator
    gw.routes[1].enabled = False
    r = rr.sync_routes(db, server, gw=gw)
    assert r["created"] == ["Reject nights"] and r["updated"] == ["Auto Execution"]
    assert [(x.name, x.position, x.enabled) for x in rr.rules(db, 1)] == [("Reject nights", 0, True),
                                                                          ("Auto Execution", 1, False)]
    del gw.routes[0]
    assert rr.sync_routes(db, server, gw=gw)["removed"] == ["Reject nights"]


def test_rule_create_update_move_delete_go_to_mt5(db, gw, server):
    rr.sync_routes(db, server, gw=gw)
    rule = rr.create_rule(db, None, server_id=1, name="Delay XAU", values={
        "action": 0, "param": "500", "types": 255, "requests": rr.ALL_REQUESTS,
        "conditions": [{"condition": 1, "rule": 0, "type": 21, "value": "XAU*"}]})
    assert names(gw) == ["Delay XAU", "Auto Execution"]  # at the top
    assert gw.routes[0].param == "500" and gw.routes[0].param_type == 3 and rule.position == 0

    rr.update_rule(db, None, rule, {"conditions": rule.conditions + [
        {"condition": 4, "rule": 5, "type": 11, "value": "07:30"}], "enabled": False})
    assert gw.routes[0].conditions[1]["value"] == "07:30" and not gw.routes[0].enabled

    rr.move_rule(db, None, rule, "down")
    assert names(gw) == ["Auto Execution", "Delay XAU"]
    assert [(x.name, x.position) for x in rr.rules(db, 1)] == [("Auto Execution", 0), ("Delay XAU", 1)]

    rr.delete_rule(db, None, rule)
    assert names(gw) == ["Auto Execution"] and len(rr.rules(db, 1)) == 1


def test_bad_rules_are_refused_before_mt5(db, gw, server):
    rr.sync_routes(db, server, gw=gw)
    for values, error in (({"action": 1001}, "Choose the gateway"),
                          ({"action": 1003, "conditions": [{"condition": 4, "rule": 0, "type": 11, "value": "4am"}]},
                           "HH:MM"),
                          ({"action": 1003, "types": 0}, "order type"),
                          ({"action": 0, "param": "soon"}, "whole number")):
        with pytest.raises(ServiceError, match=error):
            rr.create_rule(db, None, server_id=1, name="Bad", values=values, gw=gw)
    assert names(gw) == ["Auto Execution"]


def test_connect_lp_in_one_step_and_keep_secrets_out_of_the_crm(db, gw, server):
    """Gateway + translations for every symbol + a routing rule sending the real groups to it."""
    symbols.sync_symbols(db, server, gw=gw)
    rr.sync_routes(db, server, gw=gw)
    c = liquidity.create_connection(
        db, None, server_id=1, kind="gateway", name="LMAX live", module="LMAXGateway64.exe", password="lp-pass",
        fields={"params": {"FIX Market Data Password": "md-secret", "Quotes Delay": "0"}},
        map_symbols=True, route_groups=["real\\*"], route_symbols=["XAU*"])
    on_mt5 = gw.links[("gateway", "LMAX live")]
    assert len(on_mt5.translations) == 9 and on_mt5.params["FIX Market Data Password"] == "md-secret"
    assert c.params["FIX Market Data Password"] == "" and liquidity.secret_params(c) == ["FIX Market Data Password"]

    rule = gw.routes[0]
    assert rule.name == "To LMAX live" and rule.action == rr.ACTION_DEALER
    assert rule.dealers == [{"login": on_mt5.gateway_login, "name": "LMAX live"}]
    assert rule.conditions == [{"condition": 1001, "rule": 0, "type": 20, "value": "real\\*"},
                               {"condition": 1, "rule": 0, "type": 21, "value": "XAU*"}]
    assert liquidity.routes_to(db, c) == ["To LMAX live"]
    desc = rr.describe(rr.rules(db, 1)[0], rr.gateway_targets(db, 1))
    assert desc["action"] == "Send to gateway LMAX live" and desc["final"]

    # editing with the secret left blank keeps MT5's value; a new one replaces it
    liquidity.update_connection(db, None, c, {"params": {**c.params, "Quotes Delay": "5"}})
    assert gw.links[("gateway", "LMAX live")].params["FIX Market Data Password"] == "md-secret"
    liquidity.update_connection(db, None, c, {"params": {**c.params, "FIX Market Data Password": "new-secret"}})
    assert gw.links[("gateway", "LMAX live")].params["FIX Market Data Password"] == "new-secret"
    stored = [repr(x.params) for x in db.scalars(select(LiquidityConnection))] + \
             [repr(a.details) for a in db.scalars(select(AuditLog))]
    assert not any(s in text for text in stored for s in ("md-secret", "new-secret", "lp-pass"))

    with pytest.raises(ServiceError, match="Routing rules still send orders"):
        liquidity.delete_connection(db, None, c)
    assert ("gateway", "LMAX live") in gw.links


def test_simulator_offers_every_standard_module(gw):
    gateways, feeds = gw.get_link_modules("gateway"), gw.get_link_modules("feed")
    assert len(gateways) == 19 and len(feeds) == 20
    groups = dict(liquidity.grouped_modules(gateways))
    assert {m.name for m in groups["Exchanges"]} >= {"ASX Gateway", "MOEX Derivatives Gateway"}
    assert "LMAX Gateway" in {m.name for m in groups["FX liquidity and brokers"]}
    assert not any(v for m in gateways + feeds for k, v in m.params.items() if liquidity.is_secret(k, v))


# ---------------------------------------------------------------- symbols: sessions, bulk
def test_symbol_sessions_round_trip(db, gw, server):
    symbols.sync_symbols(db, server, gw=gw)
    sym = db.scalar(select(Symbol).where(Symbol.name == "EURUSD"))
    assert symbols.format_sessions(sym.sessions["trade"])[1] == "00:00-24:00"
    week = symbols.parse_sessions(["", "01:00-22:15, 22:30-24:00", "00:00-24:00", "00:00-24:00", "00:00-24:00",
                                   "00:00-21:00", ""])
    symbols.update_symbol(db, None, sym, {"sessions": {"quote": week, "trade": week}})
    assert gw.symbols["EURUSD"].sessions["trade"][1] == [[60, 1335], [1350, 1440]]
    with pytest.raises(ServiceError, match="without overlapping"):
        symbols.update_symbol(db, None, sym, {"sessions": {"quote": week, "trade": [[[0, 600], [500, 900]]] * 7}})
    with pytest.raises(ServiceError, match="HH:MM"):
        symbols.parse_sessions(["9am-5pm"] + [""] * 6)
    assert gw.symbols["EURUSD"].sessions["trade"][5] == [[0, 1260]]


def test_bulk_symbol_edit(db, gw, server, admin_web):
    symbols.sync_symbols(db, server, gw=gw)
    ids = [str(s.id) for s in db.scalars(select(Symbol).where(Symbol.name.in_(["EURUSD", "GBPUSD"])))]
    r = form_post(admin_web, "/admin/trading-setup/symbols?server=1", "/admin/trading-setup/symbols/bulk",
                  {"server_id": "1", "ids": ids, "trade_mode": "3", "spread": "15", "volume_max": ""})
    assert "Saved on MT5: 2 symbols" in r.text
    assert gw.symbols["EURUSD"].trade_mode == gw.symbols["GBPUSD"].trade_mode == 3 and gw.symbols["GBPUSD"].spread == 15
    assert gw.symbols["USDJPY"].trade_mode == 4  # not ticked
    r = form_post(admin_web, "/admin/trading-setup/symbols?server=1", "/admin/trading-setup/symbols/bulk",
                  {"server_id": "1", "ids": ids, "volume_min": "500"})
    assert "Nothing saved" in r.text and gw.symbols["EURUSD"].volume_min == Decimal("0.01")


# ---------------------------------------------------------------- screens
def test_routing_screens(admin_web, db, gw, server):
    r = form_post(admin_web, "/admin/trading-setup/routing", "/admin/trading-setup/routing/sync", {"server_id": "1"})
    assert "1 routing rules on MT5, 1 new" in r.text and "Auto Execution" in r.text
    liquidity.sync_liquidity(db, server, gw=gw)
    gwy = liquidity.create_connection(db, None, server_id=1, kind="gateway", name="Prime", module="IntegralGateway64.exe",
                                      fields={})
    page = admin_web.get(f"/admin/trading-setup/routing/new?server=1&gateway={gwy.id}")
    assert page.status_code == 200 and f'value="{gwy.id}" selected' in page.text
    r = form_post(admin_web, "/admin/trading-setup/routing/new?server=1", "/admin/trading-setup/routing/new", {
        "server_id": "1", "name": "Real to Prime", "at_top": "on", "enabled": "on",
        "order_type": ["1", "2"], "request_mode": "all", "action": "1001", "target": str(gwy.id),
        "cond": ["1001", "", ""], "cond_rule": ["0", "0", "0"], "cond_value": ["real\\*", "", ""]})
    assert "Routing rule Real to Prime created on MT5" in r.text and "Send to gateway Prime" in r.text
    assert gw.routes[0].types == 3 and gw.routes[0].dealers[0]["login"] == gwy.gateway_login
    rule = db.scalar(select(RoutingRule).where(RoutingRule.name == "Real to Prime"))
    assert "real\\*" in admin_web.get(f"/admin/trading-setup/routing/{rule.id}").text

    r = form_post(admin_web, "/admin/trading-setup/routing", f"/admin/trading-setup/routing/{rule.id}/move", {"direction": "down"})
    assert names(gw) == ["Auto Execution", "Real to Prime"]
    form_post(admin_web, "/admin/trading-setup/routing", f"/admin/trading-setup/routing/{rule.id}/toggle", {})
    assert not gw.routes[1].enabled
    r = form_post(admin_web, f"/admin/trading-setup/routing/{rule.id}", f"/admin/trading-setup/routing/{rule.id}", {
        "order_type": ["1"], "request_mode": "chosen", "request_kind": ["8", "4"], "action": "1003",
        "cond": ["4"], "cond_rule": ["3"], "cond_value": ["25:00"]})
    assert "HH:MM" in r.text and gw.routes[1].action == 1001  # refused: MT5 untouched
    r = form_post(admin_web, f"/admin/trading-setup/routing/{rule.id}", f"/admin/trading-setup/routing/{rule.id}", {
        "enabled": "on", "order_type": ["1"], "request_mode": "chosen", "request_kind": ["8", "4"], "action": "1003",
        "cond": ["4"], "cond_rule": ["3"], "cond_value": ["22:00"]})
    assert "saved on MT5" in r.text
    assert gw.routes[1].action == 1003 and gw.routes[1].requests == 12 and gw.routes[1].dealers == []
    r = form_post(admin_web, f"/admin/trading-setup/routing/{rule.id}", f"/admin/trading-setup/routing/{rule.id}/delete", {})
    assert "deleted on MT5" in r.text and names(gw) == ["Auto Execution"]


def test_trading_setup_page_and_sync_everything(admin_web, db, gw, server):
    r = form_post(admin_web, "/admin/trading-setup/overview", "/admin/trading-setup/overview/sync", {"server_id": "1"})
    assert "synced from MT5: 9 symbols, 0 gateways, 1 data feeds, 1 routing rules" in r.text
    assert "MetaTrader 5 Demo Feeder" in r.text and "Auto Execution" in r.text
    liquidity.create_connection(db, None, server_id=1, kind="gateway", name="Unrouted", module="LMAXGateway64.exe",
                                fields={})
    o = mt5_setup.overview(db, server)
    assert "No enabled routing rule sends orders to gateway Unrouted." in o["warnings"]
    page = admin_web.get("/admin/trading-setup/liquidity/new?server=1&kind=gateway")
    assert "Exchanges" in page.text and "Borsa Istanbul VIOP Gateway" in page.text
