"""Shared wrappers for MT5 calls made by the services: MT5 errors become ServiceError (HTTP 502)."""

from __future__ import annotations

from app.core.errors import ServiceError
from app.models import MT5Server
from app.mt5 import MT5Error, MT5Gateway
from app.mt5.gateway import RET_NOT_FOUND

__all__ = ["RET_NOT_FOUND", "call", "delete_tolerant", "gateway"]


def gateway(server: MT5Server | int, gw: MT5Gateway | None = None) -> MT5Gateway:
    """The given gateway, or the cached one for the server (connecting if needed)."""
    if gw is not None:
        return gw
    from app.mt5 import get_gateway

    try:
        return get_gateway(server if isinstance(server, int) else server.id)
    except MT5Error as exc:
        raise ServiceError(f"MT5 connection failed: {exc}", 502) from exc


def call(fn, what: str):
    """Run fn(); an MT5 error becomes "MT5 <what> failed: ..."."""
    try:
        return fn()
    except MT5Error as exc:
        raise ServiceError(f"MT5 {what} failed: {exc}", 502) from exc


def delete_tolerant(fn, what: str) -> None:
    """Like call(), but "not found" on MT5 counts as deleted (only the CRM's copy is left to remove)."""
    try:
        fn()
    except MT5Error as exc:
        if exc.retcode != RET_NOT_FOUND:
            raise ServiceError(f"MT5 {what} failed: {exc}", 502) from exc
