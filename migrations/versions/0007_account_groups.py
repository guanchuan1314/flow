"""rename account types to account groups (table, columns, constraints, permission)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-28
"""
import json

import sqlalchemy as sa
from alembic import op

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def _rename(old: str, new: str) -> None:
    """account_type(s) -> account_group(s) everywhere in the schema, or back."""
    conn = op.get_bind()
    op.rename_table(f'{old}s', f'{new}s')
    for table in ('trading_accounts', 'commission_rules'):
        op.alter_column(table, f'{old}_id', new_column_name=f'{new}_id')
    for table, name in conn.execute(sa.text(
            "SELECT conrelid::regclass::text, conname FROM pg_constraint WHERE conname LIKE :p"), {"p": f"%{old}%"}).all():
        op.execute(f'ALTER TABLE {table} RENAME CONSTRAINT "{name}" TO "{name.replace(old, new)}"')
    for (name,) in conn.execute(sa.text(
            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() AND indexname LIKE :p"),
            {"p": f"%{old}%"}).all():
        op.execute(f'ALTER INDEX "{name}" RENAME TO "{name.replace(old, new)}"')
    op.execute(f'ALTER SEQUENCE IF EXISTS {old}s_id_seq RENAME TO {new}s_id_seq')

    old_perm, new_perm = f'{old}s.manage', f'{new}s.manage'
    for role_id, perms in conn.execute(sa.text("SELECT id, permissions FROM roles")).all():
        perms = json.loads(perms) if isinstance(perms, str) else (perms or [])
        if old_perm in perms:
            conn.execute(sa.text("UPDATE roles SET permissions = :p WHERE id = :i"),
                         {"p": json.dumps([new_perm if p == old_perm else p for p in perms]), "i": role_id})
    for server_id, summary in conn.execute(sa.text("SELECT id, last_sync_summary FROM mt5_servers")).all():
        summary = json.loads(summary) if isinstance(summary, str) else (summary or {})
        if f'{old}s' in summary:
            summary[f'{new}s'] = summary.pop(f'{old}s')
            conn.execute(sa.text("UPDATE mt5_servers SET last_sync_summary = :s WHERE id = :i"),
                         {"s": json.dumps(summary), "i": server_id})


def upgrade() -> None:
    _rename('account_type', 'account_group')


def downgrade() -> None:
    _rename('account_group', 'account_type')
