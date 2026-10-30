"""MT5 server connections, managed from the admin dashboard."""

import re

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.audit import audit
from app.core.errors import Conflict, ServiceError
from app.core.security import encrypt_secret
from app.db import utcnow
from app.models import (
    AccountGroup, CommissionRule, Deal, IBCommission, IpRule, LiquidityConnection, MT5Server, Order, PaymentRequest,
    PaymentStatus, Position, RoutingRule, ServerHealthSample, ServerKind, Symbol, SyncJob, SyncState, TradingAccount,
)
from app import mt5

ONE_SERVER = "The CRM works with one MT5 server: change '{name}' instead of adding another."
ADDRESS_RE = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$")


def the_server(db: Session, enabled_only: bool = True) -> MT5Server | None:
    """The CRM's MT5 server (there is one), or None before it is set up (or while it is disabled)."""
    q = select(MT5Server).order_by(MT5Server.id).limit(1)
    return db.scalar(q.where(MT5Server.enabled.is_(True)) if enabled_only else q)


def require_server(db: Session) -> MT5Server:
    server = the_server(db)
    if server is None:
        raise ServiceError("No enabled MT5 server")
    return server


def _validate(db: Session, server: MT5Server | None, *, name: str, kind: ServerKind, address: str,
              manager_login: int | None, password: str | None) -> None:
    if not name.strip():
        raise ServiceError("Name is required")
    clash = db.scalar(select(MT5Server).where(MT5Server.name == name.strip()))
    if clash is not None and (server is None or clash.id != server.id):
        raise Conflict(f"A server named '{name}' already exists")
    if kind == ServerKind.simulator:
        if get_settings().is_prod:
            raise ServiceError("Simulator servers are not allowed in production")
        return
    if not ADDRESS_RE.match(address.strip()):
        raise ServiceError("Address must look like mt5.yourbroker.com:443 or 203.0.113.10:443")
    if not manager_login or manager_login <= 0:
        raise ServiceError("Manager login is required")
    if server is None and not password:
        raise ServiceError("Manager password is required")


def create_server(db: Session, actor, *, name: str, kind: ServerKind, address: str = "",
                  manager_login: int | None = None, password: str | None = None, terminal_name: str = "",
                  timeout_ms: int = 30000, enabled: bool = True, notes: str = "") -> MT5Server:
    """The CRM's MT5 server: there is only one. Live and demo accounts are both on it, in their own groups."""
    existing = the_server(db, enabled_only=False)
    if existing is not None:
        raise Conflict(ONE_SERVER.format(name=existing.name))
    _validate(db, None, name=name, kind=kind, address=address, manager_login=manager_login, password=password)
    server = MT5Server(name=name.strip(), kind=kind, is_demo=False, address=address.strip(),
                       manager_login=manager_login or 0, manager_password_enc=encrypt_secret(password or ""),
                       terminal_name=terminal_name.strip(), timeout_ms=max(1000, timeout_ms), enabled=enabled,
                       is_price_feed=True, notes=notes.strip())
    db.add(server)
    db.flush()
    audit(db, actor, "mt5_server.create", "mt5_server", server.id,
          {"name": server.name, "kind": kind, "address": server.address, "manager_login": server.manager_login})
    db.commit()
    return server


def update_server(db: Session, actor, server: MT5Server, *, name: str, kind: ServerKind,
                  address: str, manager_login: int | None, password: str | None, terminal_name: str,
                  timeout_ms: int, enabled: bool, notes: str) -> MT5Server:
    """`password` empty/None keeps the stored one."""
    _validate(db, server, name=name, kind=kind, address=address, manager_login=manager_login, password=password)
    in_use = db.scalar(select(TradingAccount.id).where(TradingAccount.server_id == server.id)) is not None
    if in_use and kind != server.kind:
        raise ServiceError("This server already has trading accounts; its type cannot be changed")
    changes = {"name": name.strip(), "kind": kind, "address": address.strip(),
               "manager_login": manager_login or 0, "terminal_name": terminal_name.strip(),
               "timeout_ms": max(1000, timeout_ms), "enabled": enabled, "notes": notes.strip()}
    before = mt5._fingerprint(server)
    for key, value in changes.items():
        setattr(server, key, value)
    if password:
        server.manager_password_enc = encrypt_secret(password)
    audit(db, actor, "mt5_server.update", "mt5_server", server.id,
          {**changes, "password_changed": bool(password)})
    db.commit()
    if mt5._fingerprint(server) != before:
        mt5.forget(server.id)  # connection settings changed: reconnect now (other processes follow within seconds)
    return server


def test_server(db: Session, actor, server: MT5Server) -> tuple[bool, str]:
    ok, message = mt5.test_connection(server)
    server.last_status = "ok" if ok else "error"
    server.last_error = None if ok else message[:512]
    server.last_checked_at = utcnow()
    audit(db, actor, "mt5_server.test", "mt5_server", server.id, {"ok": ok, "message": message[:200]})
    db.commit()
    return ok, message


def crm_data(db: Session, server: MT5Server) -> dict[str, int]:
    """What the CRM stores for a server (shown before deleting it)."""
    def count(model, col):
        return db.scalar(select(func.count()).select_from(model).where(col == server.id)) or 0
    return {"account groups": count(AccountGroup, AccountGroup.server_id),
            "trading accounts": count(TradingAccount, TradingAccount.server_id),
            "deals": count(Deal, Deal.server_id), "orders": count(Order, Order.server_id),
            "positions": count(Position, Position.server_id),
            "IB commissions": count(IBCommission, IBCommission.server_id),
            "symbols": count(Symbol, Symbol.server_id),
            "gateways and data feeds": count(LiquidityConnection, LiquidityConnection.server_id),
            "routing rules": count(RoutingRule, RoutingRule.server_id)}


def delete_server(db: Session, actor, server: MT5Server, purge: bool = False) -> dict[str, int]:
    """Remove the server from the CRM. Nothing is ever deleted on the MT5 server itself.

    With `purge`, the CRM's own copy of that server's data goes too: account groups, trading accounts,
    deals, orders, positions, sync state, paid IB commission records, symbols, gateways and data feeds,
    routing rules. Clients, wallets and the payment and audit history stay. Without it, a server that has
    data cannot be deleted (disable it).
    """
    from app.services.accounts import _copy_links

    data = crm_data(db, server)
    if any(data.values()) and not purge:
        raise ServiceError("This server has data in the CRM. Disable it, or delete it together with its CRM data")
    logins = list(db.scalars(select(TradingAccount.login).where(TradingAccount.server_id == server.id)))
    groups = select(AccountGroup.id).where(AccountGroup.server_id == server.id)
    blockers = _copy_links(db, logins) if logins else []
    if db.scalar(select(func.count()).select_from(CommissionRule).where(CommissionRule.account_group_id.in_(groups))):
        blockers.append("IB commission rules use its account groups")
    if db.scalar(select(func.count()).select_from(IBCommission).where(IBCommission.server_id == server.id,
                                                                     IBCommission.status == "pending")):
        blockers.append("unpaid IB commissions (pay or void them first)")
    if logins and db.scalar(select(func.count()).select_from(PaymentRequest).where(
            PaymentRequest.trading_login.in_(logins), PaymentRequest.status == PaymentStatus.pending)):
        blockers.append("pending deposits / withdrawals for its accounts")
    if blockers:
        raise ServiceError("Resolve these first: " + "; ".join(blockers[:6]))
    for model, col in ((Order, Order.server_id), (Position, Position.server_id), (Deal, Deal.server_id),
                       (SyncState, SyncState.server_id), (SyncJob, SyncJob.server_id),
                       (IBCommission, IBCommission.server_id), (TradingAccount, TradingAccount.server_id),
                       (AccountGroup, AccountGroup.server_id), (Symbol, Symbol.server_id),
                       (LiquidityConnection, LiquidityConnection.server_id), (RoutingRule, RoutingRule.server_id),
                       (ServerHealthSample, ServerHealthSample.server_id)):
        db.execute(delete(model).where(col == server.id))
    if logins:
        db.execute(delete(SyncState).where(SyncState.login.in_(logins)))
    for rule in db.scalars(select(IpRule)):
        if server.id in (rule.mt5_server_ids or []):
            rule.mt5_server_ids = [x for x in rule.mt5_server_ids if x != server.id]
    audit(db, actor, "mt5_server.delete", "mt5_server", server.id,
          {"name": server.name, "address": server.address, "crm_data_deleted": data if purge else {}})
    db.delete(server)
    db.commit()
    mt5.forget(server.id)
    return data


def usage(db: Session) -> dict[int, dict]:
    accounts = dict(db.execute(select(TradingAccount.server_id, func.count()).group_by(TradingAccount.server_id)).all())
    groups = dict(db.execute(select(AccountGroup.server_id, func.count()).group_by(AccountGroup.server_id)).all())
    return {sid: {"accounts": accounts.get(sid, 0), "groups": groups.get(sid, 0)}
            for sid in set(accounts) | set(groups)}
