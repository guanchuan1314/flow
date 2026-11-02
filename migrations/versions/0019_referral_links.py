"""Business development: tagged referral links

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-02
"""
import sqlalchemy as sa
from alembic import op

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'referral_links',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('staff_id', sa.Integer(), nullable=False),
        sa.Column('tag', sa.String(length=64), nullable=False),
        sa.Column('code', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['staff_id'], ['staff.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('staff_id', 'tag'),
    )
    with op.batch_alter_table('referral_links', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_referral_links_code'), ['code'], unique=True)
        batch_op.create_index(batch_op.f('ix_referral_links_staff_id'), ['staff_id'], unique=False)
    with op.batch_alter_table('clients', schema=None) as batch_op:
        batch_op.add_column(sa.Column('referral_link_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_clients_referral_link_id'), ['referral_link_id'], unique=False)
        batch_op.create_foreign_key('fk_clients_referral_link_id', 'referral_links', ['referral_link_id'], ['id'],
                                    ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('clients', schema=None) as batch_op:
        batch_op.drop_constraint('fk_clients_referral_link_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_clients_referral_link_id'))
        batch_op.drop_column('referral_link_id')
    with op.batch_alter_table('referral_links', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_referral_links_staff_id'))
        batch_op.drop_index(batch_op.f('ix_referral_links_code'))
    op.drop_table('referral_links')
