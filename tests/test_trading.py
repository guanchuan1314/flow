from decimal import Decimal

from sqlalchemy import func, select

from app.models import (
    AllocationMode, Client, CopyExecution, Deal, IBCommission, PammFund, Position, StrategyStatus,
    SubscriptionStatus, TradingAccount,
)
from app.models.marketing import RuleKind
from app.mt5 import BUY, SELL
from app.services import commissions, copy_trading, pamm, sync
from tests.conftest import login_client


# ---------------------------------------------------------------- sync
def test_deal_and_position_sync_is_idempotent(db, funded, gw):
    _, login = funded("sync@x.com", amount=5000)
    res = gw.open_position(login, "EURUSD", BUY, Decimal("1"))
    stats = sync.full_sync(db, gw)
    assert stats["new_deals"] == 2  # balance + open
    assert sync.full_sync(db, gw)["new_deals"] == 0
    pos = db.get(Position, (1, res.position_id))
    assert pos.is_open and pos.volume == Decimal("1")

    gw.set_price("EURUSD", "1.10100", "1.10110")
    gw.close_position(login, res.position_id, "EURUSD", BUY, Decimal("1"))
    sync.full_sync(db, gw)
    db.expire_all()
    assert not db.get(Position, (1, res.position_id)).is_open
    close = db.scalar(select(Deal).where(Deal.login == login, Deal.entry == 1))
    assert close.profit == Decimal("90.00")  # bought at ask 1.10010, sold at bid 1.10100: 9 pips * 1 lot
    acc = db.scalar(select(TradingAccount).where(TradingAccount.login == login))
    assert acc.balance == Decimal("5090.00")  # account summary synced


def test_sync_api(api, admin_h, funded, gw):
    _, login = funded("syncapi@x.com")
    gw.open_position(login, "XAUUSD", SELL, Decimal("0.5"))
    r = api.post("/api/sync", json={}, headers=admin_h)
    assert r.json()["new_deals"] == 2
    positions = api.get("/api/positions", params={"login": login}, headers=admin_h).json()
    assert len(positions) == 1 and positions[0]["symbol"] == "XAUUSD"
    deals = api.get("/api/deals", params={"login": login, "trades_only": True}, headers=admin_h).json()
    assert len(deals) == 1


# ---------------------------------------------------------------- MAM copy trading
def _strategy(db, gw, master_client, master_login, **kw):
    owner = db.get(Client, master_client["id"])
    return copy_trading.create_strategy(db, None, owner, master_login, kw.pop("name", "Alpha"), **kw)


def test_mam_equity_ratio_open_partial_and_full_close(db, funded, gw):
    m, master = funded("master@x.com", amount=10000)
    f1, follower1 = funded("f1@x.com", amount=5000)
    f2, follower2 = funded("f2@x.com", amount=20000)
    strat = _strategy(db, gw, m, master)
    s1 = copy_trading.subscribe(db, gw, None, db.get(Client, f1["id"]), strat, follower1)
    s2 = copy_trading.subscribe(db, gw, None, db.get(Client, f2["id"]), strat, follower2,
                                AllocationMode.lot_multiplier, Decimal("0.5"))
    sync.full_sync(db, gw)

    res = gw.open_position(master, "EURUSD", BUY, Decimal("1.00"))
    sync.sync_deals(db, gw)
    f1_pos = gw.get_positions([follower1])
    f2_pos = gw.get_positions([follower2])
    assert [p.volume for p in f1_pos] == [Decimal("0.50")]  # 5k / 10k equity
    assert [p.volume for p in f2_pos] == [Decimal("0.50")]  # multiplier 0.5

    # replaying the same deals must not duplicate copies
    sync.ingest_deals(db, gw.get_deals([master], db.scalar(select(func.min(Deal.time))), db.scalar(select(func.max(Deal.time)))))
    sync.sync_deals(db, gw)
    assert len(gw.get_positions([follower1])) == 1

    # partial close 40% -> followers close 40%
    gw.close_position(master, res.position_id, "EURUSD", BUY, Decimal("0.40"))
    sync.sync_deals(db, gw)
    assert gw.get_positions([follower1])[0].volume == Decimal("0.30")

    # paused subscription still mirrors closes
    s1.status = SubscriptionStatus.paused
    db.commit()
    gw.close_position(master, res.position_id, "EURUSD", BUY, Decimal("0.60"))
    sync.sync_deals(db, gw)
    assert gw.get_positions([follower1]) == [] and gw.get_positions([follower2]) == []
    done = db.scalar(select(func.count()).select_from(CopyExecution).where(CopyExecution.status == "done"))
    assert done == 6  # 2 subscribers x (open + partial + final close)


def test_mam_skips_paused_opens_and_min_lot(db, funded, gw):
    m, master = funded("m2@x.com", amount=100000)
    f, follower = funded("small@x.com", amount=500)
    strat = _strategy(db, gw, m, master, name="Beta", min_investment=Decimal("100"))
    copy_trading.subscribe(db, gw, None, db.get(Client, f["id"]), strat, follower)
    gw.open_position(master, "EURUSD", BUY, Decimal("1"))  # 500/100000 * 1 = 0.005 -> below min lot
    sync.sync_deals(db, gw)
    ex = db.scalar(select(CopyExecution))
    assert ex.status == "skipped" and "minimum" in ex.message
    strat.status = StrategyStatus.paused
    db.commit()
    gw.open_position(master, "EURUSD", BUY, Decimal("50"))
    sync.sync_deals(db, gw)
    assert gw.get_positions([follower]) == []


def test_mam_follow_rules_and_performance_fee(api, db, funded, gw):
    m, master = funded("m3@x.com", amount=10000)
    f, follower = funded("f3@x.com", amount=10000)
    strat = _strategy(db, gw, m, master, name="Gamma", performance_fee_pct=Decimal("20"))
    ch = login_client(api, "f3@x.com")
    assert api.post(f"/api/portal/copy/strategies/{strat.id}/follow", json={"follower_login": master},
                    headers=ch).status_code == 403  # not their account
    r = api.post(f"/api/portal/copy/strategies/{strat.id}/follow", json={"follower_login": follower}, headers=ch)
    assert r.status_code == 201, r.text
    sub_id = r.json()["id"]

    res = gw.open_position(master, "EURUSD", BUY, Decimal("1"))
    sync.sync_deals(db, gw)
    gw.set_price("EURUSD", "1.11000", "1.11010")  # +99 pips for follower buy at 1.10010
    gw.close_position(master, res.position_id, "EURUSD", BUY, Decimal("1"))
    sync.sync_deals(db, gw)
    assert gw.balance[follower] == Decimal("10990.00")

    from app.models import CopySubscription
    sub = db.get(CopySubscription, sub_id)
    fee = copy_trading.settle_fee(db, gw, sub)
    assert fee == Decimal("198.00")  # 20% of 990 profit
    assert gw.balance[follower] == Decimal("10792.00")
    assert copy_trading.settle_fee(db, gw, sub) == Decimal("0")  # HWM prevents double charge
    owner_wallet = api.get("/api/portal/wallet", headers=login_client(api, "m3@x.com")).json()
    assert Decimal(owner_wallet["balance"]) == Decimal("198.00")


# ---------------------------------------------------------------- PAMM
def test_pamm_nav_accounting_and_fees(db, funded, gw, api, admin_h):
    mgr, master = funded("pm@x.com", amount=0)
    a, _ = funded("inv_a@x.com", amount=0)
    b, _ = funded("inv_b@x.com", amount=0)
    from app.services import wallet
    from app.models import WalletTxKind
    for c in (a, b):
        wallet.post(db, c["id"], 10000, WalletTxKind.deposit)
    db.commit()

    fund = pamm.create_fund(db, gw, None, db.get(Client, mgr["id"]), master, "Fund One",
                            performance_fee_pct=Decimal("20"))
    ca, cb = db.get(Client, a["id"]), db.get(Client, b["id"])
    pamm.invest(db, gw, None, fund, ca, 1000)
    assert gw.balance[master] == Decimal("1000")

    # +50% performance: NAV 1.5
    gw.balance_operation(master, 500, "simulated profit")
    tx = pamm.invest(db, gw, None, fund, cb, 1500)  # B buys in at NAV 1.5
    assert tx.nav == Decimal("1.5") and tx.units == Decimal("1000")
    sa = pamm.investor_statement(db, gw, fund, ca.id)
    sb = pamm.investor_statement(db, gw, fund, cb.id)
    assert sa["value"] == Decimal("1500.00") and sb["value"] == Decimal("1500.00")

    # A redeems all: gain 0.5/unit over HWM 1.0 -> fee 20% * 500 = 100 -> payout 1400
    out = pamm.withdraw(db, gw, None, fund, ca, units=Decimal("1000"))
    assert out.amount == Decimal("1400.00")
    assert gw.balance[master] == Decimal("1600.00")
    mgr_stmt = pamm.investor_statement(db, gw, fund, mgr["id"])
    assert mgr_stmt["value"] == Decimal("100.00")  # fee stayed in the pool as manager units
    # B (bought at HWM 1.5) has no fee yet
    assert pamm.settle_fees(db, gw, fund)["performance_fee_units"] == Decimal("0")
    assert pamm.investor_statement(db, gw, fund, cb.id)["value"] == Decimal("1500.00")

    r = api.get(f"/api/pamm/funds/{fund.id}/investors", headers=admin_h)
    assert r.status_code == 200 and len(r.json()) == 3


def test_pamm_master_must_be_empty(db, funded, gw):
    mgr, master = funded("pm2@x.com", amount=100)
    import pytest
    from app.core.errors import ServiceError
    with pytest.raises(ServiceError):
        pamm.create_fund(db, gw, None, db.get(Client, mgr["id"]), master, "Fund Two")
    assert db.scalar(select(PammFund)) is None


# ---------------------------------------------------------------- IB commissions
def test_multilevel_ib_commissions_and_payout(api, admin_h, db, funded, gw):
    master_ib, _ = funded("ib1@x.com", amount=0)
    plan = commissions.create_plan(db, None, "Plan", [0.7, 0.3], [
        {"symbol_pattern": "XAU*", "kind": RuleKind.per_lot, "value": Decimal("10"), "priority": 10},
        {"symbol_pattern": "*", "kind": RuleKind.spread_share, "value": Decimal("0.5"), "priority": 50},
    ])
    ib1 = commissions.make_ib(db, None, db.get(Client, master_ib["id"]), plan.id)
    sub_ib, _ = funded("ib2@x.com", amount=0, ref=master_ib["referral_code"])
    ib2 = commissions.make_ib(db, None, db.get(Client, sub_ib["id"]), plan.id)
    assert ib2.parent_ib_id == ib1.id  # inferred from referral
    trader, login = funded("trader@x.com", amount=10000, ref=sub_ib["referral_code"])

    p = gw.open_position(login, "XAUUSD", BUY, Decimal("2"))
    gw.close_position(login, p.position_id, "XAUUSD", BUY, Decimal("2"))
    p = gw.open_position(login, "EURUSD", BUY, Decimal("1"))
    gw.close_position(login, p.position_id, "EURUSD", BUY, Decimal("1"))
    sync.sync_deals(db, gw)

    rows = db.scalars(select(IBCommission).order_by(IBCommission.id)).all()
    by = {(r.symbol, r.level): r.amount for r in rows}
    # XAU: 10/lot * 2 lots = 20 -> 14 / 6 ; EURUSD: 10 pts * $1/pt * 1 lot * 0.5 = 5 -> 3.50 / 1.50
    assert by == {("XAUUSD", 1): Decimal("14.00"), ("XAUUSD", 2): Decimal("6.00"),
                  ("EURUSD", 1): Decimal("3.50"), ("EURUSD", 2): Decimal("1.50")}
    # recalculation is idempotent
    assert commissions.recalculate(db, rows[0].created_at.replace(year=2000), rows[0].created_at.replace(year=2100)) == 0

    r = api.post("/api/commissions/payout", headers=admin_h)
    assert r.status_code == 200
    ib2_wallet = api.get("/api/portal/wallet", headers=login_client(api, "ib2@x.com")).json()
    assert Decimal(ib2_wallet["balance"]) == Decimal("17.50")
    tree = api.get(f"/api/ibs/{ib1.id}/tree", headers=admin_h).json()
    assert tree["sub_ibs"][0]["ib_id"] == ib2.id
    ref = api.get("/api/portal/referral", headers=login_client(api, "ib2@x.com")).json()
    assert ref["is_ib"] and ref["referred_clients"] == 1


def test_min_hold_rule_blocks_scalping(db, funded, gw):
    ib_client, _ = funded("ibh@x.com", amount=0)
    plan = commissions.create_plan(db, None, "Hold", [1.0], [
        {"symbol_pattern": "*", "kind": RuleKind.per_lot, "value": Decimal("5")}], min_hold_seconds=120)
    commissions.make_ib(db, None, db.get(Client, ib_client["id"]), plan.id)
    _, login = funded("scalper@x.com", amount=10000, ref=ib_client["referral_code"])
    p = gw.open_position(login, "EURUSD", BUY, Decimal("1"))
    gw.close_position(login, p.position_id, "EURUSD", BUY, Decimal("1"))
    sync.sync_deals(db, gw)
    assert db.scalar(select(func.count()).select_from(IBCommission)) == 0
