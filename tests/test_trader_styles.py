"""MT5 > Trader styles: metrics from the CRM's deals, styles and tags set up as ordered rules."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import ServiceError
from app.db import utcnow
from app.models import AccountGroup, Deal, MT5Server
from app.services import mt5_sync, trader_styles
from tests.test_web import admin_web, form_post  # noqa: F401

START = utcnow().replace(hour=10, minute=0, second=0, microsecond=0) - timedelta(days=3)


@pytest.fixture
def server(db):
    return db.get(MT5Server, 1)


class Deals:
    """Writes deals the way MT5 records them: an opening and a closing deal per position."""

    def __init__(self, db, login):
        self.db, self.login, self.pos = db, login, login * 1000

    def _deal(self, at, action, entry, lots, profit, reason, symbol):
        self.db.flush()
        ticket = (self.db.scalar(select(Deal.ticket).order_by(Deal.ticket.desc()).limit(1)) or 1000) + 1
        self.db.add(Deal(server_id=1, ticket=ticket, login=self.login, time=at, time_msc=int(at.timestamp() * 1000),
                         action=action, entry=entry, symbol=symbol, volume=Decimal(str(lots)), price=Decimal("1.1"),
                         profit=Decimal(str(profit)), commission=Decimal("0"), swap=Decimal("0"), fee=Decimal("0"),
                         position_id=self.pos, order=0, reason=reason, comment="", contract_size=Decimal("100000"),
                         tick_value=Decimal("1"), tick_size=Decimal("0.00001"), digits=5, synced_at=utcnow()))

    def position(self, opened, held_seconds, lots=1, profit=0, side=0, reason=0, close_reason=None, symbol="EURUSD"):
        self.pos += 1
        self._deal(opened, side, 0, lots, 0, reason, symbol)
        self._deal(opened + timedelta(seconds=held_seconds), 1 - side, 1, lots, profit,
                   reason if close_reason is None else close_reason, symbol)
        self.db.commit()


def accounts(db, gw, server, n):
    logins = [gw.create_user(group="real\\standard", leverage=100, name=f"Trader {i}", email=f"s{i}@example.com",
                             phone="", country="DE", master_password="Pass-12345", investor_password="Inv-12345")
              for i in range(n)]
    mt5_sync.sync_trading_accounts(db, server, None, gw)
    return logins


def test_metrics(db, gw, server):
    login, = accounts(db, gw, server, 1)
    d = Deals(db, login)
    d.position(START, 60, lots=1, profit=-10, reason=1)  # an EA scalp, lost
    d.position(START + timedelta(minutes=5), 60, lots=2, profit=5, reason=1)  # bigger after the loss
    d.position(START + timedelta(minutes=10), 2 * 86400, lots=1, profit=20, reason=16, close_reason=4)  # mobile, TP
    d.position(START + timedelta(minutes=11), 3600, lots=1, side=1, profit=-1, reason=16)  # sell while a buy is open
    d.position(START + timedelta(hours=1), 600, lots=3, symbol="XAUUSD")
    m = trader_styles.metrics(db, server, 30)[(server.id, login)]
    assert (m["deals"], m["placed"], m["positions"], m["symbols"]) == (10, 9, 5, 2)  # the TP close isn't placed
    assert (m["ea_pct"], m["mobile_pct"], m["desktop_pct"]) == (44.4, 33.3, 22.2)
    assert m["sl_tp_pct"] == 20.0 and m["quick_pct"] == 40.0 and m["overnight_pct"] == 20.0
    assert m["median_hold_min"] == 10.0 and m["max_lots"] == 3.0 and m["avg_lots"] == 1.6
    assert (m["loss_followups"], m["martingale_pct"]) == (1, 100.0)  # the one position after a loss was bigger
    assert m["hedge_pct"] == 20.0  # 1 of 5 opened against an open position
    assert m["win_rate"] == 40.0 and m["total_profit"] == 14.0 and m["top_symbol_pct"] == 80.0


def test_rules_in_order_tags_and_the_minimum(db, gw, server):
    ea, scalper, swing = accounts(db, gw, server, 3)
    d_ea, d_scalper, d_swing = Deals(db, ea), Deals(db, scalper), Deals(db, swing)
    for i in range(12):
        d_ea.position(START + timedelta(minutes=i * 30), 30, reason=1)
        d_scalper.position(START + timedelta(minutes=i * 30), 30, reason=16)
    for i in range(6):
        d_swing.position(START - timedelta(days=5) + timedelta(hours=i), 3 * 86400, reason=0)
    rows = {r["account"].login: r for r in trader_styles.profiles(db, server, 30, 10)}
    assert {k: r["style"] for k, r in rows.items()} == {ea: "ea", scalper: "scalper", swing: "swing"}
    assert rows[scalper]["tags"] == ["mobile"] and rows[ea]["tags"] == []
    assert trader_styles.profiles(db, server, 30, 100)[0]["style"] == "few"  # under the minimum

    # put Scalper first, without the EA condition: the EA account is now a scalper
    rules = trader_styles.rules(server)
    scalp = next(r for r in rules if r["key"] == "scalper")
    scalp["conditions"] = [c for c in scalp["conditions"] if c["metric"] != "ea_pct"]
    trader_styles.save_rules(db, None, server, [scalp] + [r for r in rules if r["key"] != "scalper"])
    rows = {r["account"].login: r for r in trader_styles.profiles(db, server, 30, 10)}
    assert rows[ea]["style"] == "scalper"
    # nothing matches: Unclassified
    trader_styles.save_rules(db, None, server, [{"key": "only", "name": "Only", "kind": "style", "color": "info",
                                                 "enabled": True, "group_id": None,
                                                 "conditions": [{"metric": "deals", "op": "ge", "value": 1000,
                                                                 "value2": None}]}])
    assert {r["style"] for r in trader_styles.profiles(db, server, 30, 10)} == {"unclassified"}


def test_old_style_groups_become_the_rules_groups(db, server):
    ag = AccountGroup(name="ECN", server_id=1, mt5_group="real\\ecn", leverage_options=[100])
    db.add(ag)
    db.commit()
    server.style_groups = {"ea": ag.id}  # saved before styles could be set up
    db.commit()
    assert trader_styles.style_groups(db, server) == {"ea": ag}
    assert next(r for r in trader_styles.rules(server) if r["key"] == "ea")["group_id"] == ag.id


def draft(name="Quick", kind="style", conditions=(("quick_pct", "ge", "50", ""),), **extra):
    rows = [dict(zip(("metric", "op", "value", "value2"), c)) for c in conditions]
    return {"name": name, "kind": kind, "color": "danger", "group_id": "", "enabled": True, "conditions": rows, **extra}


def test_a_rule_with_several_conditions(db, server):
    rule = trader_styles.save_rule(db, None, server, None, draft("My: Scalper!", conditions=[
        ("quick_pct", "between", "50", "90"), ("ea_pct", "le", "20", ""), ("positions", "ge", "10", ""),
        ("deals", "ge", "", "")]))  # a row left blank
    assert rule["key"] == "my_scalper" and len(rule["conditions"]) == 3
    assert rule["conditions"][0] == {"metric": "quick_pct", "op": "between", "value": 50.0, "value2": 90.0}
    assert trader_styles.holds(rule, {"quick_pct": 60, "ea_pct": 10, "positions": 10})
    assert not trader_styles.holds(rule, {"quick_pct": 60, "ea_pct": 30, "positions": 10})  # all of them must hold

    trader_styles.save_rule(db, None, server, "my_scalper", draft("Renamed"))  # the key stays, and the place in the list
    found = trader_styles.rules(server)
    assert [r["name"] for r in found if r["key"] == "my_scalper"] == ["Renamed"]


def test_new_styles_go_before_the_style_that_catches_the_rest(db, server):
    trader_styles.save_rule(db, None, server, None, draft())
    keys = [r["key"] for r in trader_styles.rules(server) if r["kind"] == "style"]
    assert keys[-2:] == ["quick", "mixed"]
    assert trader_styles.never_reached(trader_styles.rules(server)) == {}

    trader_styles.move_rule(db, None, server, "quick", 1)
    assert trader_styles.never_reached(trader_styles.rules(server)) == {"quick": "Mixed"}
    trader_styles.move_rule(db, None, server, "quick", 1)  # already last
    trader_styles.move_rule(db, None, server, "quick", -1)
    assert [r["key"] for r in trader_styles.rules(server) if r["kind"] == "style"][-2:] == ["quick", "mixed"]
    trader_styles.move_rule(db, None, server, "hedger", 1)  # a tag: no order
    assert trader_styles.rules(server)[-1]["kind"] == "tag"


@pytest.mark.parametrize("bad, message", [
    (draft(name=" "), "name"),
    (draft(name="scalper"), "already"),
    (draft(kind="nope"), "choose style or tag"),
    (draft(kind="tag", conditions=()), "needs at least one condition"),
    (draft(conditions=[("deals", "between", "9", "1")]), "lower value first"),
    (draft(conditions=[("deals", "ge", "x", "")]), "not a number"),
    (draft(conditions=[("nope", "ge", "1", "")]), "Unknown metric"),
    (draft(conditions=[("deals", "ge", "1", "")] * 11), "at most 10"),
])
def test_rule_validation(db, server, bad, message):
    with pytest.raises(ServiceError, match=message):
        trader_styles.save_rule(db, None, server, None, bad)


def test_keys_of_odd_names(db, server):
    for name, key in (("Mixed 2", "mixed_2"), ("few", "few_2"), ("new", "new_2"), ("交易", "style"), ("新", "style_2")):
        assert trader_styles.save_rule(db, None, server, None, draft(name))["key"] == key


def test_deleting(db, server):
    from app.models import AutomationRule

    db.add(AutomationRule(name="Move EAs", trigger="trader_style", params={"style": "ea"}, action="tag"))
    db.commit()
    with pytest.raises(ServiceError, match="Move EAs"):
        trader_styles.delete_rule(db, None, server, "ea")
    trader_styles.delete_rule(db, None, server, "scalper")
    assert "scalper" not in {r["key"] for r in trader_styles.rules(server)}
    for r in trader_styles.rules(server):
        if r["kind"] == "style" and r["key"] != "ea":
            trader_styles.delete_rule(db, None, server, r["key"])
    db.delete(db.scalar(select(AutomationRule)))
    db.commit()
    with pytest.raises(ServiceError, match="at least one enabled style"):
        trader_styles.delete_rule(db, None, server, "ea")


def test_setup_pages(admin_web, db, gw, server):  # noqa: F811
    login, = accounts(db, gw, server, 1)
    d = Deals(db, login)
    for i in range(3):
        d.position(START + timedelta(hours=i), 90, reason=1)
    url = "/admin/accounts/trader-styles/setup"
    page = admin_web.get(url).text
    assert "Bigger after a loss" in page and "Martingale-like" in page
    assert "Deals per active day ≥ 50" in page and "Closed within 2 minutes ≥ 50%" in page
    assert f"{url}/scalper" in page and f"{url}/new?server=1&amp;kind=tag" in page
    tried = admin_web.get(f"{url}?login={login}").text
    assert f"Account {login}" in tried and "EA / algo" in tried

    form = admin_web.get(f"{url}/scalper").text
    assert form.count('name="cond_metric"') >= 3 and 'value="Scalper"' in form
    assert 'name="cond_metric"' in admin_web.get(f"{url}/mixed").text  # no conditions yet: an empty row to fill in
    assert "Add a tag" in admin_web.get(f"{url}/new?kind=tag").text
    assert "no longer exists" in admin_web.get(f"{url}/gone").text

    r = form_post(admin_web, f"{url}/new", f"{url}/new", {
        "name": "Quick", "kind": "style", "color": "danger", "group_id": "", "enabled": "on",
        "cond_metric": ["quick_pct", "ea_pct"], "cond_op": ["ge", "le"], "cond_value": ["50", "20"], "cond_value2": ["", ""]})
    assert "Saved Quick" in r.text and "Closed within 2 minutes ≥ 50%" in r.text
    db.expire_all()
    assert len(next(r for r in trader_styles.rules(db.get(MT5Server, 1)) if r["key"] == "quick")["conditions"]) == 2

    r = form_post(admin_web, f"{url}/quick", f"{url}/quick", {
        "name": "Quick two", "kind": "style", "color": "danger", "group_id": "", "enabled": "on",
        "cond_metric": ["deals"], "cond_op": ["between"], "cond_value": ["9"], "cond_value2": ["1"]})
    assert "lower value first" in r.text and 'value="Quick two"' in r.text and 'value="9"' in r.text  # what was typed

    r = form_post(admin_web, url, f"{url}/quick/move", {"step": "1"})
    assert "catches every account first" in r.text
    r = form_post(admin_web, url, f"{url}/quick/delete", {})
    assert "Deleted" in r.text and "Quick" not in admin_web.get("/admin/accounts/trader-styles").text
    assert "Set up styles" in admin_web.get("/admin/accounts/trader-styles").text
    r = form_post(admin_web, url, f"{url}/reset", {})
    assert "Back to the default styles" in r.text
    db.expire_all()
    assert [r["key"] for r in trader_styles.rules(db.get(MT5Server, 1))][:3] == ["signal", "hft", "ea"]


# ---------------------------------------------------------------- styles on account lists, and the trading filters
@pytest.fixture(autouse=True)
def fresh_style_cache():
    trader_styles._style_cache.clear()
    yield
    trader_styles._style_cache.clear()


def test_badges_on_account_lists(db, gw, server):
    from app.models import TradingAccount

    ea, idle = accounts(db, gw, server, 2)
    d = Deals(db, ea)
    for i in range(12):
        d.position(START + timedelta(minutes=i * 30), 30, reason=1)
    Deals(db, idle).position(START, 600)  # one trade: too few to have a style
    rows = db.scalars(select(TradingAccount).where(TradingAccount.login.in_([ea, idle]))).all()
    found = trader_styles.badges(db, rows)
    mine = next(a for a in rows if a.login == ea)
    assert set(found) == {mine.id} and found[mine.id]["style"][0] == trader_styles.STYLES["ea"]
    assert trader_styles.accounts_with_style(db, "ea") == {(mine.server_id, ea)}


def test_trading_filters_wildcards_group_and_style(db, gw, server, admin_web):  # noqa: F811
    from app.models import TradingAccount
    from app.core.masks import wildcard
    from app.web.admin.trading import _filters

    assert (wildcard("*EUR*"), wildcard("EUR*"), wildcard("*EUR"), wildcard("EURUSD")) == ("%EUR%", "EUR%", "%EUR", None)
    assert wildcard("EUR_?") == "EUR\__"  # LIKE characters are literal
    ea, other = accounts(db, gw, server, 2)
    for i, symbol in enumerate(("EURUSD", "USDJPY", "EURGBP", "XAUUSD")):
        Deals(db, ea).position(START + timedelta(minutes=i), 30, reason=1, symbol=symbol)
    for i in range(12):
        Deals(db, ea).position(START + timedelta(hours=1, minutes=i * 30), 30, reason=1)
    Deals(db, other).position(START, 600, symbol="GBPEUR")

    def symbols(login="", symbol="", group="", style=""):
        q = _filters(db, select(Deal.symbol).distinct(), Deal, login, symbol, group, style)
        return set(db.scalars(q))

    assert symbols(symbol="*eur*") == {"EURUSD", "EURGBP", "GBPEUR"}  # contains (any case)
    assert symbols(symbol="EUR*") == {"EURUSD", "EURGBP"}  # starts with
    assert symbols(symbol="*EUR") == {"GBPEUR"}  # ends with
    assert symbols(symbol="EURUSD") == {"EURUSD"}  # exact, as before
    assert symbols(login=f"{str(other)[:-1]}?") >= {"GBPEUR"} and symbols(login="abc") == set()
    group = db.scalar(select(TradingAccount.account_group_id).where(TradingAccount.login == other))
    if group:
        assert "GBPEUR" in symbols(group=str(group))
    assert symbols(style="ea") == {"EURUSD", "USDJPY", "EURGBP", "XAUUSD"}  # only the EA account's deals

    for path in ("/admin/trades/deals/data?symbol=*EUR*&style=ea", "/admin/trades/positions/data?symbol=EUR*&group=1",
                 "/admin/trades/orders/data?status=history&login=5*"):
        r = admin_web.get(path)
        assert r.status_code == 200 and "All trader styles" in r.text and "All account groups" in r.text
