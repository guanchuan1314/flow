"""Symbols & pricing: auto symbol sync and spread, commission and swap markups

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-29
"""
import sqlalchemy as sa
from alembic import op

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('mt5_servers', sa.Column('pricing', sa.JSON(), nullable=False, server_default='{}'))
    op.add_column('symbols', sa.Column('swap_markup', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('symbols', 'swap_markup')
    op.drop_column('mt5_servers', 'pricing')
