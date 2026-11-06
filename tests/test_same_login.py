"""Two servers with the same login: they are two accounts, of two clients. Nothing of one may reach the other."""

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.core.errors import Conflict, Forbidden
from app.db import utcnow
from app.models import (
    AccountGroup, AutomationRule, AutomationRun, Client, ClientStatus, CopyStrategy, CopySubscription, Deal,
    MT5Server, PammFund, PaymentKind, PaymentRequest, Position, SyncState, TradingAccount,
)
from app.mt5 import MT5Error, routed
from app.services import (
    account_removal, accounts, automation, clients, copy_trading, mt5_sync, pamm, payments, servers, sync, trader_styles,
)
from tests.test_multi_server import deal, second, two  # noqa: F401
from tests.test_web import admin_web, csrf_of, form_post  # noqa: F401

LOGIN = 5001


def open_position(db, server_id: int, position: int) -> None:
    db.add(Position(server_id=server_id, position_id=position, login=LOGIN, symbol="EURUSD", action=0, volume=Decimal("1"),
                    price_open=Decimal("1"), price_current=Decimal("1"), time_create=utcnow(), synced_at=utcnow(),
                    is_open=True))


def account(db, server_id: int, login: int = LOGIN) -> TradingAccount:
    client = Client(email=f"{login}-{server_id}@example.com", first_name="A", last_name=f"S{server_id}",
                    referral_code=f"r{login}x{server_id}", status=ClientStatus.active)
    db.add(client)
    db.flush()
    group = db.scalar(select(AccountGroup).where(AccountGroup.name == f"G{server_id}"))
    if group is None:
        group = AccountGroup(name=f"G{server_id}", server_id=server_id, mt5_group="real\\standard", currency="USD",
                             default_leverage=100, leverage_options=[100, 200])
        db.add(group)
        db.flush()
    acc = TradingAccount(login=login, server_id=server_id, client_id=client.id, account_group_id=group.id,
                         group="real\\standard", currency="USD", leverage=100, is_demo=False)
    db.add(acc)
    db.commit()
    return acc


@pytest.fixture
def same(db, second, two):  # noqa: F811
    """Login 5001 on both servers, each with its own money on MT5."""
    other = two[second.id]
    other.users[LOGIN] = replace(other.users[7001], login=LOGIN, email="other@example.com")
    two[1].balance[LOGIN], other.balance[LOGIN] = Decimal("100"), Decimal("900")
    other.credit[LOGIN] = Decimal("0")
    return SimpleNamespace(first=account(db, 1), other=account(db, second.id), id=second.id, mt5=two[1], mt5_other=other)


# ---------------------------------------------------------------- saying which account
def test_a_login_alone_is_not_enough_and_server_login_is(db, same):
    with pytest.raises(Conflict, match="more than one MT5 server"):
        accounts.get_account(db, LOGIN)
    assert accounts.get_account(db, f"{same.id}:{LOGIN}").id == same.other.id
    assert accounts.get_account(db, same.first.ref).id == same.first.id


def test_a_client_means_their_own_account(db, same):
    owner = db.get(Client, same.other.client_id)
    assert accounts.get_client_account(db, owner, LOGIN).id == same.other.id
    with pytest.raises(Forbidden):
        accounts.get_client_account(db, owner, same.first.ref)


def test_a_call_without_a_server_is_refused_never_guessed(db, same):
    with pytest.raises(MT5Error, match="more than one MT5 server"):
        routed().balance_operation(LOGIN, Decimal("1"), "Deposit")
    with pytest.raises(MT5Error, match="more than one MT5 server"):
        routed().get_accounts([LOGIN])
    assert same.mt5.balance[LOGIN] == 100 and same.mt5_other.balance[LOGIN] == 900


# ---------------------------------------------------------------- money
def test_a_transfer_moves_money_on_the_accounts_server_only(db, same):
    owner = db.get(Client, same.other.client_id)
    from app.models import WalletTxKind
    from app.services import wallet

    wallet.post(db, owner.id, Decimal("50"), WalletTxKind.deposit, ref_type="test", ref_id=1, comment="")
    db.commit()
    accounts.transfer_to_mt5(db, routed(), None, owner, same.other, Decimal("50"))
    assert same.mt5_other.balance[LOGIN] == 950 and same.mt5.balance[LOGIN] == 100
    accounts.transfer_from_mt5(db, routed(), None, owner, same.other, Decimal("20"))
    assert same.mt5_other.balance[LOGIN] == 930 and same.mt5.balance[LOGIN] == 100


def test_a_payment_remembers_its_server(db, same):
    owner = db.get(Client, same.other.client_id)
    owner.kyc_status = "approved"
    req = payments.create_request(db, owner, owner, PaymentKind.deposit, Decimal("70"), method="bank",
                                  trading_login=same.other.ref)
    assert (req.server_id, req.trading_login) == (same.id, LOGIN)
    payments._execute(db, None, None, req, None)
    assert same.mt5_other.balance[LOGIN] == 970 and same.mt5.balance[LOGIN] == 100


def test_settings_change_on_the_accounts_server_only(db, same):
    accounts.change_leverage(db, routed(), None, same.other, 200)
    assert same.mt5_other.users[LOGIN].leverage == 200 and same.mt5.users[LOGIN].leverage != 200


def test_pamm_reads_and_moves_money_on_the_funds_server(db, same):
    fund = PammFund(name="F", manager_client_id=same.other.client_id, server_id=same.id, master_login=LOGIN,
                    total_units=Decimal("0"), last_nav=Decimal("1"))
    db.add(fund)
    db.commit()
    assert pamm.current_nav(routed(), fund)[0] == same.mt5_other.get_account(LOGIN).equity


# ---------------------------------------------------------------- sync
def test_import_adds_the_second_account_with_its_own_client(db, gw):
    first = db.get(MT5Server, 1)
    other = servers.create_server(db, None, name="Second", kind=first.kind, terminal_name="Second-Live")
    from tests.test_trader_styles import accounts as make_accounts

    login, = make_accounts(db, gw, first, 1)
    gw.users[login].email = ""
    summary = mt5_sync.sync_trading_accounts(db, other, None, gw)
    assert summary["created"] == 1
    rows = db.scalars(select(TradingAccount).where(TradingAccount.login == login)).all()
    assert {r.server_id for r in rows} == {1, other.id} and len({r.client_id for r in rows}) == 2


def test_each_server_keeps_its_own_deals_balance_and_sync_state(db, same):
    sync.ingest_deals(db, [deal(1, LOGIN)], publish=False, server_id=1)
    sync.ingest_deals(db, [deal(1, LOGIN, price="2")], publish=False, server_id=same.id)
    assert db.scalar(select(func.count()).select_from(Deal)) == 2
    assert db.scalar(select(func.count()).select_from(SyncState)) == 2
    sync.sync_accounts(db, same.mt5_other, [LOGIN], same.id)
    db.refresh(same.first), db.refresh(same.other)
    assert same.other.balance == 900 and same.first.balance == 0


def test_a_position_closing_on_one_server_leaves_the_other_alone(db, same):
    for server_id, position in ((1, 11), (same.id, 22)):
        open_position(db, server_id, position)
    db.commit()
    sync.sync_positions(db, same.mt5_other, [LOGIN], same.id)
    assert db.get(Position, (same.id, 22)).is_open is False and db.get(Position, (1, 11)).is_open is True


def test_removal_takes_only_that_servers_account(db, same):
    sync.ingest_deals(db, [deal(1, LOGIN)], publish=False, server_id=1)
    sync.ingest_deals(db, [deal(1, LOGIN)], publish=False, server_id=same.id)
    account_removal._remove_accounts(db, db.get(MT5Server, same.id), [LOGIN])
    assert db.scalars(select(TradingAccount.server_id).where(TradingAccount.login == LOGIN)).all() == [1]
    assert db.scalars(select(Deal.server_id)).all() == [1]
    assert db.scalars(select(SyncState.server_id)).all() == [1]


# ---------------------------------------------------------------- who sees what
def test_staff_see_the_rows_of_their_clients_account_only(db, same, monkeypatch):
    sync.ingest_deals(db, [deal(1, LOGIN)], publish=False, server_id=1)
    sync.ingest_deals(db, [deal(2, LOGIN)], publish=False, server_id=same.id)
    mine = same.first.client_id
    monkeypatch.setattr(clients, "visible_client_ids", lambda staff: select(Client.id).where(Client.id == mine))
    seen = db.scalars(select(Deal.server_id).where(clients.visible_rows(None, Deal))).all()
    assert seen == [1]
    assert clients.visible_account(db, None, LOGIN).id == same.first.id
    with pytest.raises(Forbidden):
        clients.visible_account(db, None, same.other.ref)


def test_trader_styles_keep_the_two_accounts_apart(db, same):
    for ticket in range(1, 13):
        sync.ingest_deals(db, [deal(ticket, LOGIN, position=ticket)], publish=False, server_id=1)
    found = trader_styles.metrics(db, None, 30)
    assert set(found) == {(1, LOGIN)}


# ---------------------------------------------------------------- copy trading
def test_a_masters_deal_is_copied_only_from_its_own_server(db, same, monkeypatch):
    strategy = CopyStrategy(name="S", owner_client_id=same.first.client_id, server_id=1, master_login=LOGIN)
    db.add(strategy)
    db.commit()
    seen = []
    monkeypatch.setattr(copy_trading, "process_master_deal", lambda db, gw, s, d: seen.append(d.server_id))
    sync.ingest_deals(db, [deal(1, LOGIN)], publish=False, server_id=same.id)
    copy_trading.handle_deals(db, db.scalars(select(Deal)).all())
    assert seen == []
    sync.ingest_deals(db, [deal(1, LOGIN)], publish=False, server_id=1)
    copy_trading.handle_deals(db, db.scalars(select(Deal)).all())
    assert seen == [1]


def test_a_strategy_can_have_the_same_master_login_on_each_server(db, same):
    for server_id, name in ((1, "One"), (same.id, "Two")):
        db.add(CopyStrategy(name=name, owner_client_id=same.first.client_id, server_id=server_id, master_login=LOGIN))
    db.commit()
    assert db.scalar(select(func.count()).select_from(CopyStrategy)) == 2


def test_a_follower_is_on_the_strategys_server(db, same):
    follower = account(db, same.id, 7001)
    same.mt5_other.balance[7001] = Decimal("1000")
    strategy = CopyStrategy(name="S", owner_client_id=same.other.client_id, server_id=same.id, master_login=LOGIN,
                            min_investment=Decimal("1"))
    db.add(strategy)
    db.commit()
    sub = copy_trading.subscribe(db, routed(), None, db.get(Client, follower.client_id), strategy, follower.ref)
    assert (sub.server_id, sub.follower_login) == (same.id, 7001)
    assert db.scalar(select(func.count()).select_from(CopySubscription)) == 1


# ---------------------------------------------------------------- automation
def rule(db, server_id=None, cooldown=24) -> AutomationRule:
    return automation.save_rule(db, None, None, name=f"r{server_id}", trigger="equity_below", params={"amount": "10"},
                                filters={"kind": "live"}, action="client_note", action_params={"message": "x"},
                                cooldown_hours=cooldown, server_id=server_id)


def low_equity(db, same):
    same.first.balance = same.other.balance = Decimal("1")
    same.first.equity = same.other.equity = Decimal("1")
    db.commit()


def test_a_rule_can_pick_a_server(db, same):
    low_equity(db, same)
    assert [m["server_id"] for m in automation.matches_now(db, rule(db, same.id))] == [same.id]


def test_a_rule_on_every_server_finds_both_accounts_and_cools_them_down_apart(db, same):
    low_equity(db, same)
    r = rule(db)
    assert {m["target"] for m in automation.matches_now(db, r)} == {f"account:{same.first.ref}", f"account:{same.other.ref}"}
    automation.run_rule(db, r)
    runs = db.scalars(select(AutomationRun)).all()
    assert {x.server_id for x in runs} == {1, same.id} and all(x.outcome == "done" for x in runs)
    assert automation.matches_now(db, r, due_only=True) == []


def test_a_rule_finding_clients_uses_their_accounts_server(db, same):
    r = automation.save_rule(db, None, None, name="n", trigger="no_deposit", params={"days": 0}, filters={"kind": "live"},
                             action="client_note", action_params={"message": "x"}, cooldown_hours=0, server_id=same.id)
    assert [m["client_id"] for m in automation.matches_now(db, r)] == [same.other.client_id]


def test_a_rule_cant_move_accounts_to_another_servers_group(db, same):
    group = db.scalar(select(AccountGroup).where(AccountGroup.name == "G1"))
    with pytest.raises(Exception, match="another MT5 server"):
        automation.save_rule(db, None, None, name="m", trigger="equity_below", params={"amount": "10"}, filters={},
                             action="change_group", action_params={"account_group_id": group.id}, cooldown_hours=1,
                             server_id=same.id)


def test_deleting_a_server_deletes_its_rules_not_widens_them(db, same):
    rule(db, same.id)
    db.execute(TradingAccount.__table__.delete().where(TradingAccount.server_id == same.id))
    db.commit()
    servers.delete_server(db, None, db.get(MT5Server, same.id), purge=True)
    assert db.scalar(select(func.count()).select_from(AutomationRule)) == 0


# ---------------------------------------------------------------- pages
def test_an_old_link_asks_which_server(admin_web, db, same):  # noqa: F811
    page = admin_web.get(f"/admin/accounts/trading-accounts/{LOGIN}").text
    assert same.first.ref in page and same.other.ref in page
    assert admin_web.get(f"/admin/accounts/trading-accounts/{same.other.ref}").status_code == 200


def test_changing_an_account_needs_the_server(admin_web, db, same):  # noqa: F811
    page = f"/admin/accounts/trading-accounts/{same.other.ref}"
    form_post(admin_web, page, f"{page}/leverage", {"leverage": "200"})
    assert same.mt5_other.users[LOGIN].leverage == 200 and same.mt5.users[LOGIN].leverage != 200


@pytest.mark.parametrize("url", [
    "/admin/accounts/trading-accounts", "/admin/accounts/account-groups", "/admin/trades/positions/data",
    "/admin/trades/positions/closed", "/admin/trades/deals/data", "/admin/trades/orders/data",
    "/admin/trades/deals/history",
])
def test_the_lists_have_a_server_column(admin_web, db, same, url):  # noqa: F811
    assert "<th>Server</th>" in admin_web.get(url).text


def test_a_rule_picks_its_server_on_the_form(admin_web, db, same):  # noqa: F811
    form_post(admin_web, "/admin/automation/new", "/admin/automation/new", {
        "name": "Picked", "trigger": "equity_below", "t_equity_below_amount": "10", "action": "client_note",
        "a_client_note_message": "x", "kind": "live", "cooldown_hours": "1", "server_id": str(same.id)})
    picked = db.scalar(select(AutomationRule).where(AutomationRule.name == "Picked"))
    assert picked.server_id == same.id
    assert "<th>Server</th>" in admin_web.get("/admin/automation").text
    assert f'value="{same.id}" selected' in admin_web.get(f"/admin/automation/{picked.id}").text


def test_payments_show_their_server(admin_web, db, same):  # noqa: F811
    db.add(PaymentRequest(client_id=same.other.client_id, kind=PaymentKind.deposit, amount=Decimal("1"), server_id=same.id,
                          trading_login=LOGIN))
    db.commit()
    assert "· Second" in admin_web.get("/admin/finance/payments").text or "· Second" in admin_web.get("/admin/payments").text


