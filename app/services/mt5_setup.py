"""One view of a server's trading setup (quotes in, symbols, routing, orders out), and one sync for all of it."""

from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ServiceError
from app.models import LiquidityConnection, MT5Server, Symbol
from app.mt5 import MT5Gateway
from app.services import liquidity, routing_rules, symbols


def sync_all(db: Session, server: MT5Server, actor=None, gw: MT5Gateway | None = None) -> dict:
    """Sync symbols, gateways and data feeds, and routing rules from MT5. A part that fails doesn't
    stop the others; its error is returned instead."""
    out = {}
    for key, fn in (("symbols", symbols.sync_symbols), ("liquidity", liquidity.sync_liquidity),
                    ("routes", routing_rules.sync_routes)):
        try:
            out[key] = fn(db, server, actor, gw=gw)
        except ServiceError as exc:
            db.rollback()
            out[key] = {"error": exc.message}
    return out


def overview(db: Session, server: MT5Server) -> dict:
    links = db.scalars(select(LiquidityConnection).where(LiquidityConnection.server_id == server.id)
                       .order_by(LiquidityConnection.kind, LiquidityConnection.name)).all()
    feeds = [c for c in links if c.kind == "feed"]
    gateways = [c for c in links if c.kind == "gateway"]
    rules = routing_rules.rules(db, server.id)
    targets = {c.gateway_login: c for c in gateways}
    used = {d["login"] for r in rules if r.enabled for d in r.dealers}
    syms = db.scalars(select(Symbol).where(Symbol.server_id == server.id)).all()
    folders = Counter(s.path.rpartition("\\")[0] or "(top level)" for s in syms)

    warnings = []
    for c in links:
        if c.enabled and not (c.state or {}).get("connected"):
            warnings.append(f"{liquidity.KINDS[c.kind]} {c.name} is enabled but not connected to its LP.")
    for c in gateways:
        if c.enabled and c.gateway_login not in used:
            warnings.append(f"No enabled routing rule sends orders to gateway {c.name}.")
    if syms and not feeds and not gateways:
        warnings.append("No data feed or gateway: the symbols get no quotes.")
    return {
        "feeds": feeds, "gateways": gateways, "rules": [(r, routing_rules.describe(r, targets)) for r in rules],
        "symbols": len(syms), "disabled_symbols": sum(1 for s in syms if s.trade_mode == 0),
        "folders": sorted(folders.items()), "warnings": warnings,
        "routes_to": {c.id: [r.name for r in rules if any(d["login"] == c.gateway_login for d in r.dealers)]
                      for c in gateways},
    }
