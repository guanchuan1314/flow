"""Synchronization history: every sync from MT5, manual or scheduled

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-02
"""
import sqlalchemy as sa
from alembic import op

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'sync_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('server_id', sa.Integer(), nullable=True),
        sa.Column('trigger', sa.String(length=16), nullable=False),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('summary', sa.JSON(), nullable=True),
        sa.Column('message', sa.String(length=1024), nullable=False),
        sa.ForeignKeyConstraint(['created_by_id'], ['staff.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['server_id'], ['mt5_servers.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('sync_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sync_runs_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_sync_runs_kind'), ['kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_sync_runs_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_sync_runs_trigger'), ['trigger'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('sync_runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sync_runs_trigger'))
        batch_op.drop_index(batch_op.f('ix_sync_runs_status'))
        batch_op.drop_index(batch_op.f('ix_sync_runs_kind'))
        batch_op.drop_index(batch_op.f('ix_sync_runs_created_at'))
    op.drop_table('sync_runs')
