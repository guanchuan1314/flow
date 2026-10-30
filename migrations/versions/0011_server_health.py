"""Server health samples, worker poll stats, demo clean-up and trader style settings per server

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-29
"""
import sqlalchemy as sa
from alembic import op

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in ('poll_stats', 'demo_cleanup', 'style_groups'):
        op.add_column('mt5_servers', sa.Column(column, sa.JSON(), nullable=False, server_default='{}'))
    op.create_table(
        'server_health_samples',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('server_id', sa.Integer(), sa.ForeignKey('mt5_servers.id'), nullable=False),
        sa.Column('taken_at', sa.DateTime(), nullable=False),
        sa.Column('components', sa.JSON(), nullable=False),
        sa.Column('poll', sa.JSON(), nullable=False),
        sa.Column('error', sa.String(512), nullable=True),
    )
    op.create_index('ix_server_health_samples_server_id', 'server_health_samples', ['server_id'])
    op.create_index('ix_server_health_samples_taken_at', 'server_health_samples', ['taken_at'])


def downgrade() -> None:
    op.drop_table('server_health_samples')
    for column in ('style_groups', 'demo_cleanup', 'poll_stats'):
        op.drop_column('mt5_servers', column)
