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
    m = trader_styles.metrics(db, server, 30)[login]
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


def test_parse_form_validation():
    row = ("10", "scalp", "Scalp", "style", "warning", "", "on")
    assert trader_styles.parse_form([row], [("scalp", "quick_pct", "between", "50", "90")])[0]["conditions"] == [
        {"metric": "quick_pct", "op": "between", "value": 50.0, "value2": 90.0}]
    with pytest.raises(ServiceError, match="lowercase"):
        trader_styles.parse_form([("1", "Bad Key", "X", "style", "info", "", "on")], [])
    with pytest.raises(ServiceError, match="used twice"):
        trader_styles.parse_form([row, row], [])
    with pytest.raises(ServiceError, match="not in the styles above"):
        trader_styles.parse_form([row], [("other", "deals", "ge", "1", "")])
    with pytest.raises(ServiceError, match="lower value first"):
        trader_styles.parse_form([row], [("scalp", "deals", "between", "9", "1")])
    with pytest.raises(ServiceError, match="at least one enabled style"):
        trader_styles.parse_form([("1", "t", "T", "tag", "info", "", "on")], [])
    ordered = trader_styles.parse_form([("20", "b", "B", "style", "info", "", "on"), ("5", "a", "A", "style", "info", "", "")], [])
    assert [(r["key"], r["enabled"]) for r in ordered] == [("a", False), ("b", True)]


def test_setup_page_save_try_and_reset(admin_web, db, gw, server):  # noqa: F811
    login, = accounts(db, gw, server, 1)
    d = Deals(db, login)
    for i in range(3):
        d.position(START + timedelta(hours=i), 90, reason=1)
    page = admin_web.get("/admin/trader-styles/setup").text
    assert "Bigger after a loss" in page and "Martingale-like" in page
    tried = admin_web.get(f"/admin/trader-styles/setup?login={login}").text
    assert f"Account {login}" in tried and "EA / algo" in tried

    url = "/admin/trader-styles/setup"
    r = form_post(admin_web, url, url, {
        "rule_order": ["1", "2"], "rule_key": ["quick", "rest"], "rule_name": ["Quick", "Everyone else"],
        "rule_kind": ["style", "style"], "rule_color": ["danger", "secondary"], "rule_group": ["", ""],
        "rule_enabled": ["on", "on"],
        "cond_rule": ["quick"], "cond_metric": ["quick_pct"], "cond_op": ["ge"], "cond_value": ["50"], "cond_value2": [""]})
    assert "Saved 2 styles and 0 tags" in r.text
    listing = admin_web.get("/admin/trader-styles").text
    assert "Quick" in listing and "Set up styles" in listing
    r = form_post(admin_web, url, url, {"rule_order": ["1"], "rule_key": ["Bad key"], "rule_name": ["x"],
                                        "rule_kind": ["style"], "rule_color": ["info"], "rule_group": [""],
                                        "rule_enabled": ["on"]})
    assert "lowercase letters" in r.text
    r = form_post(admin_web, url, url, {"reset": "1"})
    assert "Back to the default styles" in r.text
    db.expire_all()
    assert [r["key"] for r in trader_styles.rules(db.get(MT5Server, 1))][:3] == ["signal", "hft", "ea"]
