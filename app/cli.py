"""Admin CLI.

    python -m app.cli check-config          validate settings, database and MT5 connectivity
    python -m app.cli migrate               apply database migrations (run on every deploy)
    python -m app.cli create-admin --email admin@broker.com
    python -m app.cli seed-demo             sample account groups, IB plan and promo (dev only)
"""

import argparse
import getpass
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal, migrate
from app.models import AccountGroup, Campaign, CommissionPlan, MT5Server, RuleKind
from app.services import commissions, staff


def cmd_migrate(_):
    migrate()
    with SessionLocal() as db:
        staff.seed_roles(db)
    print("Database schema is up to date; default roles present.")


def cmd_check_config(_):
    from sqlalchemy import text

    from app.config import get_settings
    from app.db import engine, schema_is_current
    from sqlalchemy import select

    from app.models import MT5Server
    from app.mt5 import test_connection

    s = get_settings()  # raises on unsafe production settings
    print(f"environment: {s.app_env}  public url: {s.public_url}")
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    print(f"database:    ok ({engine.dialect.name})  schema current: {schema_is_current()}")
    with SessionLocal() as db:
        servers = list(db.scalars(select(MT5Server).where(MT5Server.enabled.is_(True)).order_by(MT5Server.id)))
    if not servers:  # a fresh install: the server is set up after the first sign-in
        print("mt5:         not set up yet - sign in and add it under MT5 > Configuration")
    failed = []
    for server in servers:
        ok, message = test_connection(server)
        where = server.address if server.kind.value == "manager" else "simulator"
        print(f"mt5:         {'ok    ' if ok else 'FAILED'} {server.name} ({where}){'' if ok else ' - ' + message}")
        if not ok:
            failed.append(server.name)
    print(f"email:       {'smtp ' + s.smtp_host if s.smtp_host else 'not configured (emails go to the log)'}")
    if failed:
        raise SystemExit(f"{len(failed)} MT5 server(s) failed: {', '.join(failed)}")


def cmd_create_admin(args):
    migrate()
    password = args.password or getpass.getpass("Password: ")
    with SessionLocal() as db:
        admin = staff.bootstrap_admin(db, args.email, password, args.name)
    print(f"Admin created: id={admin.id} email={admin.email}")


def cmd_seed_demo(_):
    migrate()
    with SessionLocal() as db:
        staff.seed_roles(db)
        server_id = db.scalar(select(MT5Server.id).order_by(MT5Server.id))
        if server_id is None:
            raise SystemExit("Set up the MT5 server first (MT5 > Configuration)")
        groups = [
            dict(name="Standard", mt5_group="real\\standard", spread_markup_points=10, leverage_options=[50, 100, 200, 500]),
            dict(name="ECN", mt5_group="real\\ecn", spread_markup_points=0, leverage_options=[50, 100, 200]),
            dict(name="Demo", mt5_group="demo\\standard", is_demo=True, leverage_options=[100, 500]),
        ]
        for t in groups:
            if not db.scalar(select(AccountGroup).where(AccountGroup.name == t["name"])):
                db.add(AccountGroup(server_id=server_id, **t))
        db.commit()
        if not db.scalar(select(CommissionPlan).where(CommissionPlan.name == "Default IB")):
            commissions.create_plan(db, None, "Default IB", [0.8, 0.15, 0.05], [
                {"symbol_pattern": "XAU*", "kind": RuleKind.per_lot, "value": Decimal("5"), "priority": 10},
                {"symbol_pattern": "*", "kind": RuleKind.spread_share, "value": Decimal("0.5"), "priority": 50},
            ], min_hold_seconds=60)
        if not db.scalar(select(Campaign).where(Campaign.code == "WELCOME50")):
            db.add(Campaign(name="Welcome bonus", code="WELCOME50", bonus_pct=Decimal("50"),
                            max_bonus=Decimal("500"), min_deposit=Decimal("100")))
        db.commit()
    print("Demo data seeded: account groups, 'Default IB' plan, WELCOME50 campaign.")


def main():
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(required=True)
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    sub.add_parser("init-db").set_defaults(fn=cmd_migrate)  # alias
    sub.add_parser("check-config").set_defaults(fn=cmd_check_config)
    a = sub.add_parser("create-admin")
    a.add_argument("--email", required=True)
    a.add_argument("--password")
    a.add_argument("--name", default="Administrator")
    a.set_defaults(fn=cmd_create_admin)
    sub.add_parser("seed-demo").set_defaults(fn=cmd_seed_demo)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
