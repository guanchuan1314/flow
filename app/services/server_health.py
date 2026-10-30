"""MT5 > Server health: each server's components (trade, access, history, backup…) with the load MT5 reports
for them, checked against MT5's own warning levels, plus the load the CRM's worker puts on the server.

Samples are read every HEALTH_INTERVAL_MINUTES by the worker (or on demand) and kept HEALTH_KEEP_DAYS."""

from __future__ import annotations

import logging
import time
from dataclasses import asdict
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.audit import audit
from app.db import SessionLocal, utcnow
from app.models import MT5Server, ServerHealthSample, TradingAccount
from app.mt5 import MT5Error, MT5Gateway, get_gateway
from app.mt5.gateway import COMPONENT_TYPES

log = logging.getLogger(__name__)
NEAR = 0.8  # share of a connection / network warning level that already counts as close to it
SLOW_CALL_MS = 500  # MT5 answers the worker's requests slower than this: worth a look


def _component(c) -> dict:
    d = asdict(c)
    d["last_boot"] = c.last_boot.isoformat() if c.last_boot else None
    return d


def collect(db: Session, server: MT5Server, gw: MT5Gateway | None = None) -> ServerHealthSample:
    """Read the server's components now and keep the sample; a server MT5 can't be read gives a sample with the error."""
    now = utcnow()
    try:
        components, error = [_component(c) for c in (gw or get_gateway(server.id)).get_server_health()], None
    except MT5Error as exc:
        components, error = [], str(exc)[:512]
    previous = _last_read(db, server.id)
    sample = ServerHealthSample(server_id=server.id, taken_at=now, components=components,
                                poll=dict(server.poll_stats or {}), error=error)
    db.add(sample)
    if previous is not None and error is None:
        _record_changes(db, server, previous.components, components)
    db.execute(delete(ServerHealthSample).where(
        ServerHealthSample.server_id == server.id,
        ServerHealthSample.taken_at < now - timedelta(days=get_settings().health_keep_days)))
    db.commit()
    _banner_cache.clear()
    return sample


# what restarting each kind of component does to the people using it
RESTART_EFFECT = {
    0: "Every client, manager, gateway and data feed disconnects, and trading stops until it is back. "
       "The CRM reconnects on its own.",
    1: "Clients on this trade server disconnect and can't trade until it is back.",
    2: "Charts and history requests pause until it is back.",
    3: "Clients connected through it disconnect; terminals reconnect, to another access server if there is one.",
    4: "Backups pause until it is back.",
}


def live_components(server: MT5Server, gw: MT5Gateway | None = None) -> list:
    """The server's components read from MT5 now (for the restart page)."""
    return sorted((gw or get_gateway(server.id)).get_server_health(), key=lambda c: (c.type, c.name))


def restart(db: Session, actor, server: MT5Server, component_id: int, gw: MT5Gateway | None = None) -> str:
    """Restart one component on MT5. Returns its name."""
    from app.core.errors import ServiceError

    gw = gw or get_gateway(server.id)
    try:
        found = next((c for c in gw.get_server_health() if c.id == component_id), None)
        if found is None:
            raise ServiceError(f"{server.name} has no component {component_id}")
        gw.restart_component(component_id)
    except MT5Error as exc:
        raise ServiceError(f"MT5 restart failed: {exc}", 502) from exc
    audit(db, actor, "mt5_server.restart", "mt5_server", server.id,
          {"component": found.name, "component_id": found.id, "type": COMPONENT_TYPES.get(found.type, found.type),
           "address": found.address})
    db.commit()
    log.warning("MT5 '%s': %s restarted by %s", server.name, found.name, getattr(actor, "email", "the CRM"))
    _banner_cache.clear()
    return found.name


def _last_read(db: Session, server_id: int) -> ServerHealthSample | None:
    """The latest sample that could be read."""
    return db.scalar(select(ServerHealthSample).where(ServerHealthSample.server_id == server_id,
                                                      ServerHealthSample.error.is_(None))
                     .order_by(ServerHealthSample.taken_at.desc()).limit(1))


def _record_changes(db: Session, server: MT5Server, before: list[dict], now: list[dict]) -> None:
    """Audit log entries for components that lost or regained their connection since the last reading."""
    was = {c["id"]: c["connected"] for c in before}
    for c in now:
        if c["id"] in was and was[c["id"]] != c["connected"]:
            what = "reconnected" if c["connected"] else "disconnected"
            (log.info if c["connected"] else log.warning)("MT5 '%s': %s %s", server.name, c["name"], what)
            audit(db, None, f"server_health.{what}", "mt5_server", server.id,
                  {"component": c["name"], "type": COMPONENT_TYPES.get(c["type"], c["type"]), "address": c["address"]})


def collect_due(db: Session, gw: MT5Gateway | None = None) -> int:
    """Worker: sample every enabled server whose last sample is older than HEALTH_INTERVAL_MINUTES."""
    minutes = get_settings().health_interval_minutes
    if minutes <= 0:
        return 0
    due = utcnow() - timedelta(minutes=minutes)
    taken = 0
    for server in db.scalars(select(MT5Server).where(MT5Server.enabled.is_(True)).order_by(MT5Server.id)).all():
        last = db.scalar(select(func.max(ServerHealthSample.taken_at)).where(ServerHealthSample.server_id == server.id))
        if last is None or last <= due:
            sample = collect(db, server, gw)
            if sample.error:
                log.warning("server health of '%s' not read: %s", server.name, sample.error)
            taken += 1
    return taken


# ---------------------------------------------------------------- checks
def issues(c: dict) -> list[tuple[str, str]]:
    """(level, text) for what needs attention on one component; level is warning or danger."""
    out = []
    if not c["connected"]:
        out.append(("danger", "not connected to the main trade server"))
    if c["cpu_critical"] and c["cpu_max"] >= c["cpu_critical"]:
        out.append(("danger" if c["cpu_max"] >= 90 else "warning",
                    f"CPU reached {c['cpu_max']}%, at or above MT5's warning level of {c['cpu_critical']}%"))
    elif c["cpu_max"] >= 80:
        out.append(("warning", f"CPU reached {c['cpu_max']}%"))
    low = min(c["memory_free"], c["memory_free_min"] or c["memory_free"])
    if c["memory_free_critical"] and low <= c["memory_free_critical"]:
        out.append(("danger", f"only {low} MB of memory free (MT5 warns at {c['memory_free_critical']} MB)"))
    elif c["memory_total"] and low < c["memory_total"] * 0.1:
        out.append(("warning", f"less than 10% of memory free at times ({low} MB of {c['memory_total']} MB)"))
    if c["disk_free_critical"] and c["disk_free"] <= c["disk_free_critical"]:
        out.append(("danger", f"only {c['disk_free'] // 1024} GB of disk free (MT5 warns at "
                              f"{c['disk_free_critical'] // 1024} GB)"))
    elif c["disk_total"] and c["disk_free"] < c["disk_total"] * 0.1:
        out.append(("warning", f"less than 10% of the disk free ({c['disk_free'] // 1024} GB)"))
    for kind in ("read", "write"):
        speed, floor = c[f"disk_{kind}"], c[f"disk_{kind}_critical"]
        if floor and speed and speed < floor:
            out.append(("warning", f"disk {kind}s at {speed} MB/s, below MT5's warning level of {floor} MB/s"))
    if c["connections_critical"] and c["connections_max"] >= c["connections_critical"] * NEAR:
        out.append(("warning", f"{c['connections_max']} connections at peak, close to MT5's warning level of "
                               f"{c['connections_critical']}"))
    if c["network_critical"] and c["network_max"] >= c["network_critical"] * NEAR:
        out.append(("warning", f"network traffic reached {c['network_max']} KB/s, close to MT5's warning level of "
                               f"{c['network_critical']} KB/s"))
    return out


def poll_issues(poll: dict) -> list[tuple[str, str]]:
    out = []
    if not poll:
        return out
    if poll.get("seconds") and poll.get("interval") and poll["seconds"] > poll["interval"]:
        out.append(("warning", f"one pass of the CRM's sync took {poll['seconds']:.1f} s, longer than its "
                               f"{poll['interval']} s interval: raise SYNC_INTERVAL_SECONDS or "
                               "SYNC_IDLE_INTERVAL_SECONDS"))
    if poll.get("ms_per_call") and poll["ms_per_call"] > SLOW_CALL_MS:
        out.append(("warning", f"MT5 takes {poll['ms_per_call']:.0f} ms on average to answer the CRM's requests"))
    if poll.get("errors"):
        out.append(("warning", f"{poll['errors']} error(s) in the CRM's last sync pass; see the worker log"))
    return out


def spark(values: list[float], width: int = 300, height: int = 48, top: float | None = None) -> str:
    """SVG polyline points for a small trend line (oldest first)."""
    if len(values) < 2:
        return ""
    top = top or max(values) or 1
    step = width / (len(values) - 1)
    return " ".join(f"{i * step:.1f},{height - min(v, top) / top * (height - 2) - 1:.1f}" for i, v in enumerate(values))


# ---------------------------------------------------------------- the page
def report(db: Session, server: MT5Server) -> dict:
    samples = list(db.scalars(select(ServerHealthSample).where(
        ServerHealthSample.server_id == server.id, ServerHealthSample.taken_at >= utcnow() - timedelta(hours=24))
        .order_by(ServerHealthSample.taken_at)))
    latest = db.scalar(select(ServerHealthSample).where(ServerHealthSample.server_id == server.id)
                       .order_by(ServerHealthSample.taken_at.desc()).limit(1))
    read = next((s for s in reversed(samples) if not s.error), None)
    if latest is not None and not latest.error:
        read = latest
    readable = [x for x in samples if not x.error]

    def series(component_id: int, field: str) -> list[float]:
        return [float(c[field]) for x in readable for c in x.components if c["id"] == component_id]

    components = []
    for c in (read.components if read else []):
        found = issues(c)
        cpu, conns = series(c["id"], "cpu_max"), series(c["id"], "connections_max")
        components.append({**c, "kind": COMPONENT_TYPES.get(c["type"], f"Type {c['type']}"), "issues": found,
                           "level": "danger" if any(lvl == "danger" for lvl, _ in found) else
                           "warning" if found else "ok",
                           "memory_used_pct": round(100 - c["memory_free"] * 100 / c["memory_total"])
                           if c["memory_total"] else None,
                           "disk_used_pct": round(100 - c["disk_free"] * 100 / c["disk_total"]) if c["disk_total"] else None,
                           "last_boot": datetime.fromisoformat(c["last_boot"]) if c["last_boot"] else None,
                           "down_since": None if c["connected"] else down_since(db, server.id, c["id"]),
                           "trends": {"cpu": (spark(cpu, top=100), max(cpu) if cpu else None),
                                      "connections": (spark(conns), max(conns) if conns else None)}})
    machines: dict[str, list[str]] = {}
    for c in components:
        machines.setdefault(c["address"] or "?", []).append(c["name"])
    shared = [(addr, names) for addr, names in machines.items() if len(names) > 1]
    polls = [float(x.poll.get("seconds") or 0) for x in samples if x.poll]
    accounts = dict(db.execute(select(TradingAccount.is_demo, func.count()).where(
        TradingAccount.server_id == server.id, TradingAccount.on_server.is_(True)).group_by(TradingAccount.is_demo)).all())
    poll = dict(server.poll_stats or {})
    level = ("danger" if (latest is not None and latest.error) or any(c["level"] == "danger" for c in components)
             else "warning" if any(c["level"] == "warning" for c in components) or poll_issues(poll)
             else "ok" if components else "unknown")
    return {
        "server": server, "latest": latest, "read": read, "components": components, "shared": shared, "poll": poll,
        "level": level, "poll_issues": poll_issues(poll),
        "accounts": {"live": accounts.get(False, 0), "demo": accounts.get(True, 0)},
        "poll_trend": (spark(polls), max(polls) if polls else None),
        "samples": len(samples), "cleanup": (server.demo_cleanup or {}).get("last_result"),
    }


def report_all(db: Session) -> list[dict]:
    return [report(db, server) for server in
            db.scalars(select(MT5Server).where(MT5Server.enabled.is_(True)).order_by(MT5Server.id)).all()]


def down_since(db: Session, server_id: int, component_id: int) -> datetime | None:
    """When the component was first seen disconnected in its current outage (None = in every reading kept)."""
    since = None
    for x in db.scalars(select(ServerHealthSample).where(ServerHealthSample.server_id == server_id,
                                                         ServerHealthSample.error.is_(None))
                        .order_by(ServerHealthSample.taken_at.desc())):
        c = next((c for c in x.components if c["id"] == component_id), None)
        if c is None or c["connected"]:
            break
        since = x.taken_at
    return since


# ---------------------------------------------------------------- alert on every admin page
ALERT_SECONDS = 30
_banner_cache: dict[str, tuple[float, list[dict]]] = {}


def alerts(db: Session) -> list[dict]:
    """Components disconnected in their server's latest reading, and servers whose latest reading failed."""
    out = []
    for server in db.scalars(select(MT5Server).where(MT5Server.enabled.is_(True)).order_by(MT5Server.id)).all():
        latest = db.scalar(select(ServerHealthSample).where(ServerHealthSample.server_id == server.id)
                           .order_by(ServerHealthSample.taken_at.desc()).limit(1))
        if latest is None:
            continue
        if latest.error:
            out.append({"server": server.name, "server_id": server.id, "name": None, "kind": None,
                        "since": latest.taken_at, "error": latest.error})
            continue
        for c in latest.components:
            if not c["connected"]:
                out.append({"server": server.name, "server_id": server.id, "name": c["name"],
                            "kind": COMPONENT_TYPES.get(c["type"], f"Type {c['type']}"), "access": c["type"] == 3,
                            "since": down_since(db, server.id, c["id"]) or latest.taken_at, "error": None})
    return out


def banner_alerts() -> list[dict]:
    """alerts(), cached for ALERT_SECONDS: shown at the top of every admin page."""
    cached = _banner_cache.get("alerts")
    if cached and time.monotonic() - cached[0] < ALERT_SECONDS:
        return cached[1]
    try:
        with SessionLocal() as db:
            found = alerts(db)
    except Exception:  # noqa: BLE001 - a banner must never break a page
        log.exception("health alerts unavailable")
        found = []
    _banner_cache["alerts"] = (time.monotonic(), found)
    return found
