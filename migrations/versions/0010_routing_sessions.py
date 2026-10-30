"""MT5 routing rules, symbol sessions; secret LP module parameters no longer kept

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-29
"""
import re

import sqlalchemy as sa
from alembic import op

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None

SECRET = re.compile(r"password|secret|token|secure key|api key", re.I)  # as app.services.liquidity.SECRET


def upgrade() -> None:
    op.add_column('symbols', sa.Column('sessions', sa.JSON(), nullable=False, server_default='{}'))
    op.create_table(
        'routing_rules',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('server_id', sa.Integer(), sa.ForeignKey('mt5_servers.id'), nullable=False),
        sa.Column('name', sa.String(64), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('types', sa.BigInteger(), nullable=False),
        sa.Column('requests', sa.BigInteger(), nullable=False),
        sa.Column('action', sa.Integer(), nullable=False),
        sa.Column('param', sa.String(255), nullable=False),
        sa.Column('param_type', sa.Integer(), nullable=False),
        sa.Column('conditions', sa.JSON(), nullable=False),
        sa.Column('dealers', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('synced_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('server_id', 'name', name='uq_routing_rules_server_name'),
    )
    op.create_index('ix_routing_rules_server_id', 'routing_rules', ['server_id'])

    # LP module parameters that are passwords or tokens stay on MT5 only
    conn = op.get_bind()
    table = sa.table('liquidity_connections', sa.column('id', sa.Integer()), sa.column('params', sa.JSON()))
    for row_id, params in conn.execute(sa.select(table.c.id, table.c.params)).all():
        if params and any(SECRET.search(k) and v for k, v in params.items()):
            cleaned = {k: "" if SECRET.search(k) else v for k, v in params.items()}
            conn.execute(table.update().where(table.c.id == row_id).values(params=cleaned))


def downgrade() -> None:
    op.drop_table('routing_rules')
    op.drop_column('symbols', 'sessions')
