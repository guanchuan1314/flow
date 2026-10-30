from alembic import context

import app.models  # noqa: F401  (registers all tables on Base.metadata)
from app.config import get_settings
from app.db import Base, make_engine

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=get_settings().database_url, target_metadata=target_metadata, literal_binds=True,
                      compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = context.config.attributes.get("connection")
    if connection is not None:  # programmatic use (app.db.migrate) passes its own connection
        _run(connection)
        return
    engine = make_engine(get_settings().database_url)
    with engine.begin() as conn:
        _run(conn)
    engine.dispose()


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
