"""The same login on two servers: an account is its server and login; automation rules can pick a server

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-07
"""
import sqlalchemy as sa
from alembic import op

revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None


def _unique_per_server(table: str, column: str) -> None:
    op.drop_index(f"ix_{table}_{column}", table_name=table)
    op.create_index(f"ix_{table}_{column}", table, [column])
    op.create_unique_constraint(f"uq_{table}_server_{column}", table, ["server_id", column])


def _master_server(table: str) -> None:
    """A master account's server. Logins were unique until now, so its account says which; a closed strategy or fund
    can outlive its account, and then only the first server is left to say."""
    op.add_column(table, sa.Column("server_id", sa.Integer(), sa.ForeignKey("mt5_servers.id"), nullable=True))
    op.execute(f"UPDATE {table} t SET server_id = a.server_id FROM trading_accounts a WHERE a.login = t.master_login")
    op.execute(f"UPDATE {table} SET server_id = (SELECT min(id) FROM mt5_servers) WHERE server_id IS NULL")
    op.alter_column(table, "server_id", nullable=False)
    _unique_per_server(table, "master_login")


def upgrade() -> None:
    _unique_per_server("trading_accounts", "login")

    op.execute("UPDATE sync_state s SET server_id = a.server_id FROM trading_accounts a WHERE a.login = s.login")
    op.execute("DELETE FROM sync_state WHERE server_id IS NULL")
    op.drop_constraint("sync_state_pkey", "sync_state", type_="primary")
    op.alter_column("sync_state", "server_id", nullable=False)
    op.create_primary_key("sync_state_pkey", "sync_state", ["server_id", "login"])

    _master_server("copy_strategies")
    _master_server("pamm_funds")

    op.add_column("copy_subscriptions", sa.Column("server_id", sa.Integer(), sa.ForeignKey("mt5_servers.id"), nullable=True))
    op.execute("UPDATE copy_subscriptions c SET server_id = s.server_id FROM copy_strategies s WHERE s.id = c.strategy_id")
    op.alter_column("copy_subscriptions", "server_id", nullable=False)

    op.add_column("payment_requests", sa.Column("server_id", sa.Integer(), sa.ForeignKey("mt5_servers.id"), nullable=True))
    op.execute("UPDATE payment_requests p SET server_id = a.server_id FROM trading_accounts a WHERE a.login = p.trading_login")

    op.add_column("automation_rules", sa.Column("server_id", sa.Integer(), sa.ForeignKey("mt5_servers.id", ondelete="CASCADE"),
                                                nullable=True))
    op.add_column("automation_runs", sa.Column("server_id", sa.Integer(), sa.ForeignKey("mt5_servers.id", ondelete="SET NULL"),
                                               nullable=True))
    op.execute("""UPDATE automation_runs r SET server_id = a.server_id, target = 'account:' || a.server_id || ':' || a.login
                  FROM trading_accounts a WHERE a.login = r.login""")


def downgrade() -> None:
    """Only while no login is on two servers."""
    op.execute("UPDATE automation_runs SET target = 'account:' || login WHERE login IS NOT NULL")
    op.drop_column("automation_runs", "server_id")
    op.drop_column("automation_rules", "server_id")
    op.drop_column("payment_requests", "server_id")
    op.drop_column("copy_subscriptions", "server_id")
    for table, column in (("pamm_funds", "master_login"), ("copy_strategies", "master_login"), ("trading_accounts", "login")):
        op.drop_constraint(f"uq_{table}_server_{column}", table, type_="unique")
        op.drop_index(f"ix_{table}_{column}", table_name=table)
        op.create_index(f"ix_{table}_{column}", table, [column], unique=True)
    op.drop_column("pamm_funds", "server_id")
    op.drop_column("copy_strategies", "server_id")
    op.drop_constraint("sync_state_pkey", "sync_state", type_="primary")
    op.alter_column("sync_state", "server_id", nullable=True)
    op.create_primary_key("sync_state_pkey", "sync_state", ["login"])
