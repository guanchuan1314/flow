"""Routing > Reduce the server's load: ready-made rules, added on MT5 at the top and switched off."""

import pytest

from app.core.errors import ServiceError
from app.models import MT5Server
from app.services import routing_rules
from tests.test_web import admin_web, form_post  # noqa: F401


def test_presets_are_added_switched_off_at_the_top(db, gw):
    server = db.get(MT5Server, 1)
    routing_rules.sync_routes(db, server, None, gw)
    rule = routing_rules.add_load_preset(db, None, 1, "ea_pending_cap", "800", gw=gw)
    first = gw.routes[0]
    assert first.name == rule.name == "Load: refuse EA pending orders after many deals"
    assert first.enabled is False and first.action == 1003 and first.requests == 32 | 128
    assert {(c["condition"], c["value"]) for c in first.conditions} == {(7, "1"), (3000, "800")}

    routing_rules.add_load_preset(db, None, 1, "ea_small_internal", "0.05", gw=gw)
    small = gw.routes[0]
    assert small.action == 1006 and {"condition": 2, "rule": 5, "type": 7, "value": "0.05"} in small.conditions

    routing_rules.add_load_preset(db, None, 1, "ea_modify_delay", "", gw=gw)  # blank: the suggested value
    assert gw.routes[0].action == 0 and gw.routes[0].param == "100" and not gw.routes[0].enabled

    with pytest.raises(ServiceError, match="give a number"):
        routing_rules.add_load_preset(db, None, 1, "busy_account_delay", "lots", gw=gw)
    with pytest.raises(ServiceError, match="already exists"):
        routing_rules.add_load_preset(db, None, 1, "ea_pending_cap", "800", gw=gw)


def test_ea_condition_takes_yes_or_no(db, gw):
    with pytest.raises(ServiceError, match="1 for yes"):
        routing_rules.create_rule(db, None, server_id=1, name="Bad", gw=gw, values={
            "action": 1003, "conditions": [{"condition": 7, "rule": 0, "type": 10, "value": "maybe"}]})


def test_presets_on_the_routing_page(admin_web, db, gw):  # noqa: F811
    page = admin_web.get("/admin/routing").text
    assert "Reduce the server&#39;s load" in page or "Reduce the server's load" in page
    r = form_post(admin_web, "/admin/routing", "/admin/routing/preset",
                  {"server_id": "1", "key": "busy_account_delay", "value": "400"})
    assert "added on MT5 at the top, switched off" in r.text
    assert gw.routes[0].name == "Load: delay accounts with many deals today" and not gw.routes[0].enabled
