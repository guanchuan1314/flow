"""Local demo server: `python -m app.devserver`

Runs the CRM against the MT5 simulator with:
  * the PostgreSQL in DATABASE_URL (setup.ps1: the one in Docker), else a bundled one (data in ./pgdata)
  * realistic demo data (seeded once, kept in PostgreSQL and ./mt5data/mock_state.pkl)
  * a live price feed (prices move every 2 s, so P/L, equity and NAV change)
  * a demo trader on the strategy/PAMM master accounts, whose trades are copied live

Never use this for production; it refuses to run with APP_ENV=prod.
"""

import logging
import os
import random
import threading
from decimal import Decimal

from dotenv import dotenv_values

os.environ.setdefault("MT5_GATEWAY", "mock")
if not (os.environ.get("DATABASE_URL") or dotenv_values(".env").get("DATABASE_URL")):
    # no database configured (setup.ps1 sets up one in Docker): bundled local PostgreSQL, data kept in ./pgdata
    from app.devdb import local_postgres

    os.environ["DATABASE_URL"] = local_postgres("./pgdata", "crm_demo", cleanup_mode="stop")
os.environ.setdefault("MOCK_STATE_FILE", "./mt5data/mock_state.pkl")
os.environ.setdefault("EMBEDDED_WORKER", "true")
os.environ.setdefault("SYNC_INTERVAL_SECONDS", "5")
os.environ.setdefault("PYTHONUTF8", "1")

from app.config import get_settings  # noqa: E402

log = logging.getLogger("devserver")
STEP = {"EURUSD": Decimal("0.00025"), "GBPUSD": Decimal("0.00030"), "XAUUSD": Decimal("0.70"),
        "USDJPY": Decimal("0.030"), "AUDUSD": Decimal("0.00020"), "XAGUSD": Decimal("0.020"),
        "US30": Decimal("12.0"), "NAS100": Decimal("8.0"), "BTCUSD": Decimal("45.00")}
DIGITS = {"EURUSD": Decimal("0.00001"), "GBPUSD": Decimal("0.00001"), "XAUUSD": Decimal("0.01"),
          "USDJPY": Decimal("0.001"), "AUDUSD": Decimal("0.00001"), "XAGUSD": Decimal("0.001"),
          "US30": Decimal("0.1"), "NAS100": Decimal("0.1"), "BTCUSD": Decimal("0.01")}
# The simulator's P/L maths is exact for USD-quoted symbols, so the demo trader sticks to those.
TRADE_SYMBOLS = ["EURUSD", "GBPUSD", "XAUUSD", "AUDUSD"]


def simulator():
    """Gateway of the CRM's MT5 server when it is the simulator, else None."""
    from app.mt5 import MT5Error, get_gateway
    from app.mt5.mock import MockGateway

    try:
        gw = get_gateway()
    except MT5Error:
        log.exception("MT5 simulator unavailable")
        return None
    return gw if isinstance(gw, MockGateway) else None


def market_feed(stop: threading.Event) -> None:
    rng = random.Random()
    while not stop.wait(2):
        moves = {symbol: (Decimal(rng.randint(-10, 10)) / 10 * step).quantize(DIGITS[symbol])
                 for symbol, step in STEP.items()}
        gw = simulator()
        if gw is not None:
            _apply(gw, moves)


def _apply(gw, moves) -> None:
    for symbol, move in moves.items():
        bid, ask = gw.prices[symbol]
        gw.set_price(symbol, bid + move, ask + move)


def _pending_orders(db_login: int, rng: random.Random) -> None:
    """Now and then place or cancel a limit order, so the Orders page has open and cancelled orders."""
    gw = simulator()
    if gw is None:  # a real server: the demo never places orders there
        return
    mine = [o.ticket for o in gw.get_orders([db_login])]
    if mine and rng.random() < 0.5:
        gw.cancel_order(rng.choice(mine))
    elif rng.random() < 0.4:
        symbol = rng.choice(TRADE_SYMBOLS)
        bid, ask = gw.prices[symbol]
        buy = rng.random() < 0.5
        price = (ask - STEP[symbol] * 40) if buy else (bid + STEP[symbol] * 40)
        gw.place_order(db_login, symbol, 2 if buy else 3, Decimal("1"), price.quantize(DIGITS[symbol]),
                       comment="demo limit")


def demo_trader(stop: threading.Event, every: int = 45) -> None:
    """Opens/closes trades on master accounts so copy trading and PAMM visibly work.
    Simulator only: on a real MT5 server it never trades."""
    from app.db import SessionLocal
    from app.devseed import demo_masters

    rng = random.Random()
    while not stop.wait(every):
        try:
            gw = simulator()
            if gw is None:  # a real server: the demo never trades there
                continue
            with SessionLocal() as db:
                masters = [login for login in demo_masters(db) if login in gw.users]
            if not masters:
                continue
            login = rng.choice(masters)
            _pending_orders(db_login=login, rng=rng)
            mine = [p for p in gw.get_positions([login])]
            if len(mine) >= 3 or (mine and rng.random() < 0.45):
                p = rng.choice(mine)
                gw.close_position(login, p.position_id, p.symbol, p.action, p.volume, comment="demo trader")
                log.info("demo trader closed %s %s on %s", p.symbol, p.volume, login)
            else:
                symbol = rng.choice(TRADE_SYMBOLS)
                lots = Decimal(rng.choice(["0.5", "1", "1.5", "2"]))
                gw.open_position(login, symbol, rng.choice([0, 1]), lots, comment="demo trader")
                log.info("demo trader opened %s %s on %s", symbol, lots, login)
        except Exception:  # noqa: BLE001 - demo only, keep running
            log.exception("demo trader step failed")


def main() -> None:
    import uvicorn

    s = get_settings()
    if s.is_prod:
        raise SystemExit("devserver is for local demos only; it refuses to run with APP_ENV=prod")

    from app.core.logging_setup import configure_logging
    from app.db import migrate
    from app.devseed import ADMIN_PASSWORD, CLIENT_PASSWORD, DOMAIN, STAFF_PASSWORD, ensure_methods, seed
    from app.services import events

    configure_logging("devserver")
    migrate()
    events.register_default_handlers()
    first_run = bool(seed())
    ensure_methods()
    from app.db import SessionLocal
    from app.devseed import demo_masters

    with SessionLocal() as db:
        masters = demo_masters(db)
    gw = simulator()
    if gw is not None:
        if masters and masters[0] in gw.users:  # its trades come from an "Expert Advisor" (MT5 > Trader styles)
            gw.deal_reasons.setdefault(masters[0], 1)
        gw.save()

    stop = threading.Event()
    threading.Thread(target=market_feed, args=(stop,), daemon=True, name="market").start()
    threading.Thread(target=demo_trader, args=(stop,), daemon=True, name="demo-trader").start()

    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8000"))
    print(f"""
==================================================================
 {s.app_name} is running {'(demo data seeded)' if first_run else ''}
   Staff back-office : http://{host}:{port}/admin
   Client portal     : http://{host}:{port}/portal
   API docs          : http://{host}:{port}/docs

 Staff logins (password {STAFF_PASSWORD}, admin: {ADMIN_PASSWORD})
   admin@{DOMAIN}     Admin - everything
   manager@{DOMAIN}   Manager
   sam@{DOMAIN}       Sales (sees own clients)
   finance@{DOMAIN}   Finance - approves payments
   support@{DOMAIN} / dealer@{DOMAIN}

 Client logins (password {CLIENT_PASSWORD})
   jane.doe@example.com      copies Kenji's strategy, has a demo account
   kenji.watanabe@example.com strategy provider
   marco.rossi@example.com   PAMM investor (referred by an IB)
   priya.shah@example.com    master IB with commissions
   emma.schmidt@example.com  KYC pending

 Press Ctrl+C to stop. Data persists in PostgreSQL (pgdata/) + mt5data/.
==================================================================
""", flush=True)
    try:
        uvicorn.run("app.main:app", host=host, port=port, log_level="warning")
    finally:
        stop.set()
        gw = simulator()
        if gw is not None:
            gw.save()


if __name__ == "__main__":
    main()
