"""import account types (MT5 groups) and trading accounts (MT5 users) from servers

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-27
"""
import sqlalchemy as sa
from alembic import op

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('mt5_servers', sa.Column('groups_synced_at', sa.DateTime(), nullable=True))
    op.add_column('mt5_servers', sa.Column('accounts_synced_at', sa.DateTime(), nullable=True))
    op.add_column('mt5_servers', sa.Column('last_sync_summary', sa.JSON(), nullable=False, server_default='{}'))
    op.alter_column('mt5_servers', 'last_sync_summary', server_default=None)

    for table in ('account_types', 'trading_accounts'):
        # existing rows were created in the CRM and exist on their server
        op.add_column(table, sa.Column('source', sa.String(8), nullable=False, server_default='crm'))
        op.add_column(table, sa.Column('on_server', sa.Boolean(), nullable=False, server_default=sa.true()))
        op.alter_column(table, 'source', server_default=None)
        op.alter_column(table, 'on_server', server_default=None)
    op.add_column('account_types', sa.Column('synced_at', sa.DateTime(), nullable=True))
    op.add_column('trading_accounts', sa.Column('last_seen_at', sa.DateTime(), nullable=True))
    op.create_unique_constraint('uq_account_types_server_group', 'account_types', ['server_id', 'mt5_group'])


def downgrade() -> None:
    op.drop_constraint('uq_account_types_server_group', 'account_types', type_='unique')
    op.drop_column('trading_accounts', 'last_seen_at')
    op.drop_column('account_types', 'synced_at')
    for table in ('trading_accounts', 'account_types'):
        op.drop_column(table, 'on_server')
        op.drop_column(table, 'source')
    op.drop_column('mt5_servers', 'last_sync_summary')
    op.drop_column('mt5_servers', 'accounts_synced_at')
    op.drop_column('mt5_servers', 'groups_synced_at')
