"""Security columns (token version, TOTP replay guard, encrypted TOTP secrets) and query indexes

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-30
"""
import sqlalchemy as sa
from alembic import op

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None

PRINCIPALS = ('staff', 'clients')


def _recode_totp(convert, only_plain: bool) -> None:
    """Rewrite every stored TOTP secret with `convert` (encrypt on upgrade, decrypt on downgrade)."""
    conn = op.get_bind()
    for table in PRINCIPALS:
        rows = conn.execute(sa.text(f"select id, totp_secret from {table} where totp_secret is not null "
                                    "and totp_secret <> ''")).all()
        for pid, secret in rows:
            encrypted = secret.startswith('gAAAA')  # Fernet tokens start like this; base32 secrets never do
            if encrypted == only_plain:
                continue
            conn.execute(sa.text(f"update {table} set totp_secret = :s where id = :i"),
                         {"s": convert(secret), "i": pid})


def upgrade() -> None:
    from app.core.security import encrypt_secret

    for table in PRINCIPALS:
        op.add_column(table, sa.Column('token_version', sa.Integer(), nullable=False, server_default='0'))
        op.add_column(table, sa.Column('totp_last_step', sa.BigInteger(), nullable=True))
        op.alter_column(table, 'totp_secret', type_=sa.String(255), existing_type=sa.String(64),
                        existing_nullable=True)
    _recode_totp(encrypt_secret, only_plain=True)

    op.create_index('ix_deals_login_time', 'deals', ['login', sa.text('time DESC')])
    op.create_index('ix_positions_open_login', 'positions', ['server_id', 'login'],
                    postgresql_where=sa.text('is_open'))
    op.create_index('ix_orders_open_login', 'orders', ['server_id', 'login'], postgresql_where=sa.text('is_open'))
    op.create_index('ix_audit_logs_entity', 'audit_logs', ['entity', 'entity_id'])
    op.create_index('ix_audit_logs_actor', 'audit_logs', ['actor_type', 'actor_id'])
    op.create_index('ix_trading_accounts_server_state', 'trading_accounts', ['server_id', 'on_server', 'status'])
    op.create_index('ix_clients_email_lower', 'clients', [sa.text('lower(email)')])
    op.create_index('ix_sync_state_last_deal_time', 'sync_state', ['last_deal_time'])


def downgrade() -> None:
    from app.core.security import decrypt_secret

    op.drop_index('ix_sync_state_last_deal_time', 'sync_state')
    op.drop_index('ix_clients_email_lower', 'clients')
    op.drop_index('ix_trading_accounts_server_state', 'trading_accounts')
    op.drop_index('ix_audit_logs_actor', 'audit_logs')
    op.drop_index('ix_audit_logs_entity', 'audit_logs')
    op.drop_index('ix_orders_open_login', 'orders')
    op.drop_index('ix_positions_open_login', 'positions')
    op.drop_index('ix_deals_login_time', 'deals')

    _recode_totp(decrypt_secret, only_plain=False)
    for table in PRINCIPALS:
        op.alter_column(table, 'totp_secret', type_=sa.String(64), existing_type=sa.String(255),
                        existing_nullable=True)
        op.drop_column(table, 'totp_last_step')
        op.drop_column(table, 'token_version')
