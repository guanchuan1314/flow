"""PAMM: pooled money-management accounts with unit (NAV) accounting.

NAV per unit = master equity / total units. Deposits buy units at the current NAV
and withdrawals redeem units at the current NAV, so each investor's share of
profit/loss is exactly proportional to their units over time. Fees are
charged by moving units from investors to the manager (no money leaves the pool):
  * performance fee: per-investor high-water mark (NAV/unit)
  * management fee: annual %, accrued pro-rata
"""

import logging
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import Conflict, NotFound, ServiceError
from app.db import utcnow
from app.models import (
    AccountPurpose, AccountStatus, Client, CopyStrategy, PammFund, PammInvestment, PammNavSnapshot,
    PammTransaction, PammTxKind, StrategyStatus, WalletTxKind,
)
from app.mt5 import MT5Error, MT5Gateway
from app.services import accounts, wallet

log = logging.getLogger(__name__)
Q8 = Decimal("0.00000001")


def _units(v: Decimal) -> Decimal:
    return Decimal(v).quantize(Q8, rounding=ROUND_DOWN)


def nav(fund: PammFund, equity: Decimal) -> Decimal:
    if fund.total_units <= 0:
        return Decimal("1")
    return _units(Decimal(equity) / Decimal(fund.total_units))


def current_nav(gw: MT5Gateway, fund: PammFund) -> tuple[Decimal, Decimal]:
    equity = gw.on(fund.server_id).get_account(fund.master_login).equity
    return Decimal(equity), nav(fund, equity)


def _lock_fund(db: Session, fund: PammFund) -> PammFund:
    """Lock the fund row (and reload it) so money moves on one fund run one at a time."""
    return db.scalar(select(PammFund).where(PammFund.id == fund.id).with_for_update()
                     .execution_options(populate_existing=True))


def _investment(db: Session, fund: PammFund, client_id: int, create: bool = False, is_manager: bool = False,
                lock: bool = False) -> PammInvestment:
    q = select(PammInvestment).where(PammInvestment.fund_id == fund.id, PammInvestment.client_id == client_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    inv = db.scalar(q)
    if inv is None:
        if not create:
            raise NotFound("No investment in this fund")
        inv = PammInvestment(fund_id=fund.id, client_id=client_id, units=Decimal("0"), net_invested=Decimal("0"),
                             high_water_mark=Decimal("0"), is_manager=is_manager)
        db.add(inv)
        db.flush()
    return inv


def _manager_investment(db: Session, fund: PammFund) -> PammInvestment:
    return _investment(db, fund, fund.manager_client_id, create=True, is_manager=True)


def create_fund(db: Session, gw: MT5Gateway, actor, manager: Client, master_login, name: str, **opts) -> PammFund:
    acc = accounts.get_client_account(db, manager, master_login)
    if acc.is_demo or acc.status != AccountStatus.active:
        raise ServiceError("Master must be an active live account")
    master_login, gw = acc.login, gw.on(acc.server_id)
    if db.scalar(select(CopyStrategy.id).where(CopyStrategy.server_id == acc.server_id,
                                               CopyStrategy.master_login == master_login)):
        raise Conflict("Account is already a copy-trading master")
    if db.scalar(select(PammFund.id).where(PammFund.name == name)):
        raise Conflict("Fund name already exists")
    info = gw.get_account(master_login)
    if info.equity > 0 or gw.get_positions([master_login]):
        raise ServiceError("Master account must be empty (zero equity, no positions) when the fund is created")
    fund = PammFund(name=name, manager_client_id=manager.id, server_id=acc.server_id, master_login=master_login,
                    total_units=Decimal("0"), last_nav=Decimal("1"), last_fee_at=utcnow(), **opts)
    acc.purpose = AccountPurpose.pamm_master
    db.add(fund)
    db.flush()
    _manager_investment(db, fund)
    audit(db, actor, "pamm.fund.create", "pamm_fund", fund.id, {"master": master_login, **opts})
    db.commit()
    return fund


def invest(db: Session, gw: MT5Gateway, actor, fund: PammFund, client: Client, amount) -> PammTransaction:
    amount = wallet.money(amount)
    fund = _lock_fund(db, fund)
    if fund.status != StrategyStatus.active:
        raise ServiceError("Fund is not accepting investments")
    is_manager = client.id == fund.manager_client_id
    if amount < fund.min_investment and not is_manager:
        raise ServiceError(f"Minimum investment is {fund.min_investment}")
    equity, price = current_nav(gw, fund)
    if fund.total_units > 0 and equity <= 0:
        raise ServiceError("Fund equity is depleted; investments are suspended")
    wallet.post(db, client.id, -amount, WalletTxKind.pamm_invest, ref_type="pamm_fund", ref_id=fund.id,
                comment=f"Invest in PAMM {fund.name}")
    try:
        gw.on(fund.server_id).balance_operation(fund.master_login, amount, f"PAMM {fund.id} in c{client.id}")
    except MT5Error as exc:
        db.rollback()
        raise ServiceError(f"MT5 deposit failed: {exc}", 502) from exc
    units = _units(amount / price)
    inv = _investment(db, fund, client.id, create=True, is_manager=is_manager, lock=True)
    old_units = Decimal(inv.units)
    # Blend the HWM so new money never pays a fee on gains made before it arrived.
    inv.high_water_mark = _units((old_units * Decimal(inv.high_water_mark) + units * price) / (old_units + units))
    inv.units = old_units + units
    inv.net_invested = wallet.money(inv.net_invested + amount)
    fund.total_units = Decimal(fund.total_units) + units
    fund.last_nav = price
    tx = PammTransaction(fund_id=fund.id, investment_id=inv.id, kind=PammTxKind.deposit, amount=amount,
                         units=units, nav=price)
    db.add(tx)
    audit(db, actor, "pamm.invest", "pamm_fund", fund.id, {"client_id": client.id, "amount": amount, "units": units, "nav": price})
    db.commit()
    return tx


def _performance_fee_units(fund: PammFund, inv: PammInvestment, units: Decimal, price: Decimal) -> Decimal:
    if inv.is_manager or price <= inv.high_water_mark or fund.performance_fee_pct <= 0:
        return Decimal("0")
    fee_value = (price - Decimal(inv.high_water_mark)) * units * Decimal(fund.performance_fee_pct) / 100
    return _units(fee_value / price)


def withdraw(db: Session, gw: MT5Gateway, actor, fund: PammFund, client: Client, amount=None, units=None) -> PammTransaction:
    """Redeem by gross value (`amount`) or by `units`; performance fee on the redeemed units is crystallised first.

    The fund and investment rows stay locked until the commit, so two withdrawals can't both
    pass the holding check: the second one waits, then sees the units the first one left."""
    fund = _lock_fund(db, fund)
    inv = _investment(db, fund, client.id, lock=True)
    equity, price = current_nav(gw, fund)
    if price <= 0:
        raise ServiceError("Fund NAV is zero")
    if units is None:
        if amount is None:
            raise ServiceError("Provide amount or units")
        units = _units(wallet.money(amount) / price)
    units = _units(units)
    if units <= 0 or units > inv.units:
        raise ServiceError("Invalid units: exceeds holding" if units > 0 else "Nothing to withdraw")
    if units == inv.units or abs(Decimal(inv.units) - units) < Decimal("0.0001"):
        units = Decimal(inv.units)  # full exit, no dust
    fee_units = _performance_fee_units(fund, inv, units, price)
    payout = wallet.money((units - fee_units) * price)
    gw = gw.on(fund.server_id)
    margin_free = gw.get_account(fund.master_login).margin_free
    if payout > margin_free:
        db.rollback()
        raise ServiceError("Not enough free margin on the master account; manager must reduce exposure first")
    try:
        gw.balance_operation(fund.master_login, -payout, f"PAMM {fund.id} out c{client.id}")
    except MT5Error as exc:
        db.rollback()
        raise ServiceError(f"MT5 withdrawal failed: {exc}", 502) from exc
    inv.units = Decimal(inv.units) - units
    inv.net_invested = wallet.money(max(Decimal(inv.net_invested) - payout, Decimal(0)))
    if fee_units > 0:
        mgr = _manager_investment(db, fund)
        mgr.units = Decimal(mgr.units) + fee_units
        db.add(PammTransaction(fund_id=fund.id, investment_id=inv.id, kind=PammTxKind.performance_fee,
                               amount=wallet.money(fee_units * price), units=-fee_units, nav=price))
    fund.total_units = Decimal(fund.total_units) - (units - fee_units)
    fund.last_nav = price
    wallet.post(db, client.id, payout, WalletTxKind.pamm_redeem, ref_type="pamm_fund", ref_id=fund.id,
                comment=f"Withdraw from PAMM {fund.name}")
    tx = PammTransaction(fund_id=fund.id, investment_id=inv.id, kind=PammTxKind.withdrawal, amount=payout,
                         units=-(units - fee_units), nav=price)
    db.add(tx)
    audit(db, actor, "pamm.withdraw", "pamm_fund", fund.id,
          {"client_id": client.id, "units": units, "fee_units": fee_units, "payout": payout, "nav": price})
    db.commit()
    return tx


def settle_fees(db: Session, gw: MT5Gateway, fund: PammFund) -> dict:
    """Periodic fee crystallisation (e.g. monthly). Moves units investor -> manager."""
    fund = _lock_fund(db, fund)
    equity, price = current_nav(gw, fund)
    now = utcnow()
    days = Decimal((now - (fund.last_fee_at or now)).total_seconds()) / Decimal(86400)
    mgr = _manager_investment(db, fund)
    perf_total = mgmt_total = Decimal("0")
    investors = db.scalars(select(PammInvestment).where(PammInvestment.fund_id == fund.id,
                                                        PammInvestment.is_manager.is_(False),
                                                        PammInvestment.units > 0))
    for inv in list(investors):
        mgmt_units = Decimal("0")
        if fund.management_fee_pct_annual > 0 and days > 0:
            mgmt_units = _units(Decimal(inv.units) * Decimal(fund.management_fee_pct_annual) / 100 * days / 365)
        perf_units = _performance_fee_units(fund, inv, Decimal(inv.units) - mgmt_units, price)
        for kind, fee_units in ((PammTxKind.management_fee, mgmt_units), (PammTxKind.performance_fee, perf_units)):
            if fee_units > 0:
                inv.units = Decimal(inv.units) - fee_units
                mgr.units = Decimal(mgr.units) + fee_units
                db.add(PammTransaction(fund_id=fund.id, investment_id=inv.id, kind=kind,
                                       amount=wallet.money(fee_units * price), units=-fee_units, nav=price))
        if price > inv.high_water_mark:
            inv.high_water_mark = price
        perf_total += perf_units
        mgmt_total += mgmt_units
    fund.last_fee_at, fund.last_nav = now, price
    audit(db, None, "pamm.fees", "pamm_fund", fund.id,
          {"nav": price, "performance_units": perf_total, "management_units": mgmt_total})
    db.commit()
    return {"nav": price, "performance_fee_units": perf_total, "management_fee_units": mgmt_total}


def snapshot(db: Session, gw: MT5Gateway, fund: PammFund) -> PammNavSnapshot:
    equity, price = current_nav(gw, fund)
    snap = PammNavSnapshot(fund_id=fund.id, equity=wallet.money(equity), total_units=fund.total_units, nav=price)
    fund.last_nav = price
    db.add(snap)
    db.commit()
    return snap


def investor_statement(db: Session, gw: MT5Gateway, fund: PammFund, client_id: int) -> dict:
    inv = _investment(db, fund, client_id)
    _, price = current_nav(gw, fund)
    value = wallet.money(Decimal(inv.units) * price)
    return {"fund_id": fund.id, "units": inv.units, "nav": price, "value": value,
            "net_invested": inv.net_invested, "pnl": wallet.money(value - Decimal(inv.net_invested)),
            "high_water_mark": inv.high_water_mark,
            "share_pct": (Decimal(inv.units) / Decimal(fund.total_units) * 100).quantize(Decimal("0.01"))
            if fund.total_units else Decimal("0")}


def due_for_fees(fund: PammFund, period_days: int = 30) -> bool:
    return fund.last_fee_at is None or utcnow() - fund.last_fee_at >= timedelta(days=period_days)


def set_fund_status(db: Session, actor, fund: PammFund, status: StrategyStatus) -> PammFund:
    before, fund.status = fund.status, status
    audit(db, actor, "pamm.fund.status", "pamm_fund", fund.id, {"before": before, "after": status})
    db.commit()
    return fund
