"""MT5 > Trader styles: how each account trades, measured from the deals stored in the CRM (no MT5 request), and
moving accounts to the group meant for their style.

The styles to detect are set up under Set up styles: a list of rules, each opening on its own page with its name and
any number of conditions on the account's metrics (EA share, holding time, deals per day, martingale-like sizing,
hedging…). All of a rule's conditions must hold.
- **Styles** are checked in order and the first whose conditions all hold is the account's style (a style without
  conditions catches everyone left). A style can name the account group it belongs in: accounts of that style in
  another group are suggested for a move.
- **Tags** are extra badges: every tag whose conditions hold is shown (e.g. Hedger, Mobile, Night trader).

Accounts with fewer placed deals than the page's minimum are "Too few trades". The rules are kept on the server
(`MT5Server.style_groups["styles"]`)."""

from __future__ import annotations

import re
import statistics
import time
from collections import defaultdict
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import and_, func, select, tuple_
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import ServiceError
from app.db import utcnow
from app.models import AccountGroup, AccountStatus, AutomationRule, Client, Deal, MT5Server, Staff, TradingAccount
from app.mt5 import MT5Gateway
from app.services import accounts

# EnDealReason
REASON_CLIENT, REASON_EXPERT, REASON_DEALER, REASON_SL, REASON_TP, REASON_SO = 0, 1, 2, 3, 4, 5
REASON_EXTERNAL, REASON_SIGNAL, REASON_MOBILE, REASON_WEB = 7, 10, 16, 17
PLATFORMS = {REASON_CLIENT: "Desktop", REASON_EXTERNAL: "External", REASON_MOBILE: "Mobile", REASON_WEB: "Web"}
PLACED = (*PLATFORMS, REASON_EXPERT, REASON_SIGNAL)  # deals the trader placed (not SL / TP / stop-out / dealer)
TRADES = (0, 1)  # DEAL_BUY, DEAL_SELL
ENTRY_IN, ENTRY_OUT, ENTRY_INOUT, ENTRY_OUT_BY = 0, 1, 2, 3
QUICK_SECONDS = 120  # "closed within 2 minutes"
DAY_SECONDS = 86400
FEW, UNCLASSIFIED = "few", "unclassified"
FIXED = {FEW: "Too few trades", UNCLASSIFIED: "Unclassified"}
COLORS = ("primary", "success", "info", "warning", "danger", "secondary", "dark", "light")
OPS = {"ge": "≥", "le": "≤", "between": "between"}
KINDS = {"style": "Style (first match)", "tag": "Tag (every match)"}
MAX_RULES, MAX_CONDITIONS = 40, 10
RESERVED = {*FIXED, "suggested", "new", "reset"}  # mean something else in filters and addresses

# metric -> (label, unit, what it measures). All over the chosen period, from the deals stored in the CRM.
METRICS = {
    "deals": ("Deals", "", "Buy and sell deals, opening and closing, whatever placed them."),
    "placed": ("Placed deals", "", "Deals the trader placed: not stop loss, take profit, stop-out or dealer deals."),
    "active_days": ("Active days", "days", "Days with at least one deal."),
    "deals_per_day": ("Deals per active day", "", "Deals divided by active days."),
    "ea_pct": ("EA deals", "%", "Share of placed deals an Expert Advisor placed."),
    "desktop_pct": ("Desktop deals", "%", "Share of placed deals from the desktop terminal."),
    "mobile_pct": ("Mobile deals", "%", "Share of placed deals from the mobile apps."),
    "web_pct": ("Web deals", "%", "Share of placed deals from the web terminal."),
    "signal_pct": ("Signal deals", "%", "Share of placed deals copied from an MQL5 signal."),
    "dealer_pct": ("Dealer deals", "%", "Share of all deals a dealer made on the account."),
    "sl_tp_pct": ("Closed by SL / TP", "%", "Share of closing deals that were a stop loss or take profit."),
    "stopout_pct": ("Stop-outs", "%", "Share of closing deals that were a stop-out."),
    "positions": ("Closed positions", "", "Positions opened and closed in the period."),
    "avg_hold_min": ("Average holding time", "min", "Average time from opening to closing a position."),
    "median_hold_min": ("Median holding time", "min", "Half the positions were held shorter than this."),
    "quick_pct": ("Closed within 2 minutes", "%", "Share of closed positions held under 2 minutes."),
    "intraday_pct": ("Closed within a day", "%", "Share of closed positions held under 24 hours."),
    "overnight_pct": ("Held over a day", "%", "Share of closed positions held 24 hours or more."),
    "avg_lots": ("Average size", "lots", "Average volume of the opening deals."),
    "max_lots": ("Largest size", "lots", "Largest opening deal."),
    "martingale_pct": ("Bigger after a loss", "%",
                       "After a losing position, how often the next one on the same symbol was larger "
                       "(martingale-like sizing)."),
    "loss_followups": ("Positions after a loss", "",
                       "Positions opened on a symbol after a losing one there: how much the line above is based on."),
    "hedge_pct": ("Opened against an open position", "%",
                  "Share of positions opened while an opposite position on the same symbol was open (hedging)."),
    "symbols": ("Symbols traded", "", "Different symbols traded."),
    "top_symbol_pct": ("Top symbol", "%", "Share of deals on the most-traded symbol."),
    "night_pct": ("Night deals", "%", "Share of deals between 22:00 and 06:00 (server time as stored)."),
    "weekend_pct": ("Weekend deals", "%", "Share of deals on Saturday or Sunday."),
    "win_rate": ("Winning positions", "%", "Share of closed positions with a profit (after commission and swap)."),
    "avg_profit": ("Average result per position", "money", "Average profit of a closed position, in the account's currency."),
    "total_profit": ("Total result", "money", "Profit of the positions closed in the period, in the account's currency."),
}


def _c(metric: str, op: str, value, value2=None) -> dict:
    return {"metric": metric, "op": op, "value": value, "value2": value2}


DEFAULT_RULES = [
    {"key": "signal", "name": "Signal follower", "kind": "style", "color": "info", "conditions": [_c("signal_pct", "ge", 70)]},
    {"key": "hft", "name": "High frequency", "kind": "style", "color": "danger",
     "conditions": [_c("deals_per_day", "ge", 50), _c("quick_pct", "ge", 50)]},
    {"key": "ea", "name": "EA / algo", "kind": "style", "color": "primary", "conditions": [_c("ea_pct", "ge", 70)]},
    {"key": "scalper", "name": "Scalper", "kind": "style", "color": "warning",
     "conditions": [_c("ea_pct", "le", 20), _c("quick_pct", "ge", 50), _c("positions", "ge", 10)]},
    {"key": "day", "name": "Day trader", "kind": "style", "color": "success",
     "conditions": [_c("ea_pct", "le", 20), _c("intraday_pct", "ge", 80), _c("positions", "ge", 10)]},
    {"key": "swing", "name": "Swing / position trader", "kind": "style", "color": "secondary",
     "conditions": [_c("ea_pct", "le", 20), _c("overnight_pct", "ge", 50), _c("positions", "ge", 5)]},
    {"key": "manual", "name": "Manual", "kind": "style", "color": "success", "conditions": [_c("ea_pct", "le", 20)]},
    {"key": "mixed", "name": "Mixed", "kind": "style", "color": "warning", "conditions": []},
    {"key": "martingale", "name": "Martingale-like", "kind": "tag", "color": "danger",
     "conditions": [_c("martingale_pct", "ge", 60), _c("loss_followups", "ge", 5)]},
    {"key": "hedger", "name": "Hedger", "kind": "tag", "color": "dark", "conditions": [_c("hedge_pct", "ge", 30)]},
    {"key": "mobile", "name": "Mobile", "kind": "tag", "color": "light", "conditions": [_c("mobile_pct", "ge", 70)]},
    {"key": "winner", "name": "Consistent winner", "kind": "tag", "color": "warning",
     "conditions": [_c("win_rate", "ge", 70), _c("positions", "ge", 20)]},
    {"key": "night", "name": "Night trader", "kind": "tag", "color": "secondary", "enabled": False,
     "conditions": [_c("night_pct", "ge", 60)]},
    {"key": "big", "name": "Large sizes", "kind": "tag", "color": "secondary", "enabled": False,
     "conditions": [_c("max_lots", "ge", 10)]},
]
STYLES = {r["key"]: r["name"] for r in DEFAULT_RULES if r["kind"] == "style"} | FIXED  # default names


# ---------------------------------------------------------------- the rules
def _defaults(old: dict) -> list[dict]:
    rules = deepcopy(DEFAULT_RULES)
    for r in rules:
        r.setdefault("enabled", True)
        r["group_id"] = old.get(r["key"]) if r["key"] in ("ea", "manual") else None  # the old EA / manual choice
    return rules


def rules(server: MT5Server | None) -> list[dict]:
    """The server's rules in order (the defaults until set up)."""
    saved = dict(server.style_groups or {}) if server is not None else {}
    if isinstance(saved.get("styles"), list):
        return deepcopy(saved["styles"])
    return _defaults({k: v for k, v in saved.items() if k in ("ea", "manual") and v})


def names(server: MT5Server | None) -> dict[str, str]:
    """Style key -> name, for every enabled style and the fixed ones."""
    return {r["key"]: r["name"] for r in rules(server) if r["kind"] == "style" and r.get("enabled", True)} | FIXED


def colors(server: MT5Server | None) -> dict[str, str]:
    return {r["key"]: r["color"] for r in rules(server)} | {FEW: "light", UNCLASSIFIED: "light"}


def _num(value, what: str) -> float:
    try:
        return float(Decimal(str(value).strip()))
    except (InvalidOperation, ValueError):
        raise ServiceError(f"{what}: '{value}' is not a number")


def _new_key(name: str, current: list[dict]) -> str:
    """A key for a new rule, from its name. Keys never change, so saved filters and automation keep working."""
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:28]
    base = base if base[:1].isalpha() else f"style_{base}".rstrip("_")
    taken = RESERVED | {r["key"] for r in current}
    n, key = 1, base
    while key in taken:
        n, key = n + 1, f"{base}_{n + 1}"
    return key


def _conditions(name: str, rows: list[dict]) -> list[dict]:
    """A form's condition rows -> conditions. Rows without a value are ignored."""
    out = []
    for row in rows:
        metric, op, value, value2 = row["metric"], row["op"], row["value"], row["value2"]
        if not str(value).strip():
            continue
        if metric not in METRICS:
            raise ServiceError(f"Unknown metric '{metric}'")
        if op not in OPS:
            raise ServiceError("Conditions: choose ≥, ≤ or between")
        label = f"{name}: {METRICS[metric][0]}"
        low = _num(value, label)
        high = _num(value2, label) if op == "between" else None
        if high is not None and high < low:
            raise ServiceError(f"{label}: 'between' needs the lower value first")
        out.append(_c(metric, op, low, high))
    if len(out) > MAX_CONDITIONS:
        raise ServiceError(f"{name}: at most {MAX_CONDITIONS} conditions")
    return out


def _insert(current: list[dict], rule: dict) -> None:
    """Add a rule to the list: a style with conditions goes before the style that catches everyone left, which would
    otherwise take every account before it is reached."""
    spot = len(current)
    if rule["kind"] == "style" and rule["conditions"]:
        spot = next((i for i, r in enumerate(current) if _catches_all(r)), spot)
    current.insert(spot, rule)


def _catches_all(rule: dict) -> bool:
    return rule["kind"] == "style" and rule.get("enabled", True) and not rule["conditions"]


def never_reached(rule_list: list[dict]) -> dict[str, str]:
    """Style key -> name of the style without conditions before it, which takes every account first."""
    catcher, out = None, {}
    for r in rule_list:
        if r["kind"] != "style" or not r.get("enabled", True):
            continue
        if catcher:
            out[r["key"]] = catcher
        elif _catches_all(r):
            catcher = r["name"]
    return out


def save_rule(db: Session, actor, server: MT5Server, key: str | None, draft: dict) -> dict:
    """Add (key None) or change one style or tag. `draft` is what the form sent: name, kind, color, group_id, enabled
    and conditions (rows of metric, op, value, value2)."""
    current = rules(server)
    old = next((r for r in current if r["key"] == key), None)
    if key and old is None:
        raise ServiceError("That style no longer exists")
    name, kind = draft["name"].strip()[:40], draft["kind"]
    if not name:
        raise ServiceError("Give it a name")
    if kind not in KINDS:
        raise ServiceError(f"{name}: choose style or tag")
    if any(r is not old and r["name"].lower() == name.lower() for r in current):
        raise ServiceError(f"There is already a style or tag called {name}")
    if old is None and len(current) >= MAX_RULES:
        raise ServiceError(f"At most {MAX_RULES} styles and tags")
    conditions = _conditions(name, draft["conditions"])
    if kind == "tag" and not conditions:
        raise ServiceError(f"{name}: a tag needs at least one condition, or every account would get it")
    group_id = str(draft["group_id"]).strip()
    rule = {"key": key or _new_key(name, current), "name": name, "kind": kind,
            "color": draft["color"] if draft["color"] in COLORS else "secondary",
            "group_id": int(group_id) if group_id.isdigit() and kind == "style" else None,
            "enabled": bool(draft["enabled"]), "conditions": conditions}
    if old is not None and old["kind"] == kind:
        current[current.index(old)] = rule
    else:
        if old is not None:
            current.remove(old)
        _insert(current, rule)
    save_rules(db, actor, server, current)
    return rule


def delete_rule(db: Session, actor, server: MT5Server, key: str) -> None:
    current = rules(server)
    rule = next((r for r in current if r["key"] == key), None)
    if rule is None:
        raise ServiceError("That style no longer exists")
    for automation in db.scalars(select(AutomationRule).where(AutomationRule.trigger == "trader_style")):
        if (automation.params or {}).get("style") == key:
            raise ServiceError(f"The automation rule {automation.name} uses {rule['name']}: change that first, "
                               "or switch the style off")
    current.remove(rule)
    save_rules(db, actor, server, current)


def move_rule(db: Session, actor, server: MT5Server, key: str, step: int) -> None:
    """Move a style one place up (-1) or down (1) among the styles. Tags have no order: all of them are checked."""
    current = rules(server)
    places = [i for i, r in enumerate(current) if r["kind"] == "style"]
    at = next((n for n, i in enumerate(places) if current[i]["key"] == key), None)
    if at is None or not 0 <= at + step < len(places):
        return
    a, b = places[at], places[at + step]
    current[a], current[b] = current[b], current[a]
    save_rules(db, actor, server, current)


def save_rules(db: Session, actor, server: MT5Server, new_rules: list[dict] | None) -> list[dict]:
    """Keep the rules (None = back to the defaults). Suggested groups must be live groups of the server."""
    if new_rules is None:
        new_rules = _defaults({})
    if not any(r["kind"] == "style" and r.get("enabled", True) for r in new_rules):
        raise ServiceError("Keep at least one enabled style")
    for r in new_rules:
        if r.get("group_id"):
            ag = db.get(AccountGroup, r["group_id"])
            if ag is None or ag.server_id != server.id:
                raise ServiceError(f"{r['name']}: choose an account group of this server")
            if ag.is_demo:
                raise ServiceError(f"{r['name']}: {ag.name} is a demo account group; styles are for live accounts")
    server.style_groups = {"styles": new_rules}
    audit(db, actor, "trader_styles.rules", "mt5_server", server.id,
          {"styles": [r["key"] for r in new_rules if r["kind"] == "style"],
           "tags": [r["key"] for r in new_rules if r["kind"] == "tag"]})
    db.commit()
    return new_rules


def style_groups(db: Session, server: MT5Server) -> dict[str, AccountGroup]:
    """style -> the account group it belongs in."""
    out = {}
    for r in rules(server):
        ag = db.get(AccountGroup, int(r["group_id"])) if r.get("group_id") and r["kind"] == "style" else None
        if ag is not None and ag.server_id == server.id:
            out[r["key"]] = ag
    return out


def save_style_groups(db: Session, actor, server: MT5Server, choices: dict[str, int | None]) -> dict:
    """Set the account group of some styles (style key -> group id or None), keeping the rest of the rules."""
    current = rules(server)
    for r in current:
        if r["key"] in choices and r["kind"] == "style":
            r["group_id"] = choices[r["key"]]
    chosen = [r["group_id"] for r in current if r.get("group_id") and r["kind"] == "style"]
    if len(chosen) != len(set(chosen)):
        raise ServiceError("Give each style its own account group")
    save_rules(db, actor, server, current)
    return {r["key"]: r["group_id"] for r in current if r.get("group_id")}


# ---------------------------------------------------------------- matching
def holds(rule: dict, m: dict) -> bool:
    for c in rule["conditions"]:
        v = m.get(c["metric"])
        if v is None:
            return False
        if c["op"] == "ge" and not v >= c["value"]:
            return False
        if c["op"] == "le" and not v <= c["value"]:
            return False
        if c["op"] == "between" and not c["value"] <= v <= c["value2"]:
            return False
    return True


def classify(m: dict, rule_list: list[dict], min_deals: int) -> tuple[str, list[str]]:
    """(style key, tag keys) of an account's metrics."""
    live = [r for r in rule_list if r.get("enabled", True)]
    tags = [r["key"] for r in live if r["kind"] == "tag" and holds(r, m)]
    if m["placed"] < min_deals:
        return FEW, tags
    style = next((r["key"] for r in live if r["kind"] == "style" and holds(r, m)), UNCLASSIFIED)
    return style, tags


def explain(m: dict, rule_list: list[dict]) -> list[dict]:
    """Per rule, whether each condition holds for these metrics (the setup page's "try on an account")."""
    out = []
    for r in rule_list:
        checks = [{**c, "label": METRICS[c["metric"]][0], "actual": m.get(c["metric"]),
                   "ok": holds({"conditions": [c]}, m)} for c in r["conditions"]]
        out.append({"rule": r, "checks": checks, "ok": all(c["ok"] for c in checks)})
    return out


# ---------------------------------------------------------------- metrics
def _pct(part: float, whole: float) -> float:
    return round(part * 100 / whole, 1) if whole else 0.0


def metrics(db: Session, server: MT5Server | None, days: int = 30, accounts: list[tuple] | None = None
            ) -> dict[tuple, dict]:
    """(server id, login) -> its metrics (see METRICS) over the last `days`, for every account with a buy or sell deal
    then, or those of `accounts`, which are such pairs too."""
    since = utcnow() - timedelta(days=days)
    q = select(Deal.server_id, Deal.login, Deal.time, Deal.action, Deal.entry, Deal.reason, Deal.symbol, Deal.volume,
               Deal.profit + Deal.commission + Deal.swap, Deal.position_id).where(
        Deal.time >= since, Deal.action.in_(TRADES))
    if server is not None:
        q = q.where(Deal.server_id == server.id)
    if accounts is not None:
        q = q.where(tuple_(Deal.server_id, Deal.login).in_(accounts))
    by_account: dict[tuple, list] = defaultdict(list)
    for row in db.execute(q.order_by(Deal.server_id, Deal.login, Deal.time)):
        by_account[(row[0], row[1])].append(row[1:])
    return {key: _account_metrics(rows) for key, rows in by_account.items()}


def _account_metrics(rows: list) -> dict:
    reasons: dict[int, int] = defaultdict(int)
    symbols: dict[str, int] = defaultdict(int)
    days, night, weekend, closes, sl_tp, stopouts, lots = set(), 0, 0, 0, 0, 0, []
    positions: dict[int, dict] = {}
    for _, t, action, entry, reason, symbol, volume, result, pos_id in rows:
        reasons[reason] += 1
        symbols[symbol] += 1
        days.add(t.date())
        night += t.hour >= 22 or t.hour < 6
        weekend += t.weekday() >= 5
        p = positions.setdefault(pos_id, {"symbol": symbol, "opened": None, "closed": None, "side": None,
                                          "lots": Decimal("0"), "result": Decimal("0")})
        if entry in (ENTRY_IN, ENTRY_INOUT):
            if p["opened"] is None:
                p["opened"], p["side"] = t, action
            p["lots"] += volume or 0
            lots.append(float(volume or 0))
        if entry in (ENTRY_OUT, ENTRY_INOUT, ENTRY_OUT_BY):
            closes += 1
            sl_tp += reason in (REASON_SL, REASON_TP)
            stopouts += reason == REASON_SO
            p["closed"] = t
            p["result"] += result or 0
    deals = len(rows)
    placed = sum(n for r, n in reasons.items() if r in PLACED)
    closed = [p for p in positions.values() if p["opened"] is not None and p["closed"] is not None]
    held = sorted((p["closed"] - p["opened"]).total_seconds() for p in closed)
    results = [p["result"] for p in positions.values() if p["closed"] is not None]
    m = {
        "deals": deals, "placed": placed, "active_days": len(days),
        "deals_per_day": round(deals / len(days), 1) if days else 0.0,
        "ea_pct": _pct(reasons[REASON_EXPERT], placed), "desktop_pct": _pct(reasons[REASON_CLIENT], placed),
        "mobile_pct": _pct(reasons[REASON_MOBILE], placed), "web_pct": _pct(reasons[REASON_WEB], placed),
        "signal_pct": _pct(reasons[REASON_SIGNAL], placed), "dealer_pct": _pct(reasons[REASON_DEALER], deals),
        "sl_tp_pct": _pct(sl_tp, closes), "stopout_pct": _pct(stopouts, closes),
        "positions": len(closed),
        "avg_hold_min": round(sum(held) / len(held) / 60, 1) if held else None,
        "median_hold_min": round(statistics.median(held) / 60, 1) if held else None,
        "quick_pct": _pct(sum(1 for h in held if h < QUICK_SECONDS), len(held)),
        "intraday_pct": _pct(sum(1 for h in held if h < DAY_SECONDS), len(held)),
        "overnight_pct": _pct(sum(1 for h in held if h >= DAY_SECONDS), len(held)),
        "avg_lots": round(sum(lots) / len(lots), 2) if lots else 0.0, "max_lots": max(lots) if lots else 0.0,
        "symbols": len(symbols), "top_symbol_pct": _pct(max(symbols.values()), deals) if symbols else 0.0,
        "night_pct": _pct(night, deals), "weekend_pct": _pct(weekend, deals),
        "win_rate": _pct(sum(1 for r in results if r > 0), len(results)) if results else None,
        "avg_profit": round(float(sum(results)) / len(results), 2) if results else None,
        "total_profit": round(float(sum(results)), 2),
        "platforms": {label: round(reasons[r] * 100 / placed) for r, label in PLATFORMS.items()
                      if placed and reasons[r]},
    }
    m.update(_sizing_and_hedging(positions))
    return m


def _sizing_and_hedging(positions: dict[int, dict]) -> dict:
    """Martingale-like sizing (bigger after a loss on the same symbol) and hedging (opened while an opposite
    position on the same symbol was open), per symbol in opening order."""
    per_symbol: dict[str, list] = defaultdict(list)
    for p in positions.values():
        if p["opened"] is not None:
            per_symbol[p["symbol"]].append(p)
    followups = bigger = hedged = opened = 0
    for items in per_symbol.values():
        items.sort(key=lambda p: p["opened"])
        for prev, cur in zip(items, items[1:]):
            if prev["closed"] is not None and prev["closed"] <= cur["opened"] and prev["result"] < 0:
                followups += 1
                bigger += cur["lots"] > prev["lots"]
        open_now: list = []  # positions still open at the current opening time
        for p in items:
            opened += 1
            open_now = [o for o in open_now if o["closed"] is None or o["closed"] > p["opened"]]
            hedged += any(o["side"] != p["side"] for o in open_now)
            open_now.append(p)
    return {"loss_followups": followups, "martingale_pct": _pct(bigger, followups),
            "hedge_pct": _pct(hedged, opened)}


# ---------------------------------------------------------------- the list
MAX_PROFILES = 2000  # accounts on the Trader styles list: the busiest, so the page never loads every deal


def _busiest_metrics(db: Session, server: MT5Server | None, days: int, demo: bool) -> dict[tuple, dict]:
    """Metrics of the (at most MAX_PROFILES) accounts with the most placed deals in the period.

    The ranking is one aggregate query; only the deals of the accounts shown are then loaded, in chunks."""
    since = utcnow() - timedelta(days=days)
    placed = func.count().filter(Deal.reason.in_(PLACED))
    q = (select(Deal.server_id, Deal.login)
         .join(TradingAccount, and_(TradingAccount.server_id == Deal.server_id, TradingAccount.login == Deal.login))
         .where(Deal.time >= since, Deal.action.in_(TRADES), TradingAccount.is_demo.is_(demo),
                TradingAccount.status != AccountStatus.archived)
         .group_by(Deal.server_id, Deal.login).having(placed > 0)
         .order_by(placed.desc(), Deal.server_id, Deal.login).limit(MAX_PROFILES))
    if server is not None:
        q = q.where(Deal.server_id == server.id)
    busiest = [tuple(row) for row in db.execute(q)]
    found: dict[tuple, dict] = {}
    for i in range(0, len(busiest), 500):
        found.update(metrics(db, server, days, busiest[i:i + 500]))
    return found


def profiles(db: Session, server: MT5Server | None, days: int = 30, min_deals: int = 10,
             demo: bool = False, staff=None) -> list[dict]:
    """One row per account that traded in the last `days`, busiest first; server None = every server.
    With `staff`, only the accounts of the clients that staff member may see.
    Each row: account, client, style (key), tags (keys), metrics, suggest (account group or None), and the
    older summary keys (placed, per_day, ea_pct, signal_pct, platforms, positions, avg_hold_minutes,
    high_frequency). At most MAX_PROFILES accounts, the busiest."""
    found = _busiest_metrics(db, server, days, demo)
    if not found:
        return []
    servers = [server] if server else list(db.scalars(select(MT5Server)))
    rule_sets = {s.id: rules(s) for s in servers}
    targets = {s.id: style_groups(db, s) for s in servers}
    rows = []
    query = (select(TradingAccount, Client).join(Client, Client.id == TradingAccount.client_id)
             .where(tuple_(TradingAccount.server_id, TradingAccount.login).in_(list(found)),
                    TradingAccount.is_demo.is_(demo), TradingAccount.status != AccountStatus.archived))
    if staff is not None:
        from app.services.clients import visible_client_ids

        query = query.where(Client.id.in_(visible_client_ids(staff)))
    for acc, client in db.execute(query):
        m = found[(acc.server_id, acc.login)]
        if not m["placed"]:
            continue
        style, tags = classify(m, rule_sets[acc.server_id], min_deals)
        target = targets.get(acc.server_id, {}).get(style)
        rows.append({
            "account": acc, "client": client, "style": style, "tags": tags, "metrics": m,
            "placed": m["placed"], "per_day": round(m["placed"] / days, 1), "ea_pct": round(m["ea_pct"]),
            "signal_pct": round(m["signal_pct"]), "platforms": m["platforms"], "positions": m["positions"],
            "avg_hold_minutes": m["avg_hold_min"],
            "high_frequency": m["positions"] >= 20 and m["quick_pct"] >= 50,
            "suggest": target if target is not None and acc.account_group_id != target.id
            and target.currency == acc.currency else None,
        })
    rows.sort(key=lambda r: -r["placed"])
    return rows


DEFAULT_DAYS, DEFAULT_MIN_DEALS = 30, 10  # as on the Trader styles page
_STYLE_CACHE_SECONDS = 60
_style_cache: dict[tuple, tuple[float, set[tuple]]] = {}


def badges(db: Session, accounts: list[TradingAccount], days: int = DEFAULT_DAYS,
           min_deals: int = DEFAULT_MIN_DEALS) -> dict[int, dict]:
    """account id -> {"style": (name, colour) or None, "tags": [(name, colour)]} for the accounts that have a style or
    a tag (accounts with too few trades, or none, are left out). For lists: one query for the given accounts."""
    if not accounts:
        return {}
    found = metrics(db, None, days, [(a.server_id, a.login) for a in accounts])
    servers = {s.id: s for s in db.scalars(select(MT5Server).where(MT5Server.id.in_({key[0] for key in found})))}
    out = {}
    for acc in accounts:
        m = found.get((acc.server_id, acc.login))
        if not m or not m["placed"]:
            continue
        server = servers.get(acc.server_id)
        rule_list, color = rules(server), colors(server)
        name = {r["key"]: r["name"] for r in rule_list}
        style, tags = classify(m, rule_list, min_deals)
        badge = None if style in FIXED else (name.get(style, style), color.get(style, "secondary"))
        tag_badges = [(name.get(t, t), color.get(t, "secondary")) for t in tags]
        if badge or tag_badges:
            out[acc.id] = {"style": badge, "tags": tag_badges}
    return out


def accounts_with_style(db: Session, key: str, days: int = DEFAULT_DAYS, min_deals: int = DEFAULT_MIN_DEALS) -> set[tuple]:
    """Accounts as (server id, login), live and demo, every server, whose style is `key`, or that carry tag `key`: the trading filters.
    Same accounts as the Trader styles list (the busiest MAX_PROFILES per kind); cached for a minute."""
    hit = _style_cache.get((key, days, min_deals))
    if hit and time.monotonic() - hit[0] < _STYLE_CACHE_SECONDS:
        return hit[1]
    out: set[tuple] = set()
    for server in db.scalars(select(MT5Server)):
        rule_list = rules(server)
        for demo in (False, True):
            for account, m in _busiest_metrics(db, server, days, demo).items():
                style, tags = classify(m, rule_list, min_deals)
                if style == key or key in tags:
                    out.add(account)
    _style_cache[(key, days, min_deals)] = (time.monotonic(), out)
    return out


def filter_options(db: Session) -> list[tuple[str, str]]:
    """(key, name) of every style and tag in use on any server, for the trading filters."""
    seen: dict[str, str] = {}
    for server in db.scalars(select(MT5Server)):
        for r in rules(server):
            if r.get("enabled", True):
                seen.setdefault(r["key"], r["name"] if r["kind"] == "style" else f"{r['name']} (tag)")
    return list(seen.items())


def move(db: Session, gw: MT5Gateway, actor, logins: list, target_id: int) -> tuple[list, list[str]]:
    """Move accounts to an account group (MT5 first, one by one). Returns (moved, failures).
    A staff actor can only move the accounts of clients they may see."""
    from app.services.clients import visible_account

    target = db.get(AccountGroup, target_id)
    if target is None:
        raise ServiceError("Choose the account group to move to")
    done, failed = [], []
    for login in logins:
        try:
            acc = (visible_account(db, actor, login) if isinstance(actor, Staff)
                   else accounts.get_account(db, login))
            accounts.change_group(db, gw, actor, acc, target)
            done.append(login)
        except ServiceError as exc:
            db.rollback()
            failed.append(f"{login}: {exc.message}")
    return done, failed
