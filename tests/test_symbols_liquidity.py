"""Symbols and liquidity providers (gateways, data feeds): kept in step with MT5 both ways."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import ServiceError
from app.models import LiquidityConnection, MT5Server, Symbol
from app.mt5 import BUY
from app.services import liquidity, servers, symbols
from tests.test_ops import trader  # noqa: F401
from tests.test_web import admin_web, form_post  # noqa: F401


@pytest.fixture
def server(db):
    return db.get(MT5Server, 1)


# ---------------------------------------------------------------- symbols
def test_symbols_sync_from_mt5_both_ways(db, gw, server):
    r = symbols.sync_symbols(db, server, gw=gw)
    assert r["symbols"] == len(r["created"]) == 9 and not r["updated"]
    eur = db.scalar(select(Symbol).where(Symbol.name == "EURUSD"))
    assert eur.path == "Forex\\EURUSD" and eur.digits == 5 and eur.volume_min == Decimal("0.01")

    gw.symbols["EURUSD"].spread = 12  # changed in MT5 Administrator
    del gw.symbols["NAS100"]  # deleted in MT5 Administrator
    r = symbols.sync_symbols(db, server, gw=gw)
    assert r["updated"] == ["EURUSD"] and r["removed"] == ["NAS100"]
    db.refresh(eur)
    assert eur.spread == 12 and db.scalar(select(Symbol).where(Symbol.name == "NAS100")) is None


def test_symbol_create_update_delete_go_to_mt5(db, gw, server):
    symbols.sync_symbols(db, server, gw=gw)
    sym = symbols.create_symbol(db, None, server_id=1, name="EURUSD.ecn", template="EURUSD",
                                changes={"description": "Euro ECN", "spread": 0, "volume_min": Decimal("0.1"),
                                         "swap_3day": 5})
    on_mt5 = gw.symbols["EURUSD.ecn"]
    assert on_mt5.path == "Forex\\EURUSD.ecn" and on_mt5.description == "Euro ECN"
    assert on_mt5.contract_size == Decimal("100000") and on_mt5.volume_min == Decimal("0.1")  # rest copied
    assert sym.volume_min == Decimal("0.1") and sym.swap_3day == 5

    symbols.update_symbol(db, None, sym, {"trade_mode": 3, "swap_long": Decimal("-7.25")})
    assert gw.symbols["EURUSD.ecn"].trade_mode == 3 and gw.symbols["EURUSD.ecn"].swap_long == Decimal("-7.25")
    with pytest.raises(ServiceError, match="Volumes"):
        symbols.update_symbol(db, None, sym, {"volume_max": Decimal("0.01")})
    assert gw.symbols["EURUSD.ecn"].volume_max == Decimal("100")  # MT5 untouched when the CRM refuses

    symbols.delete_symbol(db, None, sym)
    assert "EURUSD.ecn" not in gw.symbols and db.scalar(select(Symbol).where(Symbol.name == "EURUSD.ecn")) is None


def test_mt5_refusal_leaves_the_crm_unchanged(db, gw, server):
    symbols.sync_symbols(db, server, gw=gw)
    with pytest.raises(ServiceError, match="already exists"):
        symbols.create_symbol(db, None, server_id=1, name="XAUUSD", template="EURUSD", changes={})
    sym = db.scalar(select(Symbol).where(Symbol.name == "GBPUSD"))
    del gw.symbols["GBPUSD"]
    with pytest.raises(ServiceError, match="not found"):
        symbols.update_symbol(db, None, sym, {"spread": 30})
    db.refresh(sym)
    assert sym.spread == 0


def test_symbol_with_open_trades_cannot_be_deleted(db, gw, server, trader):
    symbols.sync_symbols(db, server, gw=gw)
    from app.services import sync
    gw.open_position(trader, "XAUUSD", BUY, Decimal("1"))
    sync.full_sync(db, gw)
    sym = db.scalar(select(Symbol).where(Symbol.name == "XAUUSD"))
    with pytest.raises(ServiceError, match="1 open positions"):
        symbols.delete_symbol(db, None, sym)
    assert "XAUUSD" in gw.symbols


def test_symbol_screens(admin_web, db, gw, server):
    r = form_post(admin_web, "/admin/trading-setup/symbols", "/admin/trading-setup/symbols/sync", {"server_id": "1"})
    assert "9 symbols on MT5, 9 new" in r.text and "EURUSD" in r.text
    r = form_post(admin_web, "/admin/trading-setup/symbols/new?server=1", "/admin/trading-setup/symbols/new",
                  {"server_id": "1", "name": "GOLD", "template": "XAUUSD", "description": "Gold spot", "digits": ""})
    assert "Symbol GOLD created on MT5" in r.text and gw.symbols["GOLD"].digits == 2
    sym = db.scalar(select(Symbol).where(Symbol.name == "GOLD"))
    page = admin_web.get(f"/admin/trading-setup/symbols/{sym.id}")
    assert page.status_code == 200 and "Gold spot" in page.text
    r = form_post(admin_web, f"/admin/trading-setup/symbols/{sym.id}", f"/admin/trading-setup/symbols/{sym.id}",
                  {"description": "Gold vs dollar", "spread": "25", "trade_mode": "1"})
    assert "saved on MT5" in r.text and gw.symbols["GOLD"].spread == 25 and gw.symbols["GOLD"].trade_mode == 1
    r = form_post(admin_web, f"/admin/trading-setup/symbols/{sym.id}", f"/admin/trading-setup/symbols/{sym.id}/delete", {})
    assert "GOLD deleted on MT5" in r.text and "GOLD" not in gw.symbols


# ---------------------------------------------------------------- liquidity providers
def test_liquidity_sync_create_update_delete(db, gw, server):
    r = liquidity.sync_liquidity(db, server, gw=gw)
    assert r["feeds"] == 1 and r["gateways"] == 0 and r["created"] == ["MetaTrader 5 Demo Feeder"]

    gwy = liquidity.create_connection(
        db, None, server_id=1, kind="gateway", name="LMAX live", module="LMAXGateway64.exe", password="lp-secret",
        fields={"lp_login": "broker01", "groups": ["real\\*"],
                "params": {"FIX Trade TargetCompID": "LMXBD", "FIX Trade SenderCompID": "BRK1"}})
    on_mt5 = gw.links[("gateway", "LMAX live")]
    assert on_mt5.server == "fix-order.london-demo.lmax.com:443"  # the module's default address
    assert on_mt5.password == "lp-secret" and on_mt5.groups == ["real\\*"] and on_mt5.mt5_id == 1
    assert on_mt5.params["FIX Trade SenderCompID"] == "BRK1" and on_mt5.gateway_server.startswith("127.0.0.1:")
    assert gwy.gateway_type == "MT5GWTLMAX" and not hasattr(gwy, "password")  # the CRM keeps no LP password

    liquidity.update_connection(db, None, gwy, {"enabled": False, "translations": [
        {"source": "EUR/USD", "symbol": "EURUSD", "digits": 5, "bid_markup": 0, "ask_markup": 2}]})
    on_mt5 = gw.links[("gateway", "LMAX live")]
    assert not on_mt5.enabled and on_mt5.translations[0]["ask_markup"] == 2
    assert on_mt5.password == "lp-secret"  # kept: no new password given

    gw.links[("gateway", "LMAX live")].login = "broker02"  # changed in MT5 Administrator
    liquidity.sync_liquidity(db, server, gw=gw)
    db.refresh(gwy)
    assert gwy.lp_login == "broker02"

    liquidity.delete_connection(db, None, gwy)
    assert ("gateway", "LMAX live") not in gw.links
    assert db.scalar(select(LiquidityConnection).where(LiquidityConnection.name == "LMAX live")) is None

    with pytest.raises(ServiceError, match="not installed"):
        liquidity.create_connection(db, None, server_id=1, kind="feed", name="X", module="Nope.exe", fields={})


def test_liquidity_screens(admin_web, db, gw, server):
    r = form_post(admin_web, "/admin/trading-setup/liquidity?kind=feed", "/admin/trading-setup/liquidity/sync", {"server_id": "1", "kind": "feed"})
    assert "0 gateways and 1 data feeds on MT5" in r.text and "MetaTrader 5 Demo Feeder" in r.text
    page = admin_web.get("/admin/trading-setup/liquidity/new?server=1&kind=feed&module=DXFeeder64.exe")
    assert "demo.dxfeed.com:7300" in page.text and "Download History = No" in page.text
    r = form_post(admin_web, "/admin/trading-setup/liquidity/new?server=1&kind=feed&module=DXFeeder64.exe", "/admin/trading-setup/liquidity/new",
                  {"server_id": "1", "kind": "feed", "module": "DXFeeder64.exe", "name": "dxFeed", "enabled": "on",
                   "lp_server": "demo.dxfeed.com:7300", "flag": "1", "symbols": "Forex\\*\nMetals\\*",
                   "translations": "EUR/USD, EURUSD, 5\nXAU/USD, XAUUSD, 2, 1, 1",
                   "params": "Download History = Yes\nQuotes Delay = 0"})
    assert "Data feed dxFeed created on MT5" in r.text
    feed = gw.links[("feed", "dxFeed")]
    assert feed.symbols == ["Forex\\*", "Metals\\*"] and feed.params["Download History"] == "Yes"
    assert feed.translations[1] == {"source": "XAU/USD", "symbol": "XAUUSD", "digits": 2, "bid_markup": 1, "ask_markup": 1}
    c = db.scalar(select(LiquidityConnection).where(LiquidityConnection.name == "dxFeed"))
    r = form_post(admin_web, f"/admin/trading-setup/liquidity/{c.id}", f"/admin/trading-setup/liquidity/{c.id}",
                  {"enabled": "on", "lp_server": "feed.dxfeed.com:7300", "flag": ["1", "2"], "other_flags": "8",
                   "symbols": "*", "translations": "", "params": "Download History = Yes"})
    assert "saved on MT5" in r.text
    feed = gw.links[("feed", "dxFeed")]
    assert feed.server == "feed.dxfeed.com:7300" and feed.flags == 1 | 2 | 8 and feed.translations == []
    r = form_post(admin_web, f"/admin/trading-setup/liquidity/{c.id}", f"/admin/trading-setup/liquidity/{c.id}",
                  {"params": "no equals sign here"})
    assert "Name = Value" in r.text
    r = form_post(admin_web, f"/admin/trading-setup/liquidity/{c.id}", f"/admin/trading-setup/liquidity/{c.id}/delete", {})
    assert "Data feed dxFeed deleted on MT5" in r.text and ("feed", "dxFeed") not in gw.links


def test_removing_the_server_takes_its_symbols_and_lps_from_the_crm_only(db, gw, server):
    symbols.sync_symbols(db, server, gw=gw)
    liquidity.sync_liquidity(db, server, gw=gw)
    from app.services import server_health
    server_health.collect(db, server, gw)  # its health readings go too
    assert servers.crm_data(db, server)["symbols"] == 9
    servers.delete_server(db, None, server, purge=True)
    assert db.scalar(select(Symbol)) is None and db.scalar(select(LiquidityConnection)) is None
    assert len(gw.symbols) == 9 and gw.links  # MT5 untouched
