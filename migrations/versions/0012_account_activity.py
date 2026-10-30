"""Account activity (last terminal connection, last trade) on trading accounts, for the inactive accounts list

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-29
"""
import sqlalchemy as sa
from alembic import op

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in ('last_access_at', 'last_trade_at', 'last_activity_at'):
        op.add_column('trading_accounts', sa.Column(column, sa.DateTime(), nullable=True))
    op.create_index('ix_trading_accounts_server_activity', 'trading_accounts', ['server_id', 'last_activity_at'])
    # the last trade of every account from the deals already stored (buy / sell deals only)
    op.execute("""
        UPDATE trading_accounts a SET last_trade_at = d.t, last_activity_at = d.t
        FROM (SELECT login, max(time) AS t FROM deals WHERE action IN (0, 1) GROUP BY login) d
        WHERE a.login = d.login
    """)


def downgrade() -> None:
    op.drop_index('ix_trading_accounts_server_activity', 'trading_accounts')
    for column in ('last_activity_at', 'last_trade_at', 'last_access_at'):
        op.drop_column('trading_accounts', column)
