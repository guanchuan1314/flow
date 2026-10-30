"""account types carry their MT5 group's margin call and stop-out levels

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-28
"""
import sqlalchemy as sa
from alembic import op

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # filled in from MT5 by the next group import
    op.add_column('account_types', sa.Column('margin_call', sa.Numeric(12, 2), nullable=True))
    op.add_column('account_types', sa.Column('stop_out', sa.Numeric(12, 2), nullable=True))


def downgrade() -> None:
    op.drop_column('account_types', 'stop_out')
    op.drop_column('account_types', 'margin_call')
