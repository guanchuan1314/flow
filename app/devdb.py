"""Local PostgreSQL for development and tests (never used in production).

Uses the `pgserver` package, which ships real PostgreSQL binaries, so no separate
installation is needed on a developer machine. Production points DATABASE_URL at your
own PostgreSQL server instead.
"""

from __future__ import annotations

from pathlib import Path


def local_postgres(data_dir: str | Path, database: str, cleanup_mode: str | None = "stop") -> str:
    """Start (or reuse) a PostgreSQL cluster in `data_dir`, ensure `database` exists and return
    its SQLAlchemy URL. cleanup_mode: "stop" = stop the server when this process exits,
    "delete" = also delete the data, None = leave it running."""
    try:
        import pgserver
    except ImportError as exc:
        raise SystemExit("Local PostgreSQL needs the dev extras: uv pip install -e \".[dev]\" "
                         "(or set DATABASE_URL to your PostgreSQL server)") from exc
    import psycopg

    Path(data_dir).mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(Path(data_dir).resolve(), cleanup_mode=cleanup_mode)
    admin_uri = server.get_uri()  # postgresql://postgres:@127.0.0.1:PORT/postgres
    with psycopg.connect(admin_uri, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone():
            conn.execute(f'CREATE DATABASE "{database}"')
    base = admin_uri.rsplit("/", 1)[0]
    return base.replace("postgresql://", "postgresql+psycopg://", 1) + f"/{database}"
