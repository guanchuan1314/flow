"""Synchronization progress: work done / total, and the run's parameters

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-02
"""
import sqlalchemy as sa
from alembic import op

revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('sync_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('params', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('total', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('done', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('sync_runs', schema=None) as batch_op:
        batch_op.drop_column('done')
        batch_op.drop_column('total')
        batch_op.drop_column('params')
