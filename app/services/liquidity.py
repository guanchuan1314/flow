"""Liquidity providers: MT5 gateways (orders sent to an LP) and data feeds (an LP's quotes).

The CRM keeps a copy of each server's gateways and data feeds; every create, update and delete is
applied on MT5 first, and the CRM only saves once MT5 has accepted it. "Sync from MT5" brings in
changes made in MT5 Administrator. LP passwords, and module parameters that are passwords or tokens,
are written to MT5 but never stored in the CRM.
"""

import logging
import re
from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import Conflict, ServiceError
from app.db import utcnow
from app.models import LiquidityConnection, MT5Server
from app.mt5 import MT5Error, MT5Gateway
from app.mt5.gateway import LINK_KINDS, MT5Link, MT5LinkModule
from app.services.mt5_calls import call, delete_tolerant, gateway

log = logging.getLogger(__name__)

KINDS = {"gateway": "Gateway", "feed": "Data feed"}
# The flags the form offers, per kind (value: label). Other bits set in MT5 Administrator are kept.
FLAGS = {
    "gateway": {2: "Import symbols from the LP", 4: "Ignore the LP's quotes (trade only)", 8: "Import balances",
                16: "Extended log"},
    "feed": {1: "Quotes", 2: "News", 64: "Import symbols from the LP"},
}
# Module parameters holding credentials: the CRM never stores their values (MOEX's "Password Change"
# and the like are Yes / No switches, not secrets).
SECRET = re.compile(r"password|secret|token|secure key|api key", re.I)
SWITCH = ("Yes", "No")
# Module categories for the picker: (label, words in the module name)
CATEGORIES = (
    ("MetaTrader platforms", ("MetaTrader",)),
    ("FX liquidity and brokers", ("LMAX", "Integral", "Currenex", "Cboe FX", "Euronext FX", "FXCM", "Interactive Brokers")),
    ("Exchanges", ("ASX", "BMFBOVESPA", "Borsa", "BSE", "DGCX", "GBOT", "MOEX")),
    ("Market data", ("DXFeeder", "IQ Feeder", "UniFeeder", "Ultency")),
    ("News", ("News", "FXStreet", "Claws", "Thomson")),
    ("Remote / custom", ("Remote",)),
)
# CRM column -> MT5Link field, where the names differ
RENAMED = {"lp_server": "server", "lp_login": "login"}
COLUMNS = ("module", "enabled", "lp_server", "lp_login", "gateway_server", "gateway_login", "mt5_id", "gateway_type",
           "flags", "keywords", "categories", "timeout_reconnect", "timeout_sleep", "timeout_attempts", "symbols",
           "groups", "params", "translations")
EDITABLE = ("enabled", "lp_server", "lp_login", "gateway_server", "gateway_login", "flags", "keywords", "categories",
            "timeout_reconnect", "timeout_sleep", "timeout_attempts", "symbols", "groups", "params", "translations")


def _kind(kind: str) -> str:
    if kind not in LINK_KINDS:
        raise ServiceError("Choose gateway or data feed")
    return kind


def is_secret(name: str, value: str | None = "") -> bool:
    return bool(SECRET.search(name)) and value not in SWITCH


def _stored(params: dict) -> dict:
    """The parameters as the CRM keeps them: secrets blanked."""
    return {k: "" if is_secret(k, v) else ("" if v is None else v) for k, v in params.items()}


def _outgoing(params: dict) -> dict:
    """Parameters to send to MT5: a blank secret means "keep MT5's value"."""
    return {k: None if is_secret(k, v) and not v else v for k, v in params.items()}


def secret_params(c: LiquidityConnection | None, module: MT5LinkModule | None = None) -> list[str]:
    params = c.params if c is not None else (module.params if module else {})
    return [k for k, v in params.items() if is_secret(k, v)]


def category(module: MT5LinkModule) -> str:
    return next((label for label, words in CATEGORIES if any(w in module.name for w in words)), "Other")


def grouped_modules(mods: list[MT5LinkModule]) -> list[tuple[str, list[MT5LinkModule]]]:
    order = [label for label, _ in CATEGORIES] + ["Other"]
    groups: dict[str, list[MT5LinkModule]] = {}
    for m in sorted(mods, key=lambda m: m.name.lower()):
        groups.setdefault(category(m), []).append(m)
    return [(label, groups[label]) for label in order if label in groups]


def to_mt5(c: LiquidityConnection, password: str | None = None) -> MT5Link:
    link = MT5Link(kind=c.kind, name=c.name, password=password,
                   **{RENAMED.get(k, k): getattr(c, k) for k in COLUMNS})
    link.params = _outgoing(link.params)
    return link


def pull(c: LiquidityConnection, link: MT5Link) -> bool:
    """Copy MT5's settings onto the CRM's copy (secret parameters blanked). Returns whether anything changed."""
    changed = False
    for k in COLUMNS:
        value = getattr(link, RENAMED.get(k, k))
        if k == "params":
            value = _stored(value)
        if getattr(c, k) != value:
            setattr(c, k, value)
            changed = True
    c.state = link.state
    return changed


def modules(server_id: int, kind: str, gw: MT5Gateway | None = None) -> list[MT5LinkModule]:
    return call(lambda: gateway(server_id, gw).get_link_modules(_kind(kind)), "module list")


def _validate(link: MT5Link) -> None:
    if not link.name or len(link.name) > 64:
        raise ServiceError("Name: 1 to 64 characters")
    if not link.module:
        raise ServiceError("Choose the module")
    for k in ("timeout_reconnect", "timeout_sleep", "timeout_attempts", "gateway_login"):
        if getattr(link, k) < 0:
            raise ServiceError(f"{k.replace('_', ' ').capitalize()} can't be negative")
    for t in link.translations:
        if not t.get("source") or not t.get("symbol"):
            raise ServiceError("Each symbol translation needs the LP's symbol and the MT5 symbol")


# ---------------------------------------------------------------- sync
def sync_liquidity(db: Session, server: MT5Server, actor=None, gw: MT5Gateway | None = None) -> dict:
    """Make the CRM's gateways and data feeds for `server` match MT5."""
    gw = gateway(server.id, gw)
    try:
        remote = {kind: gw.get_links(kind) for kind in LINK_KINDS}
    except MT5Error as exc:
        raise ServiceError(f"Could not read gateways and data feeds from '{server.name}': {exc}", 502) from exc
    now = utcnow()
    existing = {(c.kind, c.name): c for c in db.scalars(select(LiquidityConnection)
                                                       .where(LiquidityConnection.server_id == server.id))}
    created, updated, removed = [], [], []
    for kind, links in remote.items():
        for link in links:
            c = existing.pop((kind, link.name), None)
            if c is None:
                c = LiquidityConnection(server_id=server.id, kind=kind, name=link.name)
                pull(c, link)
                db.add(c)
                created.append(link.name)
            elif pull(c, link):
                updated.append(link.name)
            c.synced_at = now
    for (_, name), c in existing.items():  # deleted in MT5 Administrator
        db.delete(c)
        removed.append(name)
    summary = {"gateways": len(remote["gateway"]), "feeds": len(remote["feed"]), "created": created,
               "updated": updated, "removed": removed}
    server.last_sync_summary = {**(server.last_sync_summary or {}), "liquidity": summary, "at": now.isoformat()}
    audit(db, actor, "mt5_sync.liquidity", "mt5_server", server.id,
          {**{k: summary[k] for k in ("gateways", "feeds")}, "created": len(created), "updated": len(updated),
           "removed": len(removed)})
    db.commit()
    return summary


def refresh_connection(db: Session, c: LiquidityConnection) -> str | None:
    """Re-read from MT5 for the edit form (settings and live state). Returns a warning, or None."""
    try:
        link = next((x for x in gateway(c.server_id).get_links(c.kind) if x.name == c.name), None)
    except (MT5Error, ServiceError) as exc:
        return f"Could not read it from MT5 ({exc}). The values below are from the last sync."
    if link is None:
        return f"{c.name} no longer exists on MT5. Delete it here, or sync the server again."
    pull(c, link)
    c.synced_at = utcnow()
    db.commit()
    return None


# ---------------------------------------------------------------- create / update / delete
def same_name_translations(server_id: int, gw: MT5Gateway | None = None) -> list[dict]:
    """A translation for every symbol on the server, the LP's symbol having the same name."""
    return [{"source": s.name, "symbol": s.name, "digits": s.digits, "bid_markup": 0, "ask_markup": 0}
            for s in call(gateway(server_id, gw).get_symbols, "symbol list")]


def create_connection(db: Session, actor, *, server_id: int, kind: str, name: str, module: str, fields: dict,
                      password: str | None = None, map_symbols: bool = False, route_groups: list[str] | None = None,
                      route_symbols: list[str] | None = None, gw: MT5Gateway | None = None) -> LiquidityConnection:
    """New gateway / data feed on MT5. Blank settings take the module's defaults (LP address, parameters).

    In the same step: `map_symbols` (when no translations are given) translates every symbol on the
    server to the LP symbol of the same name, and for a gateway, `route_groups` adds a routing rule at
    the top sending those groups' orders (on `route_symbols`, if given) to it."""
    kind, name = _kind(kind), name.strip()
    if db.scalar(select(LiquidityConnection.id).where(LiquidityConnection.server_id == server_id,
                                                      LiquidityConnection.kind == kind,
                                                      LiquidityConnection.name == name)):
        raise Conflict(f"{KINDS[kind]} {name} already exists on this server")
    gw = gateway(server_id, gw)
    mod = next((m for m in modules(server_id, kind, gw) if m.module == module), None)
    if mod is None:
        raise ServiceError(f"Module {module} is not installed on that MT5 server")
    values = {RENAMED.get(k, k): v for k, v in fields.items() if k in EDITABLE and v not in (None, "", [], {})}
    link = MT5Link(kind=kind, name=name, module=module, server=mod.server, login=mod.login, symbols=["*"],
                   params=dict(mod.params), password=password or None,
                   flags=1 if kind == "feed" else 0, timeout_reconnect=1, timeout_sleep=5 if kind == "feed" else 60,
                   timeout_attempts=60 if kind == "feed" else 5)
    link = replace(link, **values)
    link.params = _outgoing(link.params)
    if map_symbols and not link.translations:
        link.translations = same_name_translations(server_id, gw)
    if route_groups and kind != "gateway":
        raise ServiceError("Only gateways take orders: data feeds can't have a routing rule")
    _validate(link)
    saved = call(lambda: gw.put_link(link, create=True), f"{KINDS[kind].lower()} creation")
    c = LiquidityConnection(server_id=server_id, kind=kind, name=saved.name, synced_at=utcnow())
    pull(c, saved)
    db.add(c)
    db.flush()
    audit(db, actor, f"liquidity.{kind}.create", "liquidity_connection", c.id,
          {"server_id": server_id, "name": name, "module": module, "password_set": bool(password),
           "translations": len(saved.translations)})
    db.commit()
    if route_groups:
        from app.services import routing_rules

        try:
            routing_rules.create_rule(db, actor, server_id=server_id, name=f"To {saved.name}"[:64],
                                      values=routing_rules.gateway_rule(c, route_groups, route_symbols or []), gw=gw)
        except ServiceError as exc:
            raise ServiceError(f"{KINDS[kind]} {saved.name} was created on MT5, but its routing rule wasn't: "
                               f"{exc.message} Add the rule under MT5 → Routing.", exc.status_code) from exc
    return c


def update_connection(db: Session, actor, c: LiquidityConnection, changes: dict, password: str | None = None,
                      gw: MT5Gateway | None = None) -> LiquidityConnection:
    current = to_mt5(c)
    link = replace(current, password=password or None,
                   **{RENAMED.get(k, k): v for k, v in changes.items() if k in EDITABLE and v is not None})
    link.params = _outgoing(link.params)
    _validate(link)
    diff = sorted(k for k in EDITABLE if getattr(link, RENAMED.get(k, k)) != getattr(current, RENAMED.get(k, k)))
    if not diff and not password:
        return c
    saved = call(lambda: gateway(c.server_id, gw).put_link(link), f"{KINDS[c.kind].lower()} update")  # MT5 first
    pull(c, saved)
    c.synced_at = utcnow()
    audit(db, actor, f"liquidity.{c.kind}.update", "liquidity_connection", c.id,
          {"name": c.name, "changed": diff, "password_changed": bool(password)})
    db.commit()
    return c


def restart_all(db: Session, actor, server: MT5Server, kind: str, gw: MT5Gateway | None = None) -> int:
    """Restart every gateway or every data feed of the server (MT5 can't restart just one). Returns how many."""
    kind = _kind(kind)
    call(lambda: gateway(server.id, gw).restart_links(kind), f"{KINDS[kind].lower()} restart")
    count = len(db.scalars(select(LiquidityConnection.id).where(LiquidityConnection.server_id == server.id,
                                                                LiquidityConnection.kind == kind)).all())
    audit(db, actor, f"liquidity.{kind}.restart", "mt5_server", server.id, {"connections": count})
    db.commit()
    return count


def routes_to(db: Session, c: LiquidityConnection) -> list[str]:
    """Names of the routing rules sending requests to this gateway."""
    if c.kind != "gateway":
        return []
    from app.services import routing_rules

    return [r.name for r in routing_rules.rules(db, c.server_id) if any(d["login"] == c.gateway_login for d in r.dealers)]


def delete_connection(db: Session, actor, c: LiquidityConnection, gw: MT5Gateway | None = None) -> None:
    used = routes_to(db, c)
    if used:
        raise ServiceError(f"Routing rules still send orders to {c.name}: {', '.join(used)}. "
                           "Change or delete them first (MT5 → Routing).")
    gw = gateway(c.server_id, gw)
    delete_tolerant(lambda: gw.delete_link(c.kind, c.name), f"{KINDS[c.kind].lower()} deletion")
    audit(db, actor, f"liquidity.{c.kind}.delete", "liquidity_connection", c.id,
          {"server_id": c.server_id, "name": c.name, "module": c.module})
    db.delete(c)
    db.commit()
