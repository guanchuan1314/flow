"""payment methods and KYC methods (manual or custom provider), client -> MT5 sync status

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-28
"""
import sqlalchemy as sa
from alembic import op

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def _provider_columns() -> list:
    return [
        sa.Column('name', sa.String(64), nullable=False, unique=True),
        sa.Column('kind', sa.String(8), nullable=False),
        sa.Column('description', sa.String(255), nullable=False),
        sa.Column('instructions', sa.Text(), nullable=False),
        sa.Column('api_url', sa.String(512), nullable=False),
        sa.Column('redirect_template', sa.String(1024), nullable=False),
        sa.Column('url_field', sa.String(64), nullable=False),
        sa.Column('api_key_enc', sa.String(512), nullable=False),
        sa.Column('secret_enc', sa.String(512), nullable=False),
        sa.Column('signature_header', sa.String(64), nullable=False),
        sa.Column('callback_token', sa.String(64), nullable=False, unique=True),
        sa.Column('ref_field', sa.String(64), nullable=False),
        sa.Column('status_field', sa.String(64), nullable=False),
        sa.Column('success_values', sa.String(255), nullable=False),
        sa.Column('failure_values', sa.String(255), nullable=False),
        sa.Column('reason_field', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        'payment_methods',
        sa.Column('id', sa.Integer(), primary_key=True),
        *_provider_columns(),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('allow_deposit', sa.Boolean(), nullable=False),
        sa.Column('allow_withdrawal', sa.Boolean(), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False),
        sa.Column('min_amount', sa.Numeric(18, 2), nullable=True),
        sa.Column('max_amount', sa.Numeric(18, 2), nullable=True),
        sa.Column('requires_proof', sa.Boolean(), nullable=False),
        sa.Column('auto_credit', sa.Boolean(), nullable=False),
        sa.Column('amount_field', sa.String(64), nullable=False),
        sa.Column('icon', sa.String(32), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
    )
    op.create_table(
        'kyc_methods',
        sa.Column('id', sa.Integer(), primary_key=True),
        *_provider_columns(),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('required_documents', sa.JSON(), nullable=False),
    )
    op.create_table(
        'kyc_sessions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('client_id', sa.Integer(), sa.ForeignKey('clients.id', ondelete='CASCADE'), nullable=False),
        sa.Column('method_id', sa.Integer(), sa.ForeignKey('kyc_methods.id'), nullable=False),
        sa.Column('reference', sa.String(64), nullable=False, unique=True),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('url', sa.String(1024), nullable=False),
        sa.Column('reason', sa.String(255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_kyc_sessions_client_id', 'kyc_sessions', ['client_id'])
    op.create_table(
        'provider_callbacks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('scope', sa.String(8), nullable=False),
        sa.Column('method_id', sa.Integer(), nullable=True),
        sa.Column('reference', sa.String(64), nullable=True),
        sa.Column('ip', sa.String(64), nullable=True),
        sa.Column('signature_ok', sa.Boolean(), nullable=False),
        sa.Column('outcome', sa.String(255), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('data', sa.JSON(), nullable=False),
    )
    op.create_index('ix_provider_callbacks_created_at', 'provider_callbacks', ['created_at'])
    op.create_index('ix_provider_callbacks_method_id', 'provider_callbacks', ['method_id'])

    op.alter_column('payment_requests', 'method', type_=sa.String(64), existing_type=sa.String(32),
                    existing_nullable=False)
    op.add_column('payment_requests', sa.Column('method_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_payment_requests_method_id', 'payment_requests', 'payment_methods', ['method_id'], ['id'])
    op.create_index('ix_payment_requests_method_id', 'payment_requests', ['method_id'])
    op.add_column('payment_requests', sa.Column('proof_path', sa.String(512), nullable=True))
    op.add_column('payment_requests', sa.Column('proof_name', sa.String(255), nullable=True))
    op.add_column('payment_requests', sa.Column('gateway_ref', sa.String(64), nullable=True))
    op.create_unique_constraint('uq_payment_requests_gateway_ref', 'payment_requests', ['gateway_ref'])
    op.add_column('payment_requests', sa.Column('gateway_url', sa.String(1024), nullable=True))
    op.add_column('payment_requests', sa.Column('gateway_status', sa.String(16), nullable=True))
    op.add_column('payment_requests', sa.Column('paid_at', sa.DateTime(), nullable=True))

    op.add_column('clients', sa.Column('mt5_synced_at', sa.DateTime(), nullable=True))
    op.add_column('clients', sa.Column('mt5_sync_error', sa.String(255), nullable=True))
    op.add_column('account_types', sa.Column('open_on_register', sa.Boolean(), nullable=False,
                                             server_default=sa.false()))
    op.alter_column('account_types', 'open_on_register', server_default=None)


def downgrade() -> None:
    op.drop_column('account_types', 'open_on_register')
    op.drop_column('clients', 'mt5_sync_error')
    op.drop_column('clients', 'mt5_synced_at')
    for col in ('paid_at', 'gateway_status', 'gateway_url'):
        op.drop_column('payment_requests', col)
    op.drop_constraint('uq_payment_requests_gateway_ref', 'payment_requests', type_='unique')
    for col in ('gateway_ref', 'proof_name', 'proof_path'):
        op.drop_column('payment_requests', col)
    op.drop_index('ix_payment_requests_method_id', 'payment_requests')
    op.drop_constraint('fk_payment_requests_method_id', 'payment_requests', type_='foreignkey')
    op.drop_column('payment_requests', 'method_id')
    op.alter_column('payment_requests', 'method', type_=sa.String(32), existing_type=sa.String(64),
                    existing_nullable=False)
    op.drop_table('provider_callbacks')
    op.drop_table('kyc_sessions')
    op.drop_table('kyc_methods')
    op.drop_table('payment_methods')
