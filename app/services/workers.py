"""Administration > Workers: what the background worker (app.workers) is doing, as it last wrote it down.

The worker can run in another process, or be stopped, so the page reads what the worker saved with its heartbeat
(app_settings, key KEY) and judges from how old that is.
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import MT5Server, SyncJob, SyncRun
from app.services import app_settings

KEY = "workers"
ALIVE_SECONDS = 60  # no heartbeat for this long: the worker is stopped

# Thread -> (name, what it does, seconds without going round before it counts as stuck)
THREADS = {
    "deal-push": ("Real-time events", "Takes in the deals, positions and orders MT5 pushes the moment they happen.", 60),
    "poller": ("Poller", "Checks every account regularly, catches up after a break, and keeps the links to MT5 up.", 300),
    "scheduler": ("Scheduler", "Runs the jobs listed below, once a minute.", 300),
    "sync-jobs": ("Sync tasks", "Runs the history sync tasks staff queue under Deals, Orders and Positions.", 900),
}


def _age(value: str | None, now: datetime) -> float | None:
    return (now - datetime.fromisoformat(value)).total_seconds() if value else None


def _server_rows(db: Session, saved: dict, alive: bool, now: datetime) -> list[dict]:
    rows = []
    for server in db.scalars(select(MT5Server).order_by(MT5Server.id)):
        link = (saved.get("servers") or {}).get(str(server.id)) or {}
        if not server.enabled:
            state = "disabled"
        elif not alive:
            state = "stopped"
        elif link.get("catching_up"):
            state = "catching up"
        elif not link.get("connected"):
            state = "disconnected"
        elif not (link.get("deals") and link.get("trades")):
            state = "polling only"
        else:
            state = "real time"
        rows.append({"name": server.name, "id": server.id, "state": state, "error": link.get("error", ""),
                     "events": link.get("events", 0), "last_event_at": link.get("last_event_at"),
                     "last_event_age": _age(link.get("last_event_at"), now),
                     "last_catch_up_at": link.get("last_catch_up_at")})
    return rows


def status(db: Session) -> dict:
    saved, now = app_settings.stored(db, KEY), utcnow()
    seen = _age(saved.get("at"), now)
    alive = seen is not None and seen < ALIVE_SECONDS
    threads = []
    for key, (name, text, limit) in THREADS.items():
        age = (saved.get("threads") or {}).get(key)
        if not alive or age is None:
            state = "stopped"
        else:
            state = "stuck" if age > limit else "running"
        threads.append({"name": name, "text": text, "state": state, "age": age})
    steps = [{"name": name, **step, "age": _age(step["at"], now)} for name, step in sorted((saved.get("steps") or {}).items())]
    return {"alive": alive, "ever": bool(saved), "seen": seen, "process": saved.get("process", ""),
            "started_at": saved.get("started_at"), "threads": threads, "servers": _server_rows(db, saved, alive, now),
            "steps": steps, "queued": saved.get("queued", 0), "dropped": saved.get("dropped", 0),
            "syncs": db.scalar(select(func.count()).select_from(SyncRun).where(SyncRun.status.in_(("queued", "running")))) or 0,
            "tasks": db.scalar(select(func.count()).select_from(SyncJob).where(SyncJob.status.in_(("queued", "running")))) or 0}
