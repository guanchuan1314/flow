"""Account activity kept in the CRM: each trading account's last terminal connection and last trade.

* Connections: the worker reads every account's LastAccess from the Manager API's local copy of the accounts
  (kept up to date by MT5 itself) every ACTIVITY_REFRESH_MINUTES, and writes the ones that changed.
* Trades: every buy / sell deal the CRM ingests (pushed or polled) moves the account's last trade forward.
* last_activity_at is the later of the two (the registration for an account that never did either). The inactive
  accounts list (MT5 servers > Inactive accounts) is a query on it, so it needs no MT5 request at all."""

from __future__ import annotations

import logging
import time
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import MT5Server, TradingAccount
from app.mt5 import MT5Error, MT5Gateway, get_gateway

log = logging.getLogger(__name__)
TRADES = (0, 1)  # DEAL_BUY, DEAL_SELL
BATCH = 1000

_seen: dict[int, dict[int, datetime | None]] = {}  # server_id -> login -> last access already written
_last_refresh: dict[int, float] = {}

_TRADE_SQL = text("""
    UPDATE trading_accounts
       SET last_trade_at = GREATEST(COALESCE(last_trade_at, :t), :t),
           last_activity_at = GREATEST(COALESCE(last_activity_at, :t), :t)
     WHERE login = :login AND server_id = :server_id""")
_ACCESS_SQL = text("""
    UPDATE trading_accounts
       SET last_access_at = :t,
           last_activity_at = GREATEST(COALESCE(last_activity_at, :t), :t)
     WHERE login = :login AND server_id = :server_id""")


def record_trades(db: Session, deals, server_id: int) -> None:
    """Move the last trade forward for the accounts of these deals of a server (rows or MT5Deals); the caller commits."""
    latest: dict[int, datetime] = {}
    for d in deals:
        if d.action in TRADES:
            latest[d.login] = max(latest.get(d.login, d.time), d.time)
    if latest:
        db.execute(_TRADE_SQL, [{"login": login, "t": t, "server_id": server_id} for login, t in latest.items()])


def refresh_access(db: Session, server: MT5Server, gw: MT5Gateway | None = None) -> int:
    """Write the last terminal connections that changed since the last refresh. Returns how many were written."""
    access = (gw or get_gateway(server.id)).get_last_access()
    seen = _seen.get(server.id)
    if seen is None:  # first refresh in this process: compare with what the CRM has
        seen = dict(db.execute(select(TradingAccount.login, TradingAccount.last_access_at)
                               .where(TradingAccount.server_id == server.id)).all())
        _seen[server.id] = seen
    changed = [(login, t) for login, t in access.items() if t is not None and seen.get(login) != t]
    for i in range(0, len(changed), BATCH):
        db.execute(_ACCESS_SQL, [{"login": login, "t": t, "server_id": server.id} for login, t in changed[i:i + BATCH]])
        db.commit()
    seen.update(changed)
    return len(changed)


def refresh_due(db: Session, gw: MT5Gateway | None = None) -> int:
    """Worker: refresh every enabled server whose turn it is (ACTIVITY_REFRESH_MINUTES)."""
    minutes = get_settings().activity_refresh_minutes
    if minutes <= 0:
        return 0
    written = 0
    for server in db.scalars(select(MT5Server).where(MT5Server.enabled.is_(True)).order_by(MT5Server.id)).all():
        if time.monotonic() - _last_refresh.get(server.id, -1e9) < minutes * 60:
            continue
        _last_refresh[server.id] = time.monotonic()
        try:
            n = refresh_access(db, server, gw)
            if n:
                log.info("activity: %d last connection(s) updated on '%s'", n, server.name)
            written += n
        except MT5Error as exc:
            db.rollback()
            log.warning("activity of '%s' not refreshed: %s", server.name, exc)
    return written


def forget() -> None:
    """Drop the per-process caches (tests, or after accounts were imported in bulk)."""
    _seen.clear()
    _last_refresh.clear()
