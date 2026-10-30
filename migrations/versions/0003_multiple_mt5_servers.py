"""multiple MT5 servers managed in the admin

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-27

* new table mt5_servers; the server from the MT5_* environment settings becomes server 1
* account_types / trading_accounts / sync_state / ib_commissions get server_id (existing rows -> server 1)
* deals and positions are keyed by (server_id, ticket) because tickets repeat across servers.
  They are rebuilt (new table, copy, swap) so the same steps work on SQLite and PostgreSQL.
"""
import sqlalchemy as sa
from alembic import op

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

server_kind = sa.Enum('manager', 'simulator', name='serverkind')

DEAL_COLUMNS = [
    ('ticket', sa.BigInteger(), False), ('login', sa.BigInteger(), False), ('time', sa.DateTime(), False),
    ('time_msc', sa.BigInteger(), False), ('action', sa.Integer(), False), ('entry', sa.Integer(), False),
    ('symbol', sa.String(32), False), ('volume', sa.Numeric(18, 4), False), ('price', sa.Numeric(20, 8), False),
    ('profit', sa.Numeric(18, 2), False), ('commission', sa.Numeric(18, 2), False), ('swap', sa.Numeric(18, 2), False),
    ('fee', sa.Numeric(18, 2), False), ('position_id', sa.BigInteger(), False), ('order', sa.BigInteger(), False),
    ('reason', sa.Integer(), False), ('comment', sa.String(64), False), ('contract_size', sa.Numeric(20, 4), False),
    ('tick_value', sa.Numeric(20, 8), False), ('tick_size', sa.Numeric(20, 8), False), ('digits', sa.Integer(), False),
    ('synced_at', sa.DateTime(), False),
]
POSITION_COLUMNS = [
    ('position_id', sa.BigInteger(), False), ('login', sa.BigInteger(), False), ('symbol', sa.String(32), False),
    ('action', sa.Integer(), False), ('volume', sa.Numeric(18, 4), False), ('price_open', sa.Numeric(20, 8), False),
    ('price_current', sa.Numeric(20, 8), False), ('sl', sa.Numeric(20, 8), False), ('tp', sa.Numeric(20, 8), False),
    ('profit', sa.Numeric(18, 2), False), ('swap', sa.Numeric(18, 2), False), ('time_create', sa.DateTime(), False),
    ('time_update', sa.DateTime(), True), ('comment', sa.String(64), False), ('is_open', sa.Boolean(), False),
    ('closed_at', sa.DateTime(), True), ('synced_at', sa.DateTime(), False),
]
INDEXES = {'deals': ['login', 'position_id', 'time'], 'positions': ['is_open', 'login']}


def _cols(spec):
    return [sa.Column(name, type_, nullable=nullable, autoincrement=False) for name, type_, nullable in spec]


def _rebuild(table: str, spec, key: str, with_server: bool) -> None:
    """Recreate `table` with or without server_id in its primary key, keeping the rows."""
    bind = op.get_bind()
    tmp = f'{table}_new'
    columns = _cols(spec)
    if with_server:
        columns.insert(0, sa.Column('server_id', sa.Integer(), sa.ForeignKey('mt5_servers.id'), nullable=False,
                                    autoincrement=False))
        pk = sa.PrimaryKeyConstraint('server_id', key, name=f'pk_{table}')
    else:
        # PostgreSQL constraint names are schema-wide and the old table still exists at this
        # point, so the downgraded table uses a different primary-key name.
        pk = sa.PrimaryKeyConstraint(key, name=f'pk_{table}_single')
    op.create_table(tmp, *columns, pk)
    meta = sa.MetaData()
    old = sa.Table(table, meta, autoload_with=bind)
    new = sa.Table(tmp, meta, autoload_with=bind)
    names = [name for name, _, _ in spec]
    if with_server:
        select = sa.select(sa.literal(1).label('server_id'), *[old.c[n] for n in names])
        op.execute(sa.insert(new).from_select(['server_id', *names], select))
    else:  # downgrade: only the first server's rows fit the old shape
        select = sa.select(*[old.c[n] for n in names]).where(old.c.server_id == 1)
        op.execute(sa.insert(new).from_select(names, select))
    op.drop_table(table)
    op.rename_table(tmp, table)
    for column in INDEXES[table]:
        op.create_index(f'ix_{table}_{column}', table, [column])


def upgrade() -> None:
    op.create_table(
        'mt5_servers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(64), nullable=False),
        sa.Column('kind', server_kind, nullable=False),
        sa.Column('is_demo', sa.Boolean(), nullable=False),
        sa.Column('address', sa.String(255), nullable=False),
        sa.Column('manager_login', sa.BigInteger(), nullable=False),
        sa.Column('manager_password_enc', sa.String(512), nullable=False),
        sa.Column('terminal_name', sa.String(64), nullable=False),
        sa.Column('timeout_ms', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('is_price_feed', sa.Boolean(), nullable=False),
        sa.Column('notes', sa.String(512), nullable=False),
        sa.Column('last_status', sa.String(16), nullable=False),
        sa.Column('last_error', sa.String(512), nullable=True),
        sa.Column('last_checked_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )

    # Server 1 comes from the environment settings used before servers moved into the database.
    from app.config import get_settings
    from app.core.security import encrypt_secret
    from app.db import utcnow

    s = get_settings()
    real = s.mt5_gateway == 'manager'
    servers = sa.table('mt5_servers', *[sa.column(c) for c in (
        'id', 'name', 'kind', 'is_demo', 'address', 'manager_login', 'manager_password_enc', 'terminal_name',
        'timeout_ms', 'enabled', 'is_price_feed', 'notes', 'last_status', 'created_at', 'updated_at')])
    now = utcnow()
    conn = op.get_bind()
    has_data = any(conn.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM {t})')).scalar()
                   for t in ('account_types', 'trading_accounts', 'sync_state', 'ib_commissions', 'deals', 'positions'))
    # Existing rows need server 1, and dev / demo keeps its simulator. A fresh production database gets no row:
    # the MT5 server is then set up under MT5 > Configuration (a simulator would be refused in production).
    if (real and s.mt5_server) or not s.is_prod or has_data:
        op.bulk_insert(servers, [{
            'id': 1, 'name': 'Main server' if real else 'Simulator', 'kind': 'manager' if real else 'simulator',
            'is_demo': False, 'address': s.mt5_server if real else '',
            'manager_login': s.mt5_manager_login if real else 0,
            'manager_password_enc': encrypt_secret(s.mt5_manager_password) if real else '',
            'terminal_name': s.mt5_server_name, 'timeout_ms': s.mt5_timeout_ms, 'enabled': real or not s.is_prod,
            'is_price_feed': True, 'notes': 'Created from the MT5_* settings when multi-server support was installed.',
            'last_status': 'unknown', 'created_at': now, 'updated_at': now}])
        if conn.dialect.name == 'postgresql':
            op.execute("SELECT setval(pg_get_serial_sequence('mt5_servers', 'id'), 1)")

    for table in ('account_types', 'trading_accounts'):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column('server_id', sa.Integer(), nullable=True))
        op.execute(sa.text(f'UPDATE {table} SET server_id = 1'))
        with op.batch_alter_table(table) as batch:
            batch.alter_column('server_id', existing_type=sa.Integer(), nullable=False)
            batch.create_foreign_key(f'fk_{table}_server_id', 'mt5_servers', ['server_id'], ['id'])
            batch.create_index(f'ix_{table}_server_id', ['server_id'])

    with op.batch_alter_table('sync_state') as batch:
        batch.add_column(sa.Column('server_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_sync_state_server_id', 'mt5_servers', ['server_id'], ['id'])
    op.execute(sa.text('UPDATE sync_state SET server_id = 1'))

    # ib_commissions keeps its rows and id sequence: alter in place.
    with op.batch_alter_table('ib_commissions') as batch:
        batch.add_column(sa.Column('server_id', sa.Integer(), nullable=True))
    op.execute(sa.text('UPDATE ib_commissions SET server_id = 1'))
    # Find the existing (deal_ticket, ib_id) constraint by its columns: its name depends on the
    # database and on whether this database was ever downgraded. SQLite reports it unnamed.
    naming = {'uq': 'uq_%(table_name)s_%(column_0_N_name)s'}
    old_uq = next((uc['name'] for uc in sa.inspect(op.get_bind()).get_unique_constraints('ib_commissions')
                   if uc['column_names'] == ['deal_ticket', 'ib_id'] and uc['name']),
                  'uq_ib_commissions_deal_ticket_ib_id')
    with op.batch_alter_table('ib_commissions', naming_convention=naming) as batch:
        batch.drop_constraint(old_uq, type_='unique')
        batch.alter_column('server_id', existing_type=sa.Integer(), nullable=False)
        batch.create_foreign_key('fk_ib_commissions_server_id', 'mt5_servers', ['server_id'], ['id'])
        batch.create_index('ix_ib_commissions_server_id', ['server_id'])
        batch.create_unique_constraint('uq_ib_commissions_server_deal_ib', ['server_id', 'deal_ticket', 'ib_id'])

    _rebuild('deals', DEAL_COLUMNS, 'ticket', with_server=True)
    _rebuild('positions', POSITION_COLUMNS, 'position_id', with_server=True)


def downgrade() -> None:
    _rebuild('positions', POSITION_COLUMNS, 'position_id', with_server=False)
    _rebuild('deals', DEAL_COLUMNS, 'ticket', with_server=False)
    with op.batch_alter_table('ib_commissions') as batch:
        batch.drop_constraint('uq_ib_commissions_server_deal_ib', type_='unique')
        batch.drop_index('ix_ib_commissions_server_id')
        batch.drop_constraint('fk_ib_commissions_server_id', type_='foreignkey')
        batch.drop_column('server_id')
        batch.create_unique_constraint('uq_ib_commissions_deal_ticket_ib_id', ['deal_ticket', 'ib_id'])
    with op.batch_alter_table('sync_state') as batch:
        batch.drop_constraint('fk_sync_state_server_id', type_='foreignkey')
        batch.drop_column('server_id')
    for table in ('trading_accounts', 'account_types'):
        with op.batch_alter_table(table) as batch:
            batch.drop_index(f'ix_{table}_server_id')
            batch.drop_constraint(f'fk_{table}_server_id', type_='foreignkey')
            batch.drop_column('server_id')
    op.drop_table('mt5_servers')
    server_kind.drop(op.get_bind(), checkfirst=True)
