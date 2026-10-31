"""Custom payment / KYC methods: IP whitelist for provider callbacks ("*" = any address)

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-31
"""
import sqlalchemy as sa
from alembic import op

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None

TABLES = ('payment_methods', 'kyc_methods')


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column('callback_ips', sa.JSON(), nullable=False, server_default='["*"]'))


def downgrade() -> None:
    for table in TABLES:
        op.drop_column(table, 'callback_ips')
