"""MT5 symbols. The CRM keeps a copy of each server's symbols; every create, update and delete is
applied on MT5 first, and the CRM only saves once MT5 has accepted it. "Sync from MT5" brings in
changes made in MT5 Administrator, so both sides always hold the same list."""

import logging
from dataclasses import fields as dc_fields
from dataclasses import replace
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import Conflict, ServiceError
from app.db import utcnow
from app.models import MT5Server, Order, Position, Symbol
from app.mt5 import MT5Error, MT5Gateway
from app.mt5.gateway import MT5Symbol
from app.services.mt5_calls import call, delete_tolerant, gateway

log = logging.getLogger(__name__)

FIELDS = tuple(f.name for f in dc_fields(MT5Symbol) if f.name != "name")
TRADE_MODES = {4: "Full access", 1: "Long only", 2: "Short only", 3: "Close only", 0: "Disabled"}
EXEC_MODES = {2: "Market", 1: "Instant", 0: "Request", 3: "Exchange"}
CALC_MODES = {0: "Forex", 5: "Forex no leverage", 2: "CFD", 3: "CFD index", 4: "CFD leverage", 1: "Futures",
              32: "Exchange stocks", 33: "Exchange futures", 35: "Exchange options", 37: "Exchange bonds",
              64: "Collateral"}
SWAP_MODES = {0: "Disabled", 1: "In points", 2: "In base currency", 3: "In margin currency", 4: "In group currency",
              9: "In profit currency", 5: "Interest, current price", 6: "Interest, open price",
              7: "Reopen at close price", 8: "Reopen at bid"}
WEEKDAYS = {0: "Sunday", 1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday", 5: "Friday", 6: "Saturday"}
CHOICES = {"trade_mode": TRADE_MODES, "exec_mode": EXEC_MODES, "calc_mode": CALC_MODES, "swap_mode": SWAP_MODES,
           "swap_3day": WEEKDAYS}
SESSION_KINDS = {"quote": "Quote", "trade": "Trade"}
# what a bulk edit can set on many symbols at once
BULK_FIELDS = ("trade_mode", "exec_mode", "spread", "stops_level", "freeze_level", "swap_mode", "swap_long",
               "swap_short", "swap_3day", "volume_min", "volume_max", "volume_step")


def to_mt5(sym: Symbol) -> MT5Symbol:
    return MT5Symbol(name=sym.name, **{k: getattr(sym, k) for k in FIELDS})


def pull(sym: Symbol, s: MT5Symbol) -> bool:
    """Copy MT5's settings onto the CRM's symbol. Returns whether anything changed."""
    changed = False
    for k in FIELDS:
        value = getattr(s, k)
        current = getattr(sym, k)
        if isinstance(value, Decimal) and current is not None:
            same = Decimal(str(current)) == value
        else:
            same = current == value
        if not same:
            setattr(sym, k, value)
            changed = True
    return changed


def _validate(name: str, s: MT5Symbol, current: MT5Symbol | None = None) -> None:
    if not name or len(name) > 31 or any(c in name for c in " \\/*?\"<>|"):
        raise ServiceError("Symbol name: 1 to 31 characters, without spaces or \\ / * ? \" < > |")
    if not 0 <= s.digits <= 11:
        raise ServiceError("Digits must be between 0 and 11")
    if s.contract_size <= 0:
        raise ServiceError("Contract size must be above 0")
    if s.volume_min <= 0 or s.volume_step <= 0 or s.volume_max < s.volume_min:
        raise ServiceError("Volumes: minimum and step above 0, maximum at least the minimum")
    for k in ("spread", "stops_level", "freeze_level"):
        if getattr(s, k) < 0:
            raise ServiceError(f"{k.replace('_', ' ').capitalize()} can't be negative")
    for k, allowed in CHOICES.items():  # a mode set in MT5 Administrator that the form doesn't list stays allowed
        if getattr(s, k) not in allowed and (current is None or getattr(s, k) != getattr(current, k)):
            raise ServiceError(f"Unknown {k.replace('_', ' ')}: {getattr(s, k)}")
    for kind, week in (s.sessions or {}).items():
        if kind not in SESSION_KINDS or len(week) != 7:
            raise ServiceError("Sessions: quote and trade sessions for each of the 7 weekdays")
        for day, sessions in enumerate(week):
            end = 0
            for open_, close in sorted(sessions):
                if not 0 <= open_ < close <= 1440 or open_ < end:
                    raise ServiceError(f"{SESSION_KINDS[kind]} sessions on {WEEKDAYS[day]}: each from 00:00 to 24:00 "
                                       "at most, starting before it ends, without overlapping")
                end = close


def format_sessions(week: list) -> list[str]:
    """Per weekday, e.g. "00:00-24:00" or "01:00-22:15, 22:30-24:00" ("" = closed)."""
    def hm(minutes: int) -> str:
        return f"{minutes // 60:02d}:{minutes % 60:02d}"
    return [", ".join(f"{hm(a)}-{hm(b)}" for a, b in day) for day in week] if week else [""] * 7


def parse_sessions(days: list[str]) -> list[list[list[int]]]:
    """The reverse of `format_sessions`, for the 7 weekday fields of the form."""
    def minutes(text: str) -> int:
        hours, sep, mins = text.strip().partition(":")
        if not sep or not hours.isdigit() or not mins.isdigit():
            raise ServiceError(f"Sessions: write times as HH:MM (not \"{text.strip()}\")")
        return int(hours) * 60 + int(mins)
    week = []
    for text in days:
        day = []
        for part in filter(None, (p.strip() for p in text.split(","))):
            start, sep, end = part.partition("-")
            if not sep:
                raise ServiceError(f"Sessions: write each one as HH:MM-HH:MM (not \"{part}\")")
            day.append([minutes(start), minutes(end)])
        week.append(sorted(day))
    return week


# ---------------------------------------------------------------- sync
def sync_symbols(db: Session, server: MT5Server, actor=None, gw: MT5Gateway | None = None) -> dict:
    """Make the CRM's symbols for `server` match MT5: add new ones, update changed ones, remove the
    ones MT5 no longer has."""
    gw = gateway(server.id, gw)
    try:
        remote = gw.get_symbols()
    except MT5Error as exc:
        raise ServiceError(f"Could not read symbols from '{server.name}': {exc}", 502) from exc
    now = utcnow()
    existing = {s.name: s for s in db.scalars(select(Symbol).where(Symbol.server_id == server.id))}
    created, updated, removed = [], [], []
    for s in remote:
        sym = existing.pop(s.name, None)
        if sym is None:
            sym = Symbol(server_id=server.id, name=s.name)
            pull(sym, s)
            db.add(sym)
            created.append(s.name)
        elif pull(sym, s):
            updated.append(s.name)
        sym.synced_at = now
    for name, sym in existing.items():  # deleted in MT5 Administrator
        db.delete(sym)
        removed.append(name)
    summary = {"symbols": len(remote), "created": created, "updated": updated, "removed": removed}
    server.last_sync_summary = {**(server.last_sync_summary or {}), "symbols": summary, "at": now.isoformat()}
    audit(db, actor, "mt5_sync.symbols", "mt5_server", server.id,
          {"symbols": len(remote), "created": len(created), "updated": len(updated), "removed": len(removed)})
    db.commit()
    log.info("server %s: %d symbols, %d new, %d updated, %d removed", server.name, len(remote), len(created),
             len(updated), len(removed))
    return summary


def refresh_symbol(db: Session, sym: Symbol) -> str | None:
    """Re-read the symbol so the edit form shows MT5's current values. Returns a warning, or None."""
    try:
        remote = next((s for s in gateway(sym.server_id).get_symbols() if s.name == sym.name), None)
    except (MT5Error, ServiceError) as exc:
        return f"Could not read the symbol from MT5 ({exc}). The values below are from the last sync."
    if remote is None:
        return f"{sym.name} no longer exists on MT5. Delete it here, or sync the server's symbols."
    if pull(sym, remote):
        sym.synced_at = utcnow()
        db.commit()
    return None


# ---------------------------------------------------------------- create / update / delete
def create_symbol(db: Session, actor, *, server_id: int, name: str, template: str, changes: dict,
                  gw: MT5Gateway | None = None) -> Symbol:
    """New symbol on MT5, copied from `template` (sessions, margin rates, flags) with `changes` applied."""
    name = name.strip()
    if db.scalar(select(Symbol.id).where(Symbol.server_id == server_id, func.lower(Symbol.name) == name.lower())):
        raise Conflict(f"Symbol {name} already exists on this server")
    gw = gateway(server_id, gw)
    base = next((s for s in call(gw.get_symbols, "symbol lookup") if s.name == template), None)
    if base is None:
        raise ServiceError(f"Symbol {template} to copy from doesn't exist on that MT5 server. Sync its symbols first.")
    folder = base.path.rpartition("\\")[0]
    defaults = {"path": f"{folder}\\{name}" if folder else name, "description": ""}
    spec = replace(base, name=name, **(defaults | {k: v for k, v in changes.items() if k in FIELDS and v is not None}))
    _validate(name, spec)
    saved = call(lambda: gw.put_symbol(spec, template=template), "symbol creation")
    sym = Symbol(server_id=server_id, name=saved.name, synced_at=utcnow())
    pull(sym, saved)
    db.add(sym)
    db.flush()
    audit(db, actor, "symbol.create", "symbol", sym.id,
          {"server_id": server_id, "name": name, "copied_from": template, **_plain(changes)})
    db.commit()
    return sym


def update_symbol(db: Session, actor, sym: Symbol, changes: dict, gw: MT5Gateway | None = None) -> Symbol:
    current = to_mt5(sym)
    spec = replace(current, **{k: v for k, v in changes.items() if k in FIELDS and v is not None})
    _validate(sym.name, spec, current)
    diff = {k: getattr(spec, k) for k in FIELDS if getattr(spec, k) != getattr(current, k)}
    if not diff:
        return sym
    saved = call(lambda: gateway(sym.server_id, gw).put_symbol(spec), "symbol update")  # MT5 first
    pull(sym, saved)
    sym.synced_at = utcnow()
    audit(db, actor, "symbol.update", "symbol", sym.id, {"name": sym.name, **_plain(diff)})
    db.commit()
    return sym


def bulk_update(db: Session, actor, syms: list[Symbol], changes: dict,
                gw: MT5Gateway | None = None) -> tuple[list[str], dict[str, str]]:
    """Apply the same settings to several symbols, each on MT5 first. Returns the symbols saved and,
    per symbol, why MT5 or the CRM refused it (the others still go through)."""
    changes = {k: v for k, v in changes.items() if k in BULK_FIELDS and v is not None}
    if not changes:
        raise ServiceError("Set at least one value to apply")
    if not syms:
        raise ServiceError("Tick the symbols to change")
    done, failed = [], {}
    for sym in syms:
        try:
            update_symbol(db, actor, sym, changes, gw=gw)
            done.append(sym.name)
        except ServiceError as exc:
            db.rollback()
            failed[sym.name] = exc.message
    return done, failed


def usage(db: Session, sym: Symbol) -> dict[str, int]:
    """Open trading the CRM knows about on this symbol."""
    def count(model, *where):
        return db.scalar(select(func.count()).select_from(model).where(
            model.server_id == sym.server_id, model.symbol == sym.name, model.is_open.is_(True), *where)) or 0
    return {"open positions": count(Position), "pending orders": count(Order)}


def delete_symbol(db: Session, actor, sym: Symbol, gw: MT5Gateway | None = None) -> None:
    busy = {k: v for k, v in usage(db, sym).items() if v}
    if busy:
        raise ServiceError(f"{sym.name} still has " + " and ".join(f"{v} {k}" for k, v in busy.items())
                           + ". Close them first.")
    gw = gateway(sym.server_id, gw)
    delete_tolerant(lambda: gw.delete_symbol(sym.name), "symbol deletion")
    audit(db, actor, "symbol.delete", "symbol", sym.id, {"server_id": sym.server_id, "name": sym.name})
    db.delete(sym)
    db.commit()


def _plain(values: dict) -> dict:
    return {k: str(v) if isinstance(v, Decimal) else v for k, v in values.items()}
