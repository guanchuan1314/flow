from collections.abc import Iterator
from datetime import datetime, timezone

from sqlalchemy import DateTime, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


def make_engine(url: str):
    """PostgreSQL engine. pre_ping replaces connections dropped by the server or a firewall."""
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20, pool_recycle=1800)


engine = make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def configure(url: str) -> None:
    """Rebind the global engine/session factory (used by tests and CLI)."""
    global engine
    engine = make_engine(url)
    SessionLocal.configure(bind=engine)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create tables directly from the models (tests only); real databases use `migrate()`."""
    import app.models  # noqa: F401  (register mappers)

    Base.metadata.create_all(engine)


def _alembic_config():
    from pathlib import Path

    from alembic.config import Config

    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    return cfg


MIGRATION_LOCK = 0x4D54_3543_524D  # pg_advisory lock key ("MT5CRM"), shared by every process


def migrate() -> None:
    """Upgrade the database schema to the latest Alembic revision. A PostgreSQL advisory lock (held until
    the migration commits) makes the API and the worker take turns instead of migrating at the same time."""
    from alembic import command
    from sqlalchemy import text

    from app import custom

    cfg = _alembic_config()
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK})
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    engine.dispose()  # pooled connections may have cached plans for tables the migration changed
    custom.migrate()


def schema_is_current() -> bool:
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    heads = set(ScriptDirectory.from_config(_alembic_config()).get_heads())
    with engine.connect() as conn:
        return set(MigrationContext.configure(conn).get_current_heads()) == heads
