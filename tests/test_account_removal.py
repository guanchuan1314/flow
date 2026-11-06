"""Accounts not on MT5 leave Flow with everything of theirs; accounts on MT5 keep their history."""

from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db import utcnow
from app.models import (
    Client, CompactedRecord, CompactSummary, CopyStrategy, Deal, MT5Server, Order, PaymentKind, PaymentRequest,
    PaymentStatus, Position, StrategyStatus, SyncState, TradingAccount, Wallet,
)
from app.mt5 import MT5Error
from app.services import account_removal, compact, mt5_sync, sync
from app.services.client_sync import PLACEHOLDER_EMAIL
from tests.test_compact import history, money, trade
from tests.test_trader_styles import accounts


@pytest.fixture
def server(db):
    return db.get(MT5Server, 1)


@pytest.fixture
def groups(db, gw, server):
    mt5_sync.sync_account_groups(db, server, None, gw)


def account(db, login):
    return db.scalar(select(TradingAccount).where(TradingAccount.login == login))


def rows(db, model, login):
    return db.scalars(select(model).where(model.login == login)).all()


def placeholder(db, login):
    """Make the account's client an imported one without an email."""
    client = db.get(Client, account(db, login).client_id)
    client.email = f"mt5-1-{login}{PLACEHOLDER_EMAIL}"
    db.commit()
    return client.id


def gone_from_mt5(db, gw, login):
    gw.users.pop(login)
    account(db, login).on_server = False
    db.commit()


def test_everything_of_an_account_not_on_mt5_goes(db, gw, server, admin):
    login, other = accounts(db, gw, server, 2)
    client = placeholder(db, login)
    money(gw, login, ("1000", "Deposit #1"), ("5", "Deposit #2"))
    money(gw, other, ("7", "Deposit #3"))
    trade(gw, login)
    compact.run(db, admin, server, [login], gw=gw)
    money(gw, login, ("20", "Deposit #4"))
    sync.sync_deals(db, gw, [login, other])
    sync.sync_orders(db, gw, [login, other])
    db.add(Position(server_id=1, position_id=9, login=login, symbol="EURUSD", action=0, volume=Decimal("1"), price_open=Decimal("1"),
                    time_create=utcnow(), synced_at=utcnow()))
    db.add(SyncState(login=login, server_id=1)) if not db.get(SyncState, (1, login)) else None
    db.commit()
    assert rows(db, Deal, login) and rows(db, CompactedRecord, login) and rows(db, CompactSummary, login)

    gone_from_mt5(db, gw, login)
    result = account_removal.remove_gone(db, server, gw)
    db.expire_all()
    assert (result["removed"], result["clients"], result["kept"]) == (1, 1, 0)
    for model in (TradingAccount, Deal, Order, Position, CompactedRecord, CompactSummary):
        assert not rows(db, model, login), model
    assert db.get(Client, client) is None and db.get(SyncState, (1, login)) is None
    assert account(db, other) and rows(db, Deal, other)  # the other account is untouched


def test_mt5_must_confirm_it(db, gw, server, monkeypatch):
    login, = accounts(db, gw, server, 1)
    account(db, login).on_server = False  # flagged, but MT5 still has it
    db.commit()
    assert account_removal.remove_gone(db, server, gw)["kept"] == 1
    assert account(db, login)

    def down(_):
        raise MT5Error("connection lost", 10)
    monkeypatch.setattr(gw, "get_user", down)
    gw.users.pop(login)
    result = account_removal.remove_gone(db, server, gw)
    assert result["removed"] == 0 and "connection lost" in result["error"] and account(db, login)


def test_real_clients_and_their_payments_stay(db, gw, server):
    login, = accounts(db, gw, server, 1)
    client = account(db, login).client_id
    gone_from_mt5(db, gw, login)
    assert account_removal.remove_gone(db, server, gw) == {"removed": 1, "clients": 0, "kept": 0, "error": ""}
    assert db.get(Client, client)


def test_placeholders_that_still_matter_stay(db, gw, server):
    paid, rich, shared = accounts(db, gw, server, 3)
    clients = {login: placeholder(db, login) for login in (paid, rich, shared)}
    db.add(PaymentRequest(client_id=clients[paid], kind=PaymentKind.deposit, status=PaymentStatus.approved, amount=Decimal("5"),
                          currency="USD", trading_login=paid, created_at=utcnow()))
    db.add(Wallet(client_id=clients[rich], currency="USD", balance=Decimal("3")))
    second = account(db, shared)
    db.add(TradingAccount(login=999123, server_id=1, client_id=clients[shared], group=second.group, currency="USD",
                          leverage=100, is_demo=second.is_demo, status=second.status))
    db.commit()
    for login in (paid, rich, shared):
        gone_from_mt5(db, gw, login)
    result = account_removal.remove_gone(db, server, gw)
    assert (result["removed"], result["clients"]) == (3, 0)
    db.expire_all()
    assert all(db.get(Client, c) for c in clients.values())
    assert db.scalars(select(PaymentRequest).where(PaymentRequest.trading_login == paid)).all()


def test_a_copy_trading_role_keeps_the_account(db, gw, server):
    login, = accounts(db, gw, server, 1)
    db.add(CopyStrategy(name="Keep", owner_client_id=account(db, login).client_id, server_id=1, master_login=login, status=StrategyStatus.active))
    db.commit()
    gone_from_mt5(db, gw, login)
    result = account_removal.remove_gone(db, server, gw)
    assert (result["removed"], result["kept"]) == (0, 1) and account(db, login)


def test_an_import_that_sees_no_user_removes_nothing(db, gw, server, groups):
    login, = accounts(db, gw, server, 1)
    gw.users.clear()
    summary = mt5_sync.sync_trading_accounts(db, server, None, gw)
    assert (summary["missing"], summary["removed"]) == (0, 0) and account(db, login)


def test_an_account_on_mt5_keeps_deals_and_orders_mt5_no_longer_has(db, gw, server, groups):
    login, = accounts(db, gw, server, 1)
    money(gw, login, ("1000", "Deposit #1"), ("5", "Deposit #2"))
    trade(gw, login)
    start = datetime(2000, 1, 1)
    sync.sync_deals(db, gw, [login])
    sync.ingest_orders(db, gw.get_order_history([login], start, datetime(2100, 1, 1)), 1, False)
    deals, orders = len(rows(db, Deal, login)), len(rows(db, Order, login))
    assert deals == 4 and orders

    gw.delete_deals([d.ticket for d in history(gw, login)])
    gw.delete_orders([o.ticket for o in gw.get_order_history([login], start, datetime(2100, 1, 1))])
    mt5_sync.sync_trading_accounts(db, server, None, gw)
    sync.sync_deals(db, gw, [login])
    sync.sync_orders(db, gw, [login])
    db.expire_all()
    assert account(db, login).on_server and len(rows(db, Deal, login)) == deals and len(rows(db, Order, login)) == orders
