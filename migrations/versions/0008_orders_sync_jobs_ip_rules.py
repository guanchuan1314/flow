"""MT5 orders, background sync jobs, IP blacklist / whitelist

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-28
"""
import sqlalchemy as sa
from alembic import op

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None

LOTS, PRICE = sa.Numeric(18, 4), sa.Numeric(20, 8)


def upgrade() -> None:
    op.create_table(
        'orders',
        sa.Column('server_id', sa.Integer(), sa.ForeignKey('mt5_servers.id'), primary_key=True),
        sa.Column('ticket', sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column('login', sa.BigInteger(), nullable=False),
        sa.Column('symbol', sa.String(32), nullable=False),
        sa.Column('type', sa.Integer(), nullable=False),
        sa.Column('state', sa.Integer(), nullable=False),
        sa.Column('volume_initial', LOTS, nullable=False),
        sa.Column('volume_current', LOTS, nullable=False),
        sa.Column('price_order', PRICE, nullable=False),
        sa.Column('price_current', PRICE, nullable=False),
        sa.Column('sl', PRICE, nullable=False),
        sa.Column('tp', PRICE, nullable=False),
        sa.Column('time_setup', sa.DateTime(), nullable=True),
        sa.Column('time_done', sa.DateTime(), nullable=True),
        sa.Column('position_id', sa.BigInteger(), nullable=False),
        sa.Column('reason', sa.Integer(), nullable=False),
        sa.Column('comment', sa.String(64), nullable=False),
        sa.Column('is_open', sa.Boolean(), nullable=False),
        sa.Column('synced_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_orders_login', 'orders', ['login'])
    op.create_index('ix_orders_time_setup', 'orders', ['time_setup'])
    op.create_index('ix_orders_is_open', 'orders', ['is_open'])
    op.add_column('sync_state', sa.Column('last_order_sync_at', sa.DateTime(), nullable=True))

    op.create_table(
        'sync_jobs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('created_by_id', sa.Integer(), sa.ForeignKey('staff.id'), nullable=True),
        sa.Column('server_id', sa.Integer(), sa.ForeignKey('mt5_servers.id'), nullable=True),
        sa.Column('logins', sa.JSON(), nullable=False),
        sa.Column('date_from', sa.DateTime(), nullable=False),
        sa.Column('date_to', sa.DateTime(), nullable=False),
        sa.Column('include_orders', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(12), nullable=False),
        sa.Column('total', sa.Integer(), nullable=False),
        sa.Column('done', sa.Integer(), nullable=False),
        sa.Column('deals_seen', sa.Integer(), nullable=False),
        sa.Column('deals_added', sa.Integer(), nullable=False),
        sa.Column('orders_seen', sa.Integer(), nullable=False),
        sa.Column('orders_added', sa.Integer(), nullable=False),
        sa.Column('errors', sa.Integer(), nullable=False),
        sa.Column('message', sa.String(512), nullable=False),
        sa.Column('cancel_requested', sa.Boolean(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('heartbeat_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_sync_jobs_created_at', 'sync_jobs', ['created_at'])
    op.create_index('ix_sync_jobs_status', 'sync_jobs', ['status'])

    op.create_table(
        'ip_rules',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('network', sa.String(64), nullable=False),
        sa.Column('action', sa.String(8), nullable=False),
        sa.Column('scope', sa.String(8), nullable=False),
        sa.Column('description', sa.String(255), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('apply_mt5', sa.Boolean(), nullable=False),
        sa.Column('mt5_server_ids', sa.JSON(), nullable=False),
        sa.Column('mt5_state', sa.JSON(), nullable=False),
        sa.Column('created_by_id', sa.Integer(), sa.ForeignKey('staff.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('ip_rules')
    op.drop_table('sync_jobs')
    op.drop_column('sync_state', 'last_order_sync_at')
    op.drop_table('orders')
