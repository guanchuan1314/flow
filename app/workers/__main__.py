"""Standalone worker: `python -m app.workers` (one for the whole CRM).

Supervises its threads: if one dies, it stops and exits with code 1 so the service manager (NSSM) restarts it.
"""

import logging
import os
import signal
import threading

from app.config import get_settings
from app.core.logging_setup import configure_logging
from app.db import migrate, schema_is_current
from app.workers.runner import Worker

log = logging.getLogger("worker")


def supervise(worker: Worker, done: threading.Event, every: float = 5.0) -> int:
    """Wait until `done` is set (exit code 0) or a worker thread dies (exit code 1)."""
    while not done.wait(every):
        dead = worker.dead_threads()
        if dead:
            log.critical("worker thread(s) %s stopped unexpectedly; exiting so the service restarts", dead)
            return 1
    return 0


def main() -> None:
    os.environ.setdefault("CRM_COMPONENT", "worker")
    configure_logging("worker")
    s = get_settings()
    if s.auto_migrate:
        migrate()
    elif not schema_is_current():
        raise SystemExit("Database schema is not up to date - run `python -m app.cli migrate` first")
    worker = Worker().start()
    done = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: done.set())
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    code = supervise(worker, done)
    worker.shutdown()
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
