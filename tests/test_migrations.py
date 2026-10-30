"""Alembic migrations on PostgreSQL, in a scratch database next to the test database."""

import uuid

import psycopg
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app import db as dbmod
from app.db import Base, make_engine
from tests.conftest import TEST_DATABASE_URL


@pytest.fixture
def scratch_engine():
    url = make_url(TEST_DATABASE_URL)
    name = f"crm_mig_{uuid.uuid4().hex[:8]}"
    admin = url.set(drivername="postgresql", database="postgres").render_as_string(hide_password=False)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    engine = make_engine(url.set(database=name))
    yield engine
    engine.dispose()
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def _run(engine, fn, revision):
    cfg = dbmod._alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        fn(cfg, revision)


def test_migrations_build_exactly_the_models(scratch_engine):
    _run(scratch_engine, command.upgrade, "head")
    with scratch_engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []


def test_full_round_trip(scratch_engine):
    _run(scratch_engine, command.upgrade, "head")
    _run(scratch_engine, command.downgrade, "base")
    _run(scratch_engine, command.upgrade, "head")
    with scratch_engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []


def test_multi_server_migration_keeps_existing_rows(scratch_engine):
    """Rows written before multi-server support (revision 0002) are moved onto server 1."""
    _run(scratch_engine, command.upgrade, "0002")
    with scratch_engine.begin() as conn:
        conn.execute(text("""insert into deals (ticket, login, time, time_msc, action, entry, symbol, volume, price,
            profit, commission, swap, fee, position_id, "order", reason, comment, contract_size, tick_value,
            tick_size, digits, synced_at) values (1000000, 500000, now(), 0, 0, 0, 'EURUSD', 1, 1.1, 5, 0, 0, 0,
            7000000, 0, 0, '', 100000, 1, 0.00001, 5, now())"""))
    _run(scratch_engine, command.upgrade, "head")
    with scratch_engine.connect() as conn:
        assert conn.execute(text("select server_id, ticket, profit from deals")).all() == [(1, 1000000, 5)]
        assert conn.execute(text("select id from mt5_servers")).scalars().all() == [1]
        # the id sequence continues after server 1, so the next server added in the admin gets id 2
        assert conn.execute(text("select nextval(pg_get_serial_sequence('mt5_servers', 'id'))")).scalar() == 2
