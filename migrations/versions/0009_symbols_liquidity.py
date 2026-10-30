"""MT5 symbols and liquidity connections (gateways, data feeds)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-29
"""
import sqlalchemy as sa
from alembic import op

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def _stamps() -> list[sa.Column]:
    return [sa.Column('created_at', sa.DateTime(), nullable=False), sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('synced_at', sa.DateTime(), nullable=True)]


def upgrade() -> None:
    op.create_table(
        'symbols',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('server_id', sa.Integer(), sa.ForeignKey('mt5_servers.id'), nullable=False),
        sa.Column('name', sa.String(64), nullable=False),
        sa.Column('path', sa.String(255), nullable=False),
        sa.Column('description', sa.String(255), nullable=False),
        sa.Column('source', sa.String(64), nullable=False),
        sa.Column('currency_base', sa.String(16), nullable=False),
        sa.Column('currency_profit', sa.String(16), nullable=False),
        sa.Column('currency_margin', sa.String(16), nullable=False),
        sa.Column('digits', sa.Integer(), nullable=False),
        sa.Column('contract_size', sa.Numeric(20, 4), nullable=False),
        sa.Column('calc_mode', sa.Integer(), nullable=False),
        sa.Column('trade_mode', sa.Integer(), nullable=False),
        sa.Column('exec_mode', sa.Integer(), nullable=False),
        sa.Column('volume_min', sa.Numeric(18, 4), nullable=False),
        sa.Column('volume_max', sa.Numeric(18, 4), nullable=False),
        sa.Column('volume_step', sa.Numeric(18, 4), nullable=False),
        sa.Column('spread', sa.Integer(), nullable=False),
        sa.Column('stops_level', sa.Integer(), nullable=False),
        sa.Column('freeze_level', sa.Integer(), nullable=False),
        sa.Column('swap_mode', sa.Integer(), nullable=False),
        sa.Column('swap_long', sa.Numeric(18, 6), nullable=False),
        sa.Column('swap_short', sa.Numeric(18, 6), nullable=False),
        sa.Column('swap_3day', sa.Integer(), nullable=False),
        sa.Column('margin_initial', sa.Numeric(20, 4), nullable=False),
        sa.Column('margin_maintenance', sa.Numeric(20, 4), nullable=False),
        sa.Column('margin_hedged', sa.Numeric(20, 4), nullable=False),
        *_stamps(),
        sa.UniqueConstraint('server_id', 'name', name='uq_symbols_server_name'),
    )
    op.create_index('ix_symbols_server_id', 'symbols', ['server_id'])

    op.create_table(
        'liquidity_connections',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('server_id', sa.Integer(), sa.ForeignKey('mt5_servers.id'), nullable=False),
        sa.Column('kind', sa.String(8), nullable=False),
        sa.Column('name', sa.String(64), nullable=False),
        sa.Column('module', sa.String(128), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('lp_server', sa.String(255), nullable=False),
        sa.Column('lp_login', sa.String(128), nullable=False),
        sa.Column('gateway_server', sa.String(255), nullable=False),
        sa.Column('gateway_login', sa.BigInteger(), nullable=False),
        sa.Column('mt5_id', sa.Integer(), nullable=False),
        sa.Column('gateway_type', sa.String(64), nullable=False),
        sa.Column('flags', sa.BigInteger(), nullable=False),
        sa.Column('keywords', sa.String(255), nullable=False),
        sa.Column('categories', sa.String(255), nullable=False),
        sa.Column('timeout_reconnect', sa.Integer(), nullable=False),
        sa.Column('timeout_sleep', sa.Integer(), nullable=False),
        sa.Column('timeout_attempts', sa.Integer(), nullable=False),
        sa.Column('symbols', sa.JSON(), nullable=False),
        sa.Column('groups', sa.JSON(), nullable=False),
        sa.Column('params', sa.JSON(), nullable=False),
        sa.Column('translations', sa.JSON(), nullable=False),
        sa.Column('state', sa.JSON(), nullable=False),
        *_stamps(),
        sa.UniqueConstraint('server_id', 'kind', 'name', name='uq_liquidity_connections_server_kind_name'),
    )
    op.create_index('ix_liquidity_connections_server_id', 'liquidity_connections', ['server_id'])


def downgrade() -> None:
    op.drop_table('liquidity_connections')
    op.drop_table('symbols')
