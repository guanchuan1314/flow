import logging
import logging.handlers
from pathlib import Path

from app.config import get_settings

FORMAT = "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"
_configured = False


def configure_logging(component: str = "api") -> None:
    """Console + daily-rotated file logs (30 days kept) under LOG_DIR."""
    global _configured
    if _configured:
        return
    s = get_settings()
    root = logging.getLogger()
    root.setLevel(s.log_level.upper())
    fmt = logging.Formatter(FORMAT)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    for noisy in ("uvicorn.access", "httpx", "multipart", "alembic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True
    if not s.log_dir:
        return
    try:
        Path(s.log_dir).mkdir(parents=True, exist_ok=True)
        file = logging.handlers.TimedRotatingFileHandler(Path(s.log_dir) / f"{component}.log", when="midnight",
                                                         backupCount=30, encoding="utf-8")
        file.setFormatter(fmt)
        root.addHandler(file)
    except OSError:
        root.warning("could not open log directory %s; logging to console only", s.log_dir)
