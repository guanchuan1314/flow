"""MT5 server access.

Servers are configured in the database (Admin -> MT5 servers). The registry keeps one
gateway per server and rebuilds it when the server's settings change, which also lets
separate processes (API, worker) pick up edits without a restart.

Never import MT5Manager outside app.mt5.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from app.config import get_settings
from app.mt5.gateway import BUY, SELL, MT5Error, MT5Gateway

log = logging.getLogger(__name__)

CONFIG_TTL_SECONDS = 5.0  # how often a cached gateway re-checks its server row for edits

_lock = threading.RLock()
_gateways: dict[int, tuple[MT5Gateway, tuple]] = {}
_checked: dict[int, float] = {}
_override: MT5Gateway | None = None  # tests: one gateway for every server


def _fingerprint(server) -> tuple:
    return (server.kind.value, server.address, server.manager_login, server.manager_password_enc,
            server.timeout_ms, server.enabled)


def build_gateway(server, work_dir: str | None = None) -> MT5Gateway:
    """Create (not connect) a gateway for a server row. `work_dir` overrides the Manager API's working folder."""
    s = get_settings()
    if server.kind.value == "simulator":
        if s.is_prod:
            raise MT5Error(f"server '{server.name}' is a simulator; simulators are disabled in production")
        from app.mt5.mock import MockGateway

        state = None
        if s.mock_state_file:
            base = Path(s.mock_state_file)
            state = str(base if server.id == 1 else base.with_name(f"{base.stem}_{server.id}{base.suffix}"))
        # distinct login ranges per simulated server, like a real multi-server setup
        return MockGateway(first_login=500000 + (server.id - 1) * 100000, state_file=state)
    from app.core.security import decrypt_secret
    from app.mt5.manager import ManagerGateway

    component = os.environ.get("CRM_COMPONENT", "api")
    return ManagerGateway(server.address, int(server.manager_login), decrypt_secret(server.manager_password_enc),
                          server.timeout_ms,
                          work_dir=work_dir or str(Path(s.mt5_work_dir) / f"{component}-server{server.id}"))


def _load_server(server_id: int | None):
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import MT5Server

    with SessionLocal() as db:
        if server_id is not None:
            server = db.get(MT5Server, server_id)
        else:
            server = db.scalar(select(MT5Server).where(MT5Server.enabled.is_(True))
                               .order_by(MT5Server.is_price_feed.desc(), MT5Server.is_demo, MT5Server.id))
        if server is None:
            raise MT5Error(f"MT5 server {server_id if server_id is not None else '(default)'} is not configured")
        db.expunge(server)
        return server


def get_gateway(server_id: int | None = None, connect: bool = True) -> MT5Gateway:
    """Gateway for a server (None = the default/price-feed server)."""
    if _override is not None:
        _override.ensure_connected()
        return _override
    with _lock:
        cached = _gateways.get(server_id) if server_id is not None else None
        fresh = server_id is not None and time.monotonic() - _checked.get(server_id, 0) < CONFIG_TTL_SECONDS
        if cached and fresh:
            gw = cached[0]
        else:
            server = _load_server(server_id)
            if not server.enabled:
                raise MT5Error(f"MT5 server '{server.name}' is disabled")
            cached = _gateways.get(server.id)
            if cached is None or cached[1] != _fingerprint(server):
                if cached is not None:
                    log.info("MT5 server '%s' settings changed; reconnecting", server.name)
                    _safe_disconnect(cached[0])
                cached = (build_gateway(server), _fingerprint(server))
                _gateways[server.id] = cached
            _checked[server.id] = time.monotonic()
            gw = cached[0]
    if connect:
        gw.ensure_connected()
    return gw


def _safe_disconnect(gw: MT5Gateway) -> None:
    try:
        gw.disconnect()
    except Exception:  # noqa: BLE001 - best effort
        log.exception("error while disconnecting gateway")


def forget(server_id: int) -> None:
    """Drop a cached gateway (server edited or deleted)."""
    with _lock:
        cached = _gateways.pop(server_id, None)
        _checked.pop(server_id, None)
    if cached:
        _safe_disconnect(cached[0])


def peek(server_id: int) -> MT5Gateway | None:
    """Cached gateway without connecting (health checks)."""
    if _override is not None:
        return _override
    cached = _gateways.get(server_id)
    return cached[0] if cached else None


def test_connection(server) -> tuple[bool, str]:
    """Connect with a throw-away gateway; used by the admin 'Test connection' button.

    It gets its own temporary working folder so it never shares the Manager API's files with the live gateway."""
    if _override is not None:
        return True, "ok (test gateway)"
    import shutil
    import tempfile

    work_dir = tempfile.mkdtemp(prefix="mt5-test-")
    try:
        gw = build_gateway(server, work_dir=work_dir)
        gw.connect()
        gw.disconnect()
        return True, "Connected successfully"
    except Exception as exc:  # noqa: BLE001 - report any failure to the admin
        return False, str(exc)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------- routing helpers
def for_account(account) -> MT5Gateway:
    return get_gateway(account.server_id)


def for_login(db, login: int, server_id: int | None = None) -> MT5Gateway:
    """The gateway of an account. Without `server_id`, of the one server Flow knows the login on."""
    from sqlalchemy import select

    from app.models import TradingAccount

    if server_id is not None:
        return get_gateway(server_id)
    servers = list(db.scalars(select(TradingAccount.server_id).where(TradingAccount.login == login)))
    if not servers:
        raise MT5Error(f"trading account {login} is not known to the CRM")
    if len(servers) > 1:
        raise MT5Error(f"login {login} is on more than one MT5 server, so its server has to be given")
    return get_gateway(servers[0])


def for_account_group(db, account_group_id: int) -> MT5Gateway:
    from app.models import AccountGroup

    ag = db.get(AccountGroup, account_group_id)
    if ag is None:
        raise MT5Error(f"account group {account_group_id} not found")
    return get_gateway(ag.server_id)


def for_payment(db, payment) -> MT5Gateway | None:
    return for_login(db, payment.trading_login, payment.server_id) if payment.trading_login else None


def routed():
    """Gateway that forwards every call to the MT5 server of the account it is about."""
    from app.mt5.routing import routed as _routed

    return _routed()


def set_gateway(gateway: MT5Gateway | None) -> None:
    """Tests: route every server to one gateway (None restores normal routing)."""
    global _override
    with _lock:
        _override = gateway
        for server_id in list(_gateways):
            cached = _gateways.pop(server_id)
            if cached[0] is not gateway:
                _safe_disconnect(cached[0])
        _checked.clear()
    from app.mt5.routing import forget_logins

    forget_logins()


__all__ = ["BUY", "SELL", "MT5Error", "MT5Gateway", "for_account", "for_account_group", "for_login", "for_payment",
           "forget", "get_gateway", "peek", "routed", "set_gateway", "test_connection"]
