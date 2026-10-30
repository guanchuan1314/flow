"""Public market data for the website and client area.

Which symbols are shown, in which order and under which name is set under MT5 > Symbols & pricing > Website
prices (stored in app_settings); until then PUBLIC_SYMBOLS from .env. Quotes are cached briefly so anonymous
website traffic can never turn into a flood of requests against the MT5 server.
"""

import threading
import time
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.audit import audit
from app.core.errors import ServiceError
from app.models import (
    CopyStrategy, CopySubscription, Deal, MT5Server, StrategyStatus, SubscriptionStatus, Symbol, TradingAccount,
)
from app.models.trading import CLOSING_ENTRIES, TRADE_ACTIONS
from app.mt5 import MT5Error, get_gateway
from app.services import app_settings

WEBSITE_KEY = "website_prices"
MAX_SYMBOLS = 30
CARD_SYMBOLS = 6  # the home page and client area price cards show the first six
CACHE_SECONDS = 0.5  # the live stream reads every 0.5 s; page loads in between share the read
INSTRUMENTS = {
    # Names for common symbols when MT5 has no description; decimals come from MT5's symbol when it is imported.
    # symbol: (display name, category, decimals shown)
    "EURUSD": ("Euro / US Dollar", "Forex", 5), "GBPUSD": ("British Pound / US Dollar", "Forex", 5),
    "USDJPY": ("US Dollar / Japanese Yen", "Forex", 3), "AUDUSD": ("Australian Dollar / US Dollar", "Forex", 5),
    "XAUUSD": ("Gold", "Metals", 2), "XAGUSD": ("Silver", "Metals", 3),
    "US30": ("Wall Street 30", "Indices", 1), "NAS100": ("US Tech 100", "Indices", 1),
    "BTCUSD": ("Bitcoin", "Crypto", 2),
}

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "quotes": {}, "previous": {}}


def _symbol_rows(db: Session) -> dict[str, Symbol]:
    """The MT5 server's symbols the CRM knows (MT5 > Trading setup > Symbols), by name."""
    server = db.scalar(select(MT5Server).order_by(MT5Server.id))
    if server is None:
        return {}
    return {s.name: s for s in db.scalars(select(Symbol).where(Symbol.server_id == server.id))}


def default_display(symbol: str, row: Symbol | None) -> tuple[str, str, int]:
    """(name, category, digits) when nothing is entered: MT5's description and folder, else the built-in names."""
    name, category, digits = INSTRUMENTS.get(symbol, (symbol, "Other", 5))
    if row is not None:
        digits = row.digits
        name = name if symbol in INSTRUMENTS else (row.description or symbol)
        folder = row.path.rsplit("\\", 1)[0] if "\\" in (row.path or "") else ""
        category = category if symbol in INSTRUMENTS else (folder.split("\\")[0] or "Other")
    return name, category, digits


def instruments() -> list[dict]:
    """The website's symbols in order: [{symbol, name, category, digits}] (cached per process)."""
    with _lock:
        hit = _cache.get("instruments")
        if hit and time.monotonic() - hit[0] < app_settings.CACHE_SECONDS:
            return hit[1]
    from app.db import SessionLocal

    saved = app_settings.load(WEBSITE_KEY).get("symbols")
    entries = saved if saved else [{"symbol": s} for s in _env_symbols()]
    try:
        with SessionLocal() as db:
            rows = _symbol_rows(db)
    except Exception:  # noqa: BLE001 - before the first migration
        rows = {}
    out = []
    for e in entries:
        name, category, digits = default_display(e["symbol"], rows.get(e["symbol"]))
        out.append({"symbol": e["symbol"], "name": e.get("name") or name, "category": e.get("category") or category,
                    "digits": digits})
    with _lock:
        _cache["instruments"] = (time.monotonic(), out)
    return out


def _env_symbols() -> list[str]:
    return [s.strip().upper() for s in get_settings().public_symbols.split(",") if s.strip()]


def symbols() -> list[str]:
    return [i["symbol"] for i in instruments()]


def website_form(db: Session) -> dict:
    """For MT5 > Symbols & pricing: the saved rows (or the .env list) and the server's symbols to pick from."""
    rows = _symbol_rows(db)
    saved = app_settings.stored(db, WEBSITE_KEY).get("symbols")
    entries = saved if saved else [{"symbol": s} for s in _env_symbols()]
    for e in entries:
        e["default_name"], e["default_category"], _ = default_display(e["symbol"], rows.get(e["symbol"]))
        e["known"] = not rows or e["symbol"] in rows
    return {"entries": entries, "available": sorted(rows), "from_env": not saved}


def save_website_symbols(db: Session, staff, rows: list[dict], ip: str | None = None) -> int:
    """Save the website's price list: [{symbol, name, category}] in display order."""
    known = _symbol_rows(db)
    out, seen = [], set()
    for r in rows:
        symbol = (r.get("symbol") or "").strip()
        if not symbol:
            continue
        if symbol in seen:
            raise ServiceError(f"{symbol} is listed twice")
        if known and symbol not in known:
            raise ServiceError(f"{symbol} is not a symbol on the MT5 server (import symbols first, or check the exact name)")
        seen.add(symbol)
        out.append({"symbol": symbol, "name": (r.get("name") or "").strip()[:64],
                    "category": (r.get("category") or "").strip()[:32]})
    if not out:
        raise ServiceError("Pick at least one symbol for the website")
    if len(out) > MAX_SYMBOLS:
        raise ServiceError(f"At most {MAX_SYMBOLS} symbols")
    app_settings.save(db, WEBSITE_KEY, {"symbols": out})
    audit(db, staff, "website_prices.update", "app_settings", WEBSITE_KEY, {"symbols": [o["symbol"] for o in out]}, ip=ip)
    db.commit()
    app_settings.clear(WEBSITE_KEY)
    with _lock:
        _cache.pop("instruments", None)
    return len(out)


def quotes() -> list[dict]:
    """[{symbol, name, category, bid, ask, spread, direction}] — direction vs. the previous refresh."""
    insts = instruments()  # before taking the lock: instruments() uses it too
    now = time.monotonic()
    with _lock:
        if now - _cache["at"] >= CACHE_SECONDS:
            try:
                fresh = get_gateway().get_quotes([i["symbol"] for i in insts])
            except MT5Error:
                fresh = {}
            if fresh:
                _cache["previous"] = _cache["quotes"] or fresh
                _cache["quotes"] = fresh
            _cache["at"] = now
        current, previous = dict(_cache["quotes"]), dict(_cache["previous"])
    out = []
    for inst in insts:
        symbol, name, category, digits = inst["symbol"], inst["name"], inst["category"], inst["digits"]
        if symbol not in current:
            continue
        bid, ask = current[symbol]
        prev_bid = previous.get(symbol, (bid, ask))[0]
        point = Decimal(1).scaleb(-digits)
        out.append({
            "symbol": symbol, "name": name, "category": category, "digits": digits,
            "bid": f"{bid:,.{digits}f}", "ask": f"{ask:,.{digits}f}",
            "spread": f"{(ask - bid) / point / (10 if digits in (3, 5) else 1):.1f}",
            "direction": "up" if bid > prev_bid else "down" if bid < prev_bid else "flat",
        })
    return out


def strategy_stats(db: Session) -> list[dict]:
    """Public performance summary of active, listed copy-trading strategies."""
    rows = []
    for s in db.scalars(select(CopyStrategy).where(CopyStrategy.is_public.is_(True),
                                                   CopyStrategy.status == StrategyStatus.active)):
        closed = select(Deal).where(Deal.login == s.master_login, Deal.action.in_(TRADE_ACTIONS),
                                    Deal.entry.in_(CLOSING_ENTRIES)).subquery()
        trades, wins, pnl = db.execute(select(func.count(), func.sum(case((closed.c.profit > 0, 1), else_=0)),
                                              func.coalesce(func.sum(closed.c.profit), 0))).one()
        followers = db.scalar(select(func.count()).select_from(CopySubscription).where(
            CopySubscription.strategy_id == s.id, CopySubscription.status != SubscriptionStatus.stopped))
        acc = db.scalar(select(TradingAccount).where(TradingAccount.login == s.master_login))
        equity = Decimal(acc.equity) if acc else Decimal(0)
        rows.append({"strategy": s, "trades": trades or 0, "win_rate": round(100 * (wins or 0) / trades) if trades else None,
                     "profit": Decimal(pnl), "followers": followers, "equity": equity})
    return sorted(rows, key=lambda r: r["profit"], reverse=True)
