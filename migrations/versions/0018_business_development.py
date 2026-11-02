"""Business development: staff referral codes and the Business Development role

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-02
"""
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None

ROLE = 'Business Development'
PERMISSIONS = ['clients.view', 'clients.referrals', 'accounts.view', 'trading.view']


def upgrade() -> None:
    with op.batch_alter_table('staff', schema=None) as batch_op:
        batch_op.add_column(sa.Column('referral_code', sa.String(length=16), nullable=True))
        batch_op.create_index(batch_op.f('ix_staff_referral_code'), ['referral_code'], unique=True)
    roles = sa.table('roles', sa.column('name', sa.String), sa.column('description', sa.String),
                     sa.column('permissions', sa.JSON), sa.column('is_system', sa.Boolean),
                     sa.column('created_at', sa.DateTime), sa.column('updated_at', sa.DateTime))
    conn = op.get_bind()
    if not conn.scalar(sa.select(sa.func.count()).select_from(roles).where(roles.c.name == ROLE)):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        conn.execute(roles.insert().values(name=ROLE, description=f'Default {ROLE} role', permissions=PERMISSIONS,
                                           is_system=True, created_at=now, updated_at=now))


def downgrade() -> None:
    with op.batch_alter_table('staff', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_staff_referral_code'))
        batch_op.drop_column('referral_code')
