"""MT5 servers > Symbols & pricing: keep the server's symbols in sync on a timer, and the broker's markups on top
of the LP's prices, written to MT5:

- spread: points added to the spread of the matching account groups (MT5's "spread difference" on the group's
  symbol settings), e.g. Standard +15 and ECN +0 on the same symbols;
- commission: money per lot, charged on every deal in and out, on the matching account groups (MT5 group
  commissions the CRM names "CRM …"; commissions set up in MT5 Administrator are left alone);
- swap: points taken off the LP's swap, long and short. The CRM remembers the LP's swap; when MT5's swap is no
  longer the one the CRM wrote (the LP or someone in MT5 Administrator changed it), that becomes the LP's swap.

Group and symbol masks are MT5's: comma-separated, `*` for any text, `!` to leave out.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.masks import matches
from app.core.errors import ServiceError
from app.db import utcnow
from app.models import MT5Server, Symbol
from app.mt5 import MT5Error, MT5Gateway
from app.services.mt5_calls import gateway

log = logging.getLogger(__name__)

PREFIX = "CRM "  # the commissions the CRM owns in each group
MIN_MINUTES = 5
SKIP_GROUPS = "managers\\*"  # MT5's own staff groups never get markups
SIX = Decimal("0.000001")


# ---------------------------------------------------------------- settings
def settings(server: MT5Server) -> dict:
    p = dict(server.pricing or {})
    p.setdefault("auto_sync", {"enabled": False, "minutes": 60})
    for kind in ("spread", "commission", "swap"):
        p.setdefault(kind, [])
    p.setdefault("applied", {})
    return p


def _num(value, what: str, whole: bool = False) -> Decimal | int:
    try:
        n = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ServiceError(f"{what}: '{value}' is not a number")
    if whole:
        if n != n.to_integral_value():
            raise ServiceError(f"{what}: whole points only")
        return int(n)
    return n


def parse_rules(spread: list[tuple], commission: list[tuple], swap: list[tuple]) -> dict:
    """Form rows (blank rows ignored) -> rules. spread (groups, symbols, points), commission (groups, symbols,
    per lot), swap (symbols, long, short)."""
    out = {"spread": [], "commission": [], "swap": []}
    for groups, symbols, points in spread:
        if not (groups or symbols or points):
            continue
        if not groups or not symbols:
            raise ServiceError("Spread markup: each row needs account groups and symbols")
        pts = _num(points or 0, f"Spread markup for {groups}", whole=True)
        if abs(pts) > 100000:
            raise ServiceError("Spread markup: at most 100000 points")
        out["spread"].append({"groups": groups, "symbols": symbols, "points": pts})
    for groups, symbols, per_lot in commission:
        if not (groups or symbols or per_lot):
            continue
        if not groups or not symbols:
            raise ServiceError("Commission: each row needs account groups and symbols")
        value = _num(per_lot or 0, f"Commission for {groups}")
        if not 0 <= value <= 10000:
            raise ServiceError("Commission: from 0 to 10000 per lot")
        out["commission"].append({"groups": groups, "symbols": symbols, "per_lot": str(value)})
    for symbols, long, short in swap:
        if not (symbols or long or short):
            continue
        if not symbols:
            raise ServiceError("Swap markup: each row needs symbols")
        out["swap"].append({"symbols": symbols, "long": str(_num(long or 0, f"Swap markup for {symbols}")),
                            "short": str(_num(short or 0, f"Swap markup for {symbols}"))})
    return out


def save(db: Session, actor, server: MT5Server, rules: dict, auto_enabled: bool, minutes: int | None,
         gw: MT5Gateway | None = None) -> dict:
    """Keep the rules and the auto sync, then apply the markups on MT5. Returns apply()'s summary."""
    minutes = minutes or 60
    if minutes < MIN_MINUTES:
        raise ServiceError(f"Auto sync: every {MIN_MINUTES} minutes at most")
    p = settings(server)
    p.update(rules)
    p["auto_sync"] = {**p["auto_sync"], "enabled": auto_enabled, "minutes": minutes}
    server.pricing = p
    audit(db, actor, "pricing.settings", "mt5_server", server.id,
          {"auto_sync": auto_enabled, "minutes": minutes, **{k: rules[k] for k in ("spread", "commission", "swap")}})
    db.commit()
    return apply(db, actor, server, gw)


# ---------------------------------------------------------------- plan and apply
def _group_plan(p: dict, group) -> tuple[dict, list[dict]] | None:
    """What the group should have: ({path: points or None}, [CRM commissions]); None when it's already so."""
    spreads: dict[str, int | None] = {}
    for r in p["spread"]:
        if matches(r["groups"], group.name) and r["symbols"] not in spreads:
            spreads[r["symbols"]] = r["points"]
    for path in p["applied"].get(group.name, []):  # set by the CRM before, no rule now: back to the symbol's own
        spreads.setdefault(path, None)
    comms, paths = [], set()
    for r in p["commission"]:
        if matches(r["groups"], group.name) and r["symbols"] not in paths:
            paths.add(r["symbols"])
            comms.append({"name": (PREFIX + r["symbols"])[:63], "path": r["symbols"], "per_lot": r["per_lot"]})
    have = sorted((c["path"], round(c["value"], 6)) for c in group.commissions if c["name"].startswith(PREFIX))
    want = sorted((c["path"], round(float(c["per_lot"]), 6)) for c in comms)
    if all(group.spreads.get(path) == pts for path, pts in spreads.items()) and have == want:
        return None
    return spreads, comms


def _swap_plan(sym: Symbol, rule: dict | None) -> tuple[Decimal, Decimal, dict | None] | None:
    """(long, short, swap_markup to keep) for the symbol, or None when nothing is to be written."""
    cur_l, cur_s = Decimal(str(sym.swap_long)).quantize(SIX), Decimal(str(sym.swap_short)).quantize(SIX)
    m = sym.swap_markup

    def lp(side: str, current: Decimal) -> Decimal:
        """Still what the CRM wrote: the LP's swap it kept. Otherwise (a new value from the LP or MT5
        Administrator) the current one is the LP's."""
        if m and (Decimal(m[f"lp_{side}"]) - Decimal(m[side])).quantize(SIX) == current:
            return Decimal(m[f"lp_{side}"])
        return current
    lp_l, lp_s = lp("long", cur_l), lp("short", cur_s)
    if rule is None or not sym.swap_mode:
        target = (lp_l, lp_s, None) if m else None
    else:
        target = (lp_l - Decimal(rule["long"]), lp_s - Decimal(rule["short"]),
                  {"lp_long": str(lp_l), "lp_short": str(lp_s), "long": rule["long"], "short": rule["short"]})
    if target is None:
        return None
    if target[0].quantize(SIX) == cur_l and target[1].quantize(SIX) == cur_s and target[2] == m:
        return None
    return target


def _swap_rule(p: dict, sym: Symbol) -> dict | None:
    return next((r for r in p["swap"] if matches(r["symbols"], sym.name) or matches(r["symbols"], sym.path)), None)


def preview(db: Session, server: MT5Server, gw: MT5Gateway | None = None) -> dict:
    """What apply() would change now: {groups: [names], swaps: [symbol names], other_commissions: {group: [..]}}."""
    p = settings(server)
    groups = [g for g in gateway(server, gw).get_groups() if not matches(SKIP_GROUPS, g.name)]
    syms = db.scalars(select(Symbol).where(Symbol.server_id == server.id).order_by(Symbol.name)).all()
    return {"groups": [g.name for g in groups if _group_plan(p, g) is not None],
            "swaps": [s.name for s in syms if _swap_plan(s, _swap_rule(p, s)) is not None],
            "other_commissions": {g.name: [f"{c['name']} ({c['value']:g} per {c['per']})" for c in g.commissions
                                           if not c["name"].startswith(PREFIX)] for g in groups
                                  if any(not c["name"].startswith(PREFIX) for c in g.commissions)},
            "group_names": [g.name for g in groups]}


def apply(db: Session, actor, server: MT5Server, gw: MT5Gateway | None = None) -> dict:
    """Write the markups to MT5 where they differ. Returns {groups, swaps, failed: {what: why}}."""
    from app.services import symbols as symbol_svc

    gw = gateway(server, gw)
    p = settings(server)
    try:
        groups = [g for g in gw.get_groups() if not matches(SKIP_GROUPS, g.name)]
    except MT5Error as exc:
        raise ServiceError(f"Could not read the groups from MT5: {exc}", 502) from exc
    done_groups, done_swaps, failed = [], [], {}
    applied = dict(p["applied"])
    for g in groups:
        planned = _group_plan(p, g)
        if planned is None:
            continue
        spreads, comms = planned
        try:
            gw.set_group_pricing(g.name, spreads, comms, PREFIX)
        except MT5Error as exc:
            failed[g.name] = str(exc)
            continue
        done_groups.append(g.name)
        kept = sorted(path for path, pts in spreads.items() if pts is not None)
        if kept:
            applied[g.name] = kept
        else:
            applied.pop(g.name, None)
    for sym in db.scalars(select(Symbol).where(Symbol.server_id == server.id).order_by(Symbol.name)).all():
        planned = _swap_plan(sym, _swap_rule(p, sym))
        if planned is None:
            continue
        long, short, markup = planned
        try:
            symbol_svc.update_symbol(db, actor, sym, {"swap_long": long, "swap_short": short}, gw=gw)
        except ServiceError as exc:
            db.rollback()
            failed[sym.name] = exc.message
            continue
        sym.swap_markup = markup
        db.commit()
        done_swaps.append(sym.name)
    _sync_account_groups(db, server, p)
    summary = {"groups": done_groups, "swaps": done_swaps, "failed": failed, "at": utcnow().isoformat()}
    db.refresh(server)
    server.pricing = {**settings(server), "applied": applied, "last_apply": summary}
    if done_groups or done_swaps or failed:
        audit(db, actor, "pricing.apply", "mt5_server", server.id,
              {"groups": len(done_groups), "swaps": len(done_swaps), "failed": len(failed)})
    db.commit()
    return summary


def _sync_account_groups(db: Session, server: MT5Server, p: dict) -> None:
    """Account groups keep their spread markup (used for spread-share IB rebates) equal to the rule for all
    their symbols ("*"), when there is one."""
    from app.models import AccountGroup

    for ag in db.scalars(select(AccountGroup).where(AccountGroup.server_id == server.id)).all():
        rule = next((r for r in p["spread"] if r["symbols"] == "*" and matches(r["groups"], ag.mt5_group)), None)
        if rule is not None and ag.spread_markup_points != rule["points"]:
            ag.spread_markup_points = rule["points"]


def describe(summary: dict) -> str:
    parts = [f"{len(summary['groups'])} account group(s) and {len(summary['swaps'])} symbol swap(s) updated on MT5"]
    if not summary["groups"] and not summary["swaps"]:
        parts = ["MT5 already has these markups"]
    if summary["failed"]:
        parts.append("not done: " + "; ".join(f"{k}: {v}" for k, v in list(summary["failed"].items())[:5]))
    return ". ".join(parts) + "."


# ---------------------------------------------------------------- auto sync
def run_due(db: Session, gw: MT5Gateway | None = None) -> int:
    """Worker: for each server with auto sync on and due, sync its symbols from MT5 (new ones the LPs brought
    in, changed swaps) and apply the markups again. Returns how many servers ran."""
    from app.services import symbols as symbol_svc
    from app.services import sync_runs

    ran = 0
    for server in db.scalars(select(MT5Server).where(MT5Server.enabled.is_(True)).order_by(MT5Server.id)).all():
        auto = settings(server)["auto_sync"]
        if not auto.get("enabled"):
            continue
        last = auto.get("last_run")
        if last and datetime.fromisoformat(last) > utcnow() - timedelta(minutes=auto.get("minutes") or 60):
            continue
        try:
            out = {}

            def run() -> dict:  # kept in the synchronization history as a scheduled run
                out["symbols"] = symbol_svc.sync_symbols(db, server, None, gw)
                out["pricing"] = apply(db, None, server, gw)
                return out
            sync_runs.record(db, "pricing", server, run)
            r = out["symbols"]
            result = f"{r['symbols']} symbols, {len(r['created'])} new, {len(r['updated'])} updated. " + \
                describe(out["pricing"])
        except (ServiceError, MT5Error) as exc:
            db.rollback()
            why = getattr(exc, "message", None) or str(exc)
            result = f"failed: {why}"
            log.error("symbol auto sync failed for '%s': %s", server.name, why)
        db.refresh(server)
        p = settings(server)
        p["auto_sync"] = {**p["auto_sync"], "last_run": utcnow().isoformat(), "last_result": result}
        server.pricing = p
        db.commit()
        ran += 1
    return ran
