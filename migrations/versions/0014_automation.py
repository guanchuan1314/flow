"""Automation rules and their run log (CRM side, nothing on MT5)

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-30
"""
import sqlalchemy as sa
from alembic import op

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'automation_rules',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(64), nullable=False, unique=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('trigger', sa.String(32), nullable=False),
        sa.Column('params', sa.JSON(), nullable=False),
        sa.Column('filters', sa.JSON(), nullable=False),
        sa.Column('action', sa.String(32), nullable=False),
        sa.Column('action_params', sa.JSON(), nullable=False),
        sa.Column('cooldown_hours', sa.Integer(), nullable=False),
        sa.Column('last_run_at', sa.DateTime(), nullable=True),
        sa.Column('last_result', sa.String(255), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_table(
        'automation_runs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('rule_id', sa.Integer(), sa.ForeignKey('automation_rules.id', ondelete='CASCADE'), nullable=False),
        sa.Column('target', sa.String(32), nullable=False),
        sa.Column('login', sa.BigInteger(), nullable=True),
        sa.Column('client_id', sa.Integer(), nullable=True),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('outcome', sa.String(8), nullable=False),
        sa.Column('message', sa.String(255), nullable=False),
    )
    op.create_index('ix_automation_runs_rule_id', 'automation_runs', ['rule_id'])
    op.create_index('ix_automation_runs_at', 'automation_runs', ['at'])
    op.create_index('ix_automation_runs_rule_target', 'automation_runs', ['rule_id', 'target', 'at'])


def downgrade() -> None:
    op.drop_table('automation_runs')
    op.drop_table('automation_rules')
