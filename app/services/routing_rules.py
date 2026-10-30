"""MT5 routing rules: what happens to each trade request (confirm, send to a gateway or dealer, delay,
reject, …). MT5 checks the enabled rules from the top and the first match decides, so order matters.

Every create, update, delete and move is applied on MT5 first; the CRM then re-reads the server's
rules so its copy (and their order) always matches MT5. "Sync from MT5" brings in changes made in
MT5 Administrator.
"""

import logging
import re
from dataclasses import fields as dc_fields
from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import Conflict, ServiceError
from app.db import utcnow
from app.models import LiquidityConnection, MT5Server, RoutingRule
from app.mt5 import MT5Error, MT5Gateway
from app.mt5.gateway import MT5Route
from app.services.mt5_calls import call, delete_tolerant, gateway

log = logging.getLogger(__name__)

FIELDS = tuple(f.name for f in dc_fields(MT5Route) if f.name != "name")
ACTION_DEALER, ACTION_DEALER_ONLINE = 1001, 1002
# EnRouteAction: final actions end the routing; the others change the request and MT5 checks the next rule
ACTIONS = {1006: "Confirm at the market price", 1005: "Confirm at the client's price",
           ACTION_DEALER: "Send to a gateway or dealer", ACTION_DEALER_ONLINE: "Send to a dealer if one is online",
           1003: "Reject", 1004: "Requote", 1007: "Cancel the order", 1008: "Send to Ultency",
           0: "Delay (milliseconds)", 1: "Delay (ticks)", 2: "Clear take profit", 3: "Clear stop loss",
           4: "Clear stop loss and take profit"}
INTERMEDIATE = {0, 1, 2, 3, 4}
ACTION_PARAMS = {0: 3, 1: 3}  # actions taking a value -> its EnConditionType (3 = whole number)
TYPES = {1: "Buy", 2: "Sell", 4: "Buy limit", 8: "Sell limit", 16: "Buy stop", 32: "Sell stop", 64: "Buy stop limit",
         128: "Sell stop limit"}
ALL_TYPES = 255
REQUESTS = {8: "Market execution", 4: "Instant execution", 2: "Request execution", 16: "Exchange execution",
            1: "Price request", 32: "Place pending order", 64: "Modify SL / TP", 128: "Modify order",
            256: "Delete order", 512: "Order activation", 1024: "Stop limit activation", 2048: "Stop loss hit",
            4096: "Take profit hit", 8192: "Stop out (order)", 16384: "Stop out (position)",
            32768: "Order expiry", 16777216: "Close by"}
ALL_REQUESTS = 33554431  # also the dealer-side requests (bits 65536 … 8388608)
RULES = {0: "=", 1: "≠", 2: ">", 3: "≥", 4: "<", 5: "≤"}
# EnRouteCondition -> (label, EnConditionType of its value, hint). Conditions set in MT5 Administrator
# that are not listed here are kept as they are.
CONDITIONS = {
    1001: ("Group", 20, "mask, e.g. real\\*"),
    1: ("Symbol", 21, "mask, e.g. XAU* or Forex\\*"),
    1000: ("Account login", 18, "login"),
    2: ("Volume", 7, "lots"),
    4: ("Time (server)", 11, "HH:MM"),
    5: ("Weekday", 12, "0 = Sunday … 6 = Saturday"),
    6: ("Order comment", 1, "text"),
    1006: ("Client comment", 1, "text"),
    1002: ("Country", 1, "name"),
    1005: ("Leverage", 9, "e.g. 100"),
    2004: ("Balance", 6, "amount"),
    2003: ("Equity", 6, "amount"),
    7: ("Placed by an Expert Advisor", 10, "1 = yes, 0 = no"),
    8: ("Placed by a trading signal", 10, "1 = yes, 0 = no"),
    3000: ("Deals today (account)", 3, "number of deals"),
}
TIME = re.compile(r"^([01]?\d|2[0-4]):[0-5]\d$")


def to_mt5(rule: RoutingRule) -> MT5Route:
    return MT5Route(name=rule.name, **{k: getattr(rule, k) for k in FIELDS})


def pull(rule: RoutingRule, route: MT5Route) -> bool:
    changed = False
    for k in FIELDS:
        value = getattr(route, k)
        if getattr(rule, k) != value:
            setattr(rule, k, value)
            changed = True
    return changed


def _validate(route: MT5Route, current: MT5Route | None = None) -> None:
    if not route.name.strip() or len(route.name) > 64:
        raise ServiceError("Rule name: 1 to 64 characters")
    if route.action not in ACTIONS and (current is None or route.action != current.action):
        raise ServiceError(f"Unknown action {route.action}")
    if not route.types & ALL_TYPES:
        raise ServiceError("Tick at least one order type")
    if not route.requests:
        raise ServiceError("Tick at least one kind of request")
    if route.action in ACTION_PARAMS and not str(route.param).isdigit():
        raise ServiceError("Give the delay as a whole number")
    if route.action == ACTION_DEALER and not route.dealers:
        raise ServiceError("Choose the gateway, or give the dealer logins, to send the requests to")
    known = {(c["condition"], c["type"]) for c in (current.conditions if current else [])}
    for c in route.conditions:
        label = condition_label(c["condition"])
        if c["condition"] not in CONDITIONS and (c["condition"], c["type"]) not in known:
            raise ServiceError(f"Unknown condition {c['condition']}")
        if c["rule"] not in RULES:
            raise ServiceError(f"{label}: unknown comparison")
        value = str(c["value"]).strip()
        if not value:
            raise ServiceError(f"{label}: give a value")
        if c["type"] == 11 and not TIME.match(value):
            raise ServiceError(f"{label}: write the time as HH:MM")
        if c["type"] == 10 and value not in ("0", "1"):
            raise ServiceError(f"{label}: 1 for yes, 0 for no")
        if c["type"] in (2, 3, 9, 12, 18) and not value.lstrip("-").isdigit():
            raise ServiceError(f"{label}: give a whole number")
        if c["type"] in (4, 6, 7, 15):
            try:
                float(value)
            except ValueError:
                raise ServiceError(f"{label}: give a number") from None


def condition_label(code: int) -> str:
    return CONDITIONS[code][0] if code in CONDITIONS else f"Condition {code}"


def condition_type(code: int, current: list[dict] | None = None) -> int:
    """The value type of a condition: the catalogue's, or the one MT5 reported for a condition the
    catalogue doesn't list."""
    if code in CONDITIONS:
        return CONDITIONS[code][1]
    return next((c["type"] for c in current or [] if c["condition"] == code), 0)


def gateway_targets(db: Session, server_id: int) -> dict[int, LiquidityConnection]:
    """Gateways by the login they use on the MT5 server, which is what a rule sends requests to."""
    return {c.gateway_login: c for c in db.scalars(select(LiquidityConnection).where(
        LiquidityConnection.server_id == server_id, LiquidityConnection.kind == "gateway"))}


def describe(rule: RoutingRule, targets: dict[int, LiquidityConnection]) -> dict:
    """Plain-language parts of a rule, for the lists."""
    if rule.types & ALL_TYPES == ALL_TYPES:
        types = "All orders"
    else:
        types = ", ".join(label for bit, label in TYPES.items() if rule.types & bit)
    if rule.requests == ALL_REQUESTS:
        requests = "all requests"
    else:
        names = [label for bit, label in REQUESTS.items() if rule.requests & bit]
        requests = ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "") if names else "dealer requests"
    conditions = [f"{condition_label(c['condition']).lower()} {RULES.get(c['rule'], '?')} {c['value']}"
                  for c in rule.conditions]
    action = ACTIONS.get(rule.action, f"Action {rule.action}")
    if rule.action in ACTION_PARAMS:
        action = f"Delay {rule.param} {'ms' if rule.action == 0 else 'ticks'}"
    elif rule.action in (ACTION_DEALER, ACTION_DEALER_ONLINE) and rule.dealers:
        names = [f"gateway {targets[d['login']].name}" if d["login"] in targets else (d.get("name") or f"dealer {d['login']}")
                 for d in rule.dealers]
        action = "Send to " + ", ".join(names)
    return {"what": f"{types}, {requests}", "conditions": conditions, "action": action,
            "final": rule.action not in INTERMEDIATE}


# ---------------------------------------------------------------- sync
def _mirror(db: Session, server_id: int, remote: list[MT5Route]) -> dict:
    now = utcnow()
    existing = {r.name: r for r in db.scalars(select(RoutingRule).where(RoutingRule.server_id == server_id))}
    created, updated, removed = [], [], []
    for position, route in enumerate(remote):
        rule = existing.pop(route.name, None)
        if rule is None:
            rule = RoutingRule(server_id=server_id, name=route.name, position=position)
            pull(rule, route)
            db.add(rule)
            created.append(route.name)
        elif pull(rule, route) or rule.position != position:
            updated.append(route.name)
        rule.position, rule.synced_at = position, now
    for name, rule in existing.items():  # deleted in MT5 Administrator
        db.delete(rule)
        removed.append(name)
    db.flush()
    return {"routes": len(remote), "created": created, "updated": updated, "removed": removed}


def _read(server_id: int, gw: MT5Gateway) -> list[MT5Route]:
    return call(gw.get_routes, "routing rules read")


def sync_routes(db: Session, server: MT5Server, actor=None, gw: MT5Gateway | None = None) -> dict:
    """Make the CRM's routing rules for `server` match MT5, in MT5's order."""
    gw = gateway(server.id, gw)
    try:
        remote = gw.get_routes()
    except MT5Error as exc:
        raise ServiceError(f"Could not read routing rules from '{server.name}': {exc}", 502) from exc
    summary = _mirror(db, server.id, remote)
    server.last_sync_summary = {**(server.last_sync_summary or {}), "routes": summary, "at": utcnow().isoformat()}
    audit(db, actor, "mt5_sync.routes", "mt5_server", server.id,
          {"routes": len(remote), **{k: len(summary[k]) for k in ("created", "updated", "removed")}})
    db.commit()
    return summary


def rules(db: Session, server_id: int) -> list[RoutingRule]:
    return db.scalars(select(RoutingRule).where(RoutingRule.server_id == server_id)
                      .order_by(RoutingRule.position, RoutingRule.id)).all()


# ---------------------------------------------------------------- create / update / delete / move
def create_rule(db: Session, actor, *, server_id: int, name: str, values: dict, at_top: bool = True,
                gw: MT5Gateway | None = None) -> RoutingRule:
    """New rule on MT5. MT5 adds rules at the bottom; `at_top` then moves it above the others, so it
    applies before rules that would otherwise catch the same requests."""
    name = name.strip()
    if db.scalar(select(RoutingRule.id).where(RoutingRule.server_id == server_id, RoutingRule.name == name)):
        raise Conflict(f"Routing rule {name} already exists on this server")
    gw = gateway(server_id, gw)
    route = replace(MT5Route(name=name), **{k: v for k, v in values.items() if k in FIELDS and v is not None})
    route.param_type = ACTION_PARAMS.get(route.action, 0)
    if not route.param_type:
        route.param = ""
    _validate(route)
    call(lambda: gw.put_route(route, create=True), "routing rule creation")
    if at_top:
        call(lambda: gw.move_route(name, 0), "routing rule move")
    _mirror(db, server_id, _read(server_id, gw))
    rule = db.scalar(select(RoutingRule).where(RoutingRule.server_id == server_id, RoutingRule.name == name))
    audit(db, actor, "routing_rule.create", "routing_rule", rule.id,
          {"server_id": server_id, "name": name, "action": route.action, "at_top": at_top})
    db.commit()
    return rule


def update_rule(db: Session, actor, rule: RoutingRule, changes: dict, gw: MT5Gateway | None = None) -> RoutingRule:
    current = to_mt5(rule)
    route = replace(current, **{k: v for k, v in changes.items() if k in FIELDS and v is not None})
    if route.action not in ACTION_PARAMS and route.action != current.action:
        route.param, route.param_type = "", 0
    elif route.action in ACTION_PARAMS:
        route.param_type = ACTION_PARAMS[route.action]
    _validate(route, current)
    diff = sorted(k for k in FIELDS if getattr(route, k) != getattr(current, k))
    if not diff:
        return rule
    gw = gateway(rule.server_id, gw)
    call(lambda: gw.put_route(route), "routing rule update")  # MT5 first
    _mirror(db, rule.server_id, _read(rule.server_id, gw))
    audit(db, actor, "routing_rule.update", "routing_rule", rule.id, {"name": rule.name, "changed": diff})
    db.commit()
    return rule


def delete_rule(db: Session, actor, rule: RoutingRule, gw: MT5Gateway | None = None) -> None:
    gw = gateway(rule.server_id, gw)
    delete_tolerant(lambda: gw.delete_route(rule.name), "routing rule deletion")
    audit(db, actor, "routing_rule.delete", "routing_rule", rule.id, {"server_id": rule.server_id, "name": rule.name})
    server_id = rule.server_id
    db.delete(rule)
    db.flush()
    _mirror(db, server_id, _read(server_id, gw))
    db.commit()


def move_rule(db: Session, actor, rule: RoutingRule, direction: str, gw: MT5Gateway | None = None) -> RoutingRule:
    total = len(rules(db, rule.server_id))
    target = {"top": 0, "up": rule.position - 1, "down": rule.position + 1, "bottom": total - 1}.get(direction)
    if target is None:
        raise ServiceError("Move it up, down, to the top or to the bottom")
    target = max(0, min(target, total - 1))
    if target == rule.position:
        return rule
    gw = gateway(rule.server_id, gw)
    call(lambda: gw.move_route(rule.name, target), "routing rule move")
    _mirror(db, rule.server_id, _read(rule.server_id, gw))
    audit(db, actor, "routing_rule.move", "routing_rule", rule.id, {"name": rule.name, "position": target})
    db.commit()
    return rule


# ---------------------------------------------------------------- presets to reduce the server's load
# Requests that open, place or change orders (closing a position is never held back by these rules).
_OPEN = 8 | 4 | 2 | 16  # market, instant, request and exchange execution
_EA = {"condition": 7, "rule": 0, "type": 10, "value": "1"}
LOAD_PRESETS = {
    "ea_modify_delay": {
        "name": "Load: slow EA SL/TP and order changes",
        "about": "Expert Advisors that move stops or orders on every tick send most of a busy server's requests. "
                 "A short delay on those changes (MT5 then goes on to the next rule) means each EA can send only "
                 "a few per second, because MT5's trade functions wait for the answer.",
        "param": ("Delay (milliseconds)", "100"),
        "values": lambda v: {"types": ALL_TYPES, "requests": 64 | 128, "action": 0, "param": str(int(v)),
                             "conditions": [_EA]}},
    "busy_account_delay": {
        "name": "Load: delay accounts with many deals today",
        "about": "Any account (EA or not) above this many deals today gets a short delay on new orders and "
                 "changes, which slows hyperactive accounts without refusing anything.",
        "param": ("Deals today above", "500"),
        "values": lambda v: {"types": ALL_TYPES, "requests": _OPEN | 32 | 64 | 128 | 256, "action": 0, "param": "300",
                             "conditions": [{"condition": 3000, "rule": 2, "type": 3, "value": str(int(v))}]}},
    "ea_pending_cap": {
        "name": "Load: refuse EA pending orders after many deals",
        "about": "After this many deals today, an EA can no longer place or change pending orders. Market orders "
                 "and closing positions are not affected, so no one is stuck in a trade.",
        "param": ("Deals today above", "1000"),
        "values": lambda v: {"types": ALL_TYPES, "requests": 32 | 128, "action": 1003,
                             "conditions": [_EA, {"condition": 3000, "rule": 2, "type": 3, "value": str(int(v))}]}},
    "ea_small_internal": {
        "name": "Load: confirm small EA orders on the server",
        "about": "Small EA market orders are confirmed by MT5 at the market price instead of going out to a gateway, "
                 "saving the round trip to the LP. You take the other side of these trades (B-book): only for "
                 "volumes you are happy to keep.",
        "param": ("Volume up to (lots)", "0.1"),
        "values": lambda v: {"types": 1 | 2, "requests": 8 | 4, "action": 1006,
                             "conditions": [_EA, {"condition": 2, "rule": 5, "type": 7, "value": str(v)}]}},
}


def add_load_preset(db: Session, actor, server_id: int, key: str, value: str,
                    gw: MT5Gateway | None = None) -> RoutingRule:
    """Add a load preset as a rule at the top, disabled: it changes nothing until it is switched on."""
    preset = LOAD_PRESETS.get(key)
    if preset is None:
        raise ServiceError("Unknown preset")
    value = (value or preset["param"][1]).strip()
    try:
        number = float(value)
    except ValueError:
        raise ServiceError(f"{preset['param'][0]}: give a number") from None
    if number <= 0:
        raise ServiceError(f"{preset['param'][0]}: more than 0")
    values = {**preset["values"](value if key == "ea_small_internal" else int(number)), "enabled": False}
    return create_rule(db, actor, server_id=server_id, name=preset["name"], values=values, at_top=True, gw=gw)


def gateway_rule(gateway: LiquidityConnection, groups: list[str], symbols: list[str]) -> dict:
    """The values of a rule sending all orders of `groups` (and `symbols`, if given) to `gateway`."""
    conditions = [{"condition": 1001, "rule": 0, "type": 20, "value": ",".join(groups)}] if groups else []
    if symbols:
        conditions.append({"condition": 1, "rule": 0, "type": 21, "value": ",".join(symbols)})
    return {"types": ALL_TYPES, "requests": ALL_REQUESTS, "action": ACTION_DEALER, "conditions": conditions,
            "dealers": [{"login": gateway.gateway_login, "name": gateway.name}]}
