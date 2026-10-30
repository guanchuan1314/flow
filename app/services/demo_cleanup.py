"""Inactive demo accounts (MT5 servers > Inactive accounts): list demo accounts nobody has used for a while from the
CRM's own data, and archive (or delete) them on MT5. Also sets MT5's own demo inactivity period per group.

The list is a database query on each account's last activity (app.services.activity keeps it: last terminal
connection and last trade), so it costs MT5 nothing however many accounts there are. An account qualifies when:
  * it is a demo account (on a live server: in a group named demo* or used by a demo account group) in a group
    matching the masks,
  * its last activity is older than `days`,
  * the CRM knows no open position or order on it, and it has no copy trading or PAMM role.
Before anything changes, each batch is checked against MT5 again: its last connection (from the Manager API's
local copy) and its open positions and orders. Archived accounts stay in MT5's archive database, so MT5
Administrator can restore them; deleting removes them."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import exists, false, func, not_, or_, select
from sqlalchemy.orm import Session

from app.core import masks
from app.core.audit import audit
from app.core.errors import ServiceError
from app.db import utcnow
from app.models import (
    AccountGroup, AccountPurpose, AccountStatus, Client, CopyStrategy, CopySubscription, MT5Server, Order, PammFund,
    Position, StrategyStatus, SubscriptionStatus, TradingAccount,
)
from app.mt5 import MT5Error, MT5Gateway, get_gateway
from app.services.mt5_sync import excluded

log = logging.getLogger(__name__)

ACTIONS = {"archive": "Archive (MT5 Administrator can restore them)",
           "delete": "Delete (the login and its history are removed for good)"}
DEFAULTS = {"enabled": False, "days": 90, "action": "archive", "groups": "demo*"}
MIN_DAYS = 7
MAX_PER_RUN = 5000  # a scheduled run continues with the rest the next day
BATCH = 500
PREVIEW = 500  # rows listed on the page
MAX_MT5_DAYS = 3650


def settings_of(server: MT5Server) -> dict:
    return {**DEFAULTS, **({"groups": "*"} if server.is_demo else {}), **(server.demo_cleanup or {})}


def save_settings(db: Session, actor, server: MT5Server, *, enabled: bool, days: int, action: str,
                  groups: str) -> dict:
    if days < MIN_DAYS:
        raise ServiceError(f"Use at least {MIN_DAYS} days without activity")
    if action not in ACTIONS:
        raise ServiceError("Choose archive or delete")
    if not masks.parse(groups):
        raise ServiceError("Give at least one group mask, e.g. demo*")
    values = {"enabled": enabled, "days": days, "action": action, "groups": ", ".join(masks.parse(groups))}
    server.demo_cleanup = {**(server.demo_cleanup or {}), **values}
    audit(db, actor, "demo_cleanup.settings", "mt5_server", server.id, values)
    db.commit()
    return settings_of(server)


# ---------------------------------------------------------------- the query
def _activity():
    return func.coalesce(TradingAccount.last_activity_at, TradingAccount.created_at)


def _in_scope(server: MT5Server, cfg: dict) -> list:
    group = func.lower(TradingAccount.group)
    chosen = masks.parse(cfg["groups"])
    never = [m[1:] for m in chosen if m.startswith("!")] + masks.excluded_groups()
    return [TradingAccount.server_id == server.id, TradingAccount.is_demo.is_(True),
            TradingAccount.on_server.is_(True), TradingAccount.status != AccountStatus.archived,
            or_(false(), *[group.like(masks.to_like(m), escape="!") for m in chosen if not m.startswith("!")]),
            *[not_(group.like(masks.to_like(m), escape="!")) for m in never]]


def _busy():
    """Open positions or pending orders known to the CRM."""
    return or_(exists().where(Position.login == TradingAccount.login, Position.is_open.is_(True)),
               exists().where(Order.login == TradingAccount.login, Order.is_open.is_(True)))


def _protected():
    """Accounts the CRM still needs: copy trading masters and followers, PAMM masters, non-trading purposes."""
    return or_(TradingAccount.purpose != AccountPurpose.trading,
               exists().where(CopyStrategy.master_login == TradingAccount.login,
                              CopyStrategy.status != StrategyStatus.closed),
               exists().where(CopySubscription.follower_login == TradingAccount.login,
                              CopySubscription.status == SubscriptionStatus.active),
               exists().where(PammFund.master_login == TradingAccount.login, PammFund.status != StrategyStatus.closed))


def _candidates(server: MT5Server, cfg: dict, cutoff: datetime) -> list:
    return [*_in_scope(server, cfg), _activity() < cutoff, not_(_busy()), not_(_protected())]


def scan(db: Session, server: MT5Server, cfg: dict | None = None) -> dict:
    """The demo groups with their inactive accounts, the accounts that qualify (first PREVIEW) and why some are kept.
    Database only."""
    cfg = cfg or settings_of(server)
    cutoff = utcnow() - timedelta(days=int(cfg["days"]))
    scope, inactive = _in_scope(server, cfg), _activity() < cutoff
    per_group = [{"group": g, "accounts": n, "inactive": idle} for g, n, idle in db.execute(
        select(TradingAccount.group, func.count(), func.count().filter(inactive))
        .where(*scope).group_by(TradingAccount.group).order_by(TradingAccount.group))]
    busy = db.scalar(select(func.count()).select_from(TradingAccount).where(*scope, inactive, _busy()))
    protected = db.scalar(select(func.count()).select_from(TradingAccount).where(*scope, inactive, not_(_busy()),
                                                                                 _protected()))
    where = _candidates(server, cfg, cutoff)
    total = db.scalar(select(func.count()).select_from(TradingAccount).where(*where))
    rows = db.execute(select(TradingAccount, Client).join(Client, Client.id == TradingAccount.client_id)
                      .where(*where).order_by(_activity(), TradingAccount.login).limit(PREVIEW)).all()
    return {"days": int(cfg["days"]), "groups": per_group, "total": total,
            "skipped": {"open positions or orders": busy, "copy trading or PAMM": protected},
            "never_seen": db.scalar(select(func.count()).select_from(TradingAccount).where(
                *where, TradingAccount.last_access_at.is_(None))),
            "candidates": [{"login": a.login, "group": a.group, "name": f"{c.first_name} {c.last_name}",
                            "email": c.email, "last_access": a.last_access_at, "last_trade": a.last_trade_at,
                            "activity": a.last_activity_at or a.created_at, "registration": a.created_at}
                           for a, c in rows]}


# ---------------------------------------------------------------- archive / delete
def run(db: Session, actor, server: MT5Server, gw: MT5Gateway | None = None, logins: list[int] | None = None,
        action: str | None = None) -> dict:
    """Archive or delete the inactive demo accounts (only `logins` of them, when given), at most MAX_PER_RUN."""
    cfg = settings_of(server)
    action = action or cfg["action"]
    if action not in ACTIONS:
        raise ServiceError("Choose archive or delete")
    cutoff = utcnow() - timedelta(days=int(cfg["days"]))
    q = select(TradingAccount.login).where(*_candidates(server, cfg, cutoff))
    if logins is not None:
        q = q.where(TradingAccount.login.in_(logins[:MAX_PER_RUN]))
    targets = list(db.scalars(q.order_by(_activity(), TradingAccount.login).limit(MAX_PER_RUN)))
    total = db.scalar(select(func.count()).select_from(TradingAccount).where(*_candidates(server, cfg, cutoff)))
    gw = gw or get_gateway(server.id)
    try:
        seen = gw.get_last_access() if targets else {}
    except MT5Error as exc:
        raise ServiceError(f"Could not check the accounts on MT5: {exc}", 502) from exc
    done, failed, back = [], {}, []
    for i in range(0, len(targets), BATCH):
        batch = targets[i:i + BATCH]
        try:  # what MT5 says now: used since the CRM's last refresh, or trading?
            busy = {p.login for p in gw.get_positions(batch)} | {o.login for o in gw.get_orders(batch)}
        except MT5Error as exc:
            failed.update({x: f"not checked on MT5: {exc}" for x in batch})
            continue
        used = [x for x in batch if seen.get(x) and seen[x] >= cutoff]
        back += used
        go = [x for x in batch if x not in busy and x not in used and x in seen]
        failed.update({x: "not on MT5" for x in batch if x not in seen})
        if action == "archive":
            try:
                refused = gw.archive_users(go)
            except MT5Error as exc:
                refused = {x: str(exc) for x in go}
        else:
            refused = {}
            for login in go:
                try:
                    gw.delete_user(login)
                except MT5Error as exc:
                    refused[login] = str(exc)
        failed.update(refused)
        done += [x for x in go if x not in refused]
    for i in range(0, len(done), 1000):
        for acc in db.scalars(select(TradingAccount).where(TradingAccount.login.in_(done[i:i + 1000]))):
            acc.status, acc.on_server = AccountStatus.archived, False
    for acc in db.scalars(select(TradingAccount).where(TradingAccount.login.in_(back))) if back else []:
        acc.last_access_at = seen[acc.login]  # connected since the last refresh: active again
        acc.last_activity_at = max(acc.last_activity_at or seen[acc.login], seen[acc.login])
    now = utcnow()
    result = {"at": now.isoformat(), "action": action, "done": len(done), "failed": len(failed),
              "active_again": len(back), "errors": [f"{login}: {why}" for login, why in list(failed.items())[:10]],
              "remaining": max(0, total - len(done) - len(back)) if logins is None else 0,
              "by": getattr(actor, "email", None) or "schedule"}
    server.demo_cleanup = {**(server.demo_cleanup or {}), "last_run": now.isoformat(), "last_result": result}
    audit(db, actor, f"demo_cleanup.{action}", "mt5_server", server.id,
          {**{k: v for k, v in result.items() if k not in ("at", "by")}, "logins": done[:500]})
    db.commit()
    log.info("demo clean-up on '%s': %s %d accounts, %d refused", server.name, action, len(done), len(failed))
    return result


def run_due(db: Session, gw: MT5Gateway | None = None) -> int:
    """Worker: the daily clean-up on every server that has it switched on."""
    ran = 0
    for server in db.scalars(select(MT5Server).where(MT5Server.enabled.is_(True)).order_by(MT5Server.id)).all():
        cfg = settings_of(server)
        last = cfg.get("last_run")
        if not cfg["enabled"] or (last and utcnow() - datetime.fromisoformat(last) < timedelta(hours=23)):
            continue
        try:
            run(db, None, server, gw)
            ran += 1
        except (MT5Error, ServiceError) as exc:
            db.rollback()
            why = getattr(exc, "message", None) or str(exc)
            log.error("demo clean-up on '%s' failed: %s", server.name, why)
            now = utcnow().isoformat()  # tried today: the next attempt is tomorrow, not in a minute
            server.demo_cleanup = {**(server.demo_cleanup or {}), "last_run": now,
                                   "last_result": {"at": now, "error": why[:300], "by": "schedule"}}
            db.commit()
    return ran


# ---------------------------------------------------------------- MT5's own demo inactivity period
def _is_demo_group(db: Session, server: MT5Server, name: str) -> bool:
    if excluded(name):
        return False
    if server.is_demo or name.lower().startswith("demo"):
        return True
    return db.scalar(select(AccountGroup.id).where(AccountGroup.server_id == server.id, AccountGroup.mt5_group == name,
                                                   AccountGroup.is_demo.is_(True))) is not None


def mt5_demo_groups(db: Session, server: MT5Server, gw: MT5Gateway | None = None) -> list[dict]:
    """The server's demo groups on MT5 with their demo inactivity period (days; 0 = MT5 never removes them)."""
    try:
        groups = (gw or get_gateway(server.id)).get_groups("*")
    except MT5Error as exc:
        raise ServiceError(f"Could not read the groups from MT5: {exc}", 502) from exc
    return [{"name": g.name, "days": g.demo_inactivity} for g in groups if _is_demo_group(db, server, g.name)]


def set_mt5_inactivity(db: Session, actor, server: MT5Server, groups: list[str], days: int,
                       gw: MT5Gateway | None = None) -> tuple[list[str], list[str]]:
    """Set MT5's demo inactivity period on demo groups (MT5 then removes inactive demo accounts itself).
    Returns (changed, failures)."""
    if not 0 <= days <= MAX_MT5_DAYS:
        raise ServiceError(f"Use 0 (never) to {MAX_MT5_DAYS} days")
    if not groups:
        raise ServiceError("Choose the groups")
    gw = gw or get_gateway(server.id)
    done, failed = [], []
    for name in groups:
        if not _is_demo_group(db, server, name):
            failed.append(f"{name}: not a demo group")
            continue
        try:
            gw.update_group(name, demo_inactivity=days)
            done.append(name)
        except MT5Error as exc:
            failed.append(f"{name}: {exc}")
    if done:
        audit(db, actor, "demo_cleanup.mt5_inactivity", "mt5_server", server.id, {"groups": done, "days": days})
        db.commit()
    return done, failed
