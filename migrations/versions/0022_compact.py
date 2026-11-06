"""Compact: the summary deals, and exact copies of what they replaced

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-06
"""
import sqlalchemy as sa
from alembic import op

revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'compact_summaries',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('server_id', sa.Integer(), nullable=False),
        sa.Column('login', sa.BigInteger(), nullable=False),
        sa.Column('key', sa.String(length=128), nullable=False),
        sa.Column('action', sa.Integer(), nullable=False),
        sa.Column('label', sa.String(length=64), nullable=False),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('ticket', sa.BigInteger(), nullable=False),
        sa.Column('date_from', sa.DateTime(), nullable=False),
        sa.Column('date_to', sa.DateTime(), nullable=False),
        sa.Column('amount', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('deals', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['server_id'], ['mt5_servers.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('server_id', 'login', 'key', name='uq_compact_summary'),
    )
    op.create_index('ix_compact_summaries_login', 'compact_summaries', ['login'])
    op.create_table(
        'compacted_records',
        sa.Column('server_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=8), nullable=False),
        sa.Column('ticket', sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column('summary_id', sa.Integer(), nullable=True),
        sa.Column('login', sa.BigInteger(), nullable=False),
        sa.Column('time', sa.DateTime(), nullable=True),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('action', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('comment', sa.String(length=64), nullable=False),
        sa.Column('data', sa.JSON(), nullable=False),
        sa.Column('compacted_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['server_id'], ['mt5_servers.id']),
        sa.ForeignKeyConstraint(['summary_id'], ['compact_summaries.id']),
        sa.PrimaryKeyConstraint('server_id', 'kind', 'ticket'),
    )
    op.create_index('ix_compacted_records_login', 'compacted_records', ['login'])
    op.create_index('ix_compacted_records_summary_id', 'compacted_records', ['summary_id'])


def downgrade() -> None:
    op.drop_index('ix_compacted_records_summary_id', table_name='compacted_records')
    op.drop_index('ix_compacted_records_login', table_name='compacted_records')
    op.drop_table('compacted_records')
    op.drop_index('ix_compact_summaries_login', table_name='compact_summaries')
    op.drop_table('compact_summaries')
