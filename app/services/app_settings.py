"""Settings edited in the back-office, stored as JSON in app_settings (one row per key).

load() is for readers anywhere (templates, the price stream): it opens its own session, caches for
CACHE_SECONDS per process and returns {} before the first migration. save() writes in the caller's
session and clears the cache, so this process sees the change at once and the others within CACHE_SECONDS.
"""

import threading
import time

from sqlalchemy.orm import Session

CACHE_SECONDS = 30

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}


def load(key: str) -> dict:
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    from app.db import SessionLocal
    from app.models import AppSetting

    try:
        with SessionLocal() as db:
            row = db.get(AppSetting, key)
            value = dict(row.value) if row else {}
    except Exception:  # noqa: BLE001 - before the first migration the table does not exist yet
        return {}
    with _lock:
        _cache[key] = (time.monotonic(), value)
    return value


def stored(db: Session, key: str) -> dict:
    """The saved value, read in the caller's session (for forms)."""
    from app.models import AppSetting

    row = db.get(AppSetting, key)
    return dict(row.value) if row else {}


def save(db: Session, key: str, value: dict) -> dict:
    """Replace the value (not committed: the caller commits with its audit entry). Returns the previous value."""
    from app.models import AppSetting

    row = db.get(AppSetting, key) or AppSetting(key=key, value={})
    before = dict(row.value or {})
    row.value = value
    db.add(row)
    clear(key)
    return before


def clear(key: str | None = None) -> None:
    with _lock:
        if key is None:
            _cache.clear()
        else:
            _cache.pop(key, None)
