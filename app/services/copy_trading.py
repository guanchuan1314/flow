"""MAM / social copy trading.

Each master trade deal is mirrored onto every follower subscription:
  * ENTRY_IN  -> open a follower position sized by the allocation mode, and link it
  * closing   -> close the same *fraction* of the linked follower position
Every (subscription, master deal) pair is recorded in CopyExecution, so the
engine is idempotent across the push (DealSubscribe) and poll (sync) paths.

A dealer timeout (retcode -1) leaves the outcome unknown: the trade may have been executed.
Such executions are marked "unknown" and reconciled on the next pass for the strategy: the
follower's positions are checked (the open's comment carries "copy#<execution id>") and the
trade is only sent again when MT5 shows it did not happen.

Assumes hedging-mode accounts (one MT5 position per opening deal).
"""

import logging
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.audit import audit
from app.core.errors import Conflict, Forbidden, ServiceError
from app.db import utcnow
from app.models import (
    AccountPurpose, AccountStatus, AllocationMode, Client, CopyExecution, CopyLink, CopyStrategy,
    CopySubscription, Deal, PammFund, StrategyStatus, SubscriptionStatus, TradingAccount, WalletTxKind,
)
from app.models.trading import CLOSING_ENTRIES, DEAL_BUY, ENTRY_IN, TRADE_ACTIONS
from app.mt5 import BUY, SELL, MT5Error, MT5Gateway, get_gateway
from app.services import accounts, wallet

log = logging.getLogger(__name__)
RET_TIMEOUT = -1  # the gateway's "dealer timeout": the request may or may not have been executed


def _step() -> Decimal:
    return Decimal(get_settings().copy_lot_step)


def round_lots(volume: Decimal) -> Decimal:
    step = _step()
    return (volume / step).to_integral_value(rounding=ROUND_DOWN) * step


# ---------------------------------------------------------------- admin


def create_strategy(db: Session, actor, owner: Client, master_login, name: str, **opts) -> CopyStrategy:
    acc = accounts.get_client_account(db, owner, master_login)
    if acc.is_demo or acc.status != AccountStatus.active:
        raise ServiceError("Master must be an active live account")
    master_login = acc.login
    if db.scalar(select(CopySubscription.id).where(CopySubscription.server_id == acc.server_id,
                                                   CopySubscription.follower_login == master_login,
                                                   CopySubscription.status != SubscriptionStatus.stopped)):
        raise ServiceError("A follower account cannot be a master")
    if db.scalar(select(PammFund.id).where(PammFund.server_id == acc.server_id, PammFund.master_login == master_login)):
        raise Conflict("Account is already a PAMM master")
    if db.scalar(select(CopyStrategy.id).where(CopyStrategy.name == name)):
        raise Conflict("Strategy name already exists")
    s = CopyStrategy(name=name, owner_client_id=owner.id, server_id=acc.server_id, master_login=master_login, **opts)
    acc.purpose = AccountPurpose.copy_master
    db.add(s)
    db.flush()
    audit(db, actor, "copy.strategy.create", "copy_strategy", s.id, {"master": master_login, **opts})
    db.commit()
    return s


def subscribe(db: Session, gw: MT5Gateway, actor, follower: Client, strategy: CopyStrategy, follower_login,
              allocation_mode: AllocationMode = AllocationMode.equity_ratio, ratio: Decimal = Decimal("1"),
              max_lot: Decimal | None = None) -> CopySubscription:
    if strategy.status != StrategyStatus.active:
        raise ServiceError("Strategy is not accepting followers")
    acc = accounts.get_client_account(db, follower, follower_login)
    if acc.status != AccountStatus.active or acc.purpose != AccountPurpose.trading:
        raise ServiceError("Follower must be an active trading account")
    if acc.is_demo:
        raise ServiceError("Demo accounts cannot follow live strategies")
    follower_login = acc.login
    master = accounts.get_account(db, f"{strategy.server_id}:{strategy.master_login}")
    if master.server_id != acc.server_id:
        raise ServiceError(f"This strategy trades on {master.server.display_name}; your account must be on the same "
                           "MT5 server to copy it")
    if follower_login == strategy.master_login:
        raise ServiceError("An account cannot follow itself")
    if ratio <= 0:
        raise ServiceError("ratio must be positive")
    active = db.scalar(select(CopySubscription).where(CopySubscription.server_id == acc.server_id,
                                                      CopySubscription.follower_login == follower_login,
                                                      CopySubscription.status != SubscriptionStatus.stopped))
    if active:
        raise Conflict("This account already follows a strategy")
    equity = gw.on(acc.server_id).get_account(follower_login).equity
    if equity < strategy.min_investment:
        raise ServiceError(f"Minimum equity to follow is {strategy.min_investment}")
    sub = db.scalar(select(CopySubscription).where(CopySubscription.strategy_id == strategy.id,
                                                   CopySubscription.follower_login == follower_login))
    if sub is None:
        sub = CopySubscription(strategy_id=strategy.id, server_id=acc.server_id, follower_login=follower_login,
                               follower_client_id=follower.id)
        db.add(sub)
    sub.allocation_mode, sub.ratio, sub.max_lot = allocation_mode, ratio, max_lot
    sub.status, sub.high_water_mark = SubscriptionStatus.active, equity
    db.flush()
    audit(db, actor, "copy.subscribe", "copy_subscription", sub.id,
          {"strategy": strategy.id, "login": follower_login, "mode": allocation_mode, "ratio": ratio})
    db.commit()
    return sub


def unsubscribe(db: Session, gw: MT5Gateway, actor, sub: CopySubscription, close_positions: bool = True) -> CopySubscription:
    gw = gw.on(sub.server_id)
    if close_positions:
        for link in db.scalars(select(CopyLink).where(CopyLink.subscription_id == sub.id, CopyLink.is_open.is_(True))):
            res = gw.close_position(sub.follower_login, link.follower_position_id, link.symbol, link.side,
                                    link.follower_volume_open, comment="copy stop")
            if res.ok:
                link.is_open, link.follower_volume_open = False, Decimal("0")
            else:
                log.warning("could not close follower position %s: %s", link.follower_position_id, res.retcode)
    else:
        for link in db.scalars(select(CopyLink).where(CopyLink.subscription_id == sub.id, CopyLink.is_open.is_(True))):
            link.is_open = False  # follower keeps the positions and manages them manually
    sub.status = SubscriptionStatus.stopped
    audit(db, actor, "copy.unsubscribe", "copy_subscription", sub.id, {"close_positions": close_positions})
    db.commit()
    return sub


def adjust_high_water_mark(db: Session, acc: TradingAccount, amount: Decimal) -> None:
    """Deposits/withdrawals on a follower account move its HWM so they are not counted as profit."""
    sub = db.scalar(select(CopySubscription).where(CopySubscription.server_id == acc.server_id,
                                                   CopySubscription.follower_login == acc.login,
                                                   CopySubscription.status != SubscriptionStatus.stopped))
    if sub:
        sub.high_water_mark = wallet.money(sub.high_water_mark + amount)


# ---------------------------------------------------------------- engine


def follower_volume(gw: MT5Gateway, sub: CopySubscription, master_login: int, master_volume: Decimal) -> Decimal:
    mode, ratio = sub.allocation_mode, Decimal(sub.ratio)
    if mode == AllocationMode.fixed_lot:
        raw = ratio
    elif mode == AllocationMode.lot_multiplier:
        raw = master_volume * ratio
    else:
        infos = {i.login: i for i in gw.get_accounts([master_login, sub.follower_login])}
        m, f = infos.get(master_login), infos.get(sub.follower_login)
        if not m or not f:
            return Decimal("0")
        m_val, f_val = (m.equity, f.equity) if mode == AllocationMode.equity_ratio else (m.balance, f.balance)
        if m_val <= 0 or f_val <= 0:
            return Decimal("0")
        raw = master_volume * Decimal(f_val) / Decimal(m_val) * ratio
    volume = round_lots(raw)
    if sub.max_lot is not None:
        volume = min(volume, Decimal(sub.max_lot))
    return volume if volume >= Decimal(get_settings().copy_min_lot) else Decimal("0")


def _claim(db: Session, sub: CopySubscription, deal: Deal, action: str) -> CopyExecution | None:
    """Insert the execution row *before* trading, so a deal is mirrored at most once
    even if the push and poll paths (or two processes) see it concurrently."""
    ex = CopyExecution(subscription_id=sub.id, master_deal_ticket=deal.ticket, follower_login=sub.follower_login,
                       action=action, symbol=deal.symbol, volume=Decimal("0"), status="pending")
    db.add(ex)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    return ex


def _finish(ex: CopyExecution, status: str, volume=None, retcode=None, message: str = "", follower_deal=None) -> None:
    ex.status, ex.retcode, ex.message, ex.follower_deal_ticket = status, retcode, message[:255], follower_deal
    if volume is not None:
        ex.volume = volume


def _marker(ex: CopyExecution) -> str:
    """First word of the follower order's comment; finds the position again after a timeout."""
    return f"copy#{ex.id}"


def _copy_open(db: Session, gw: MT5Gateway, strategy: CopyStrategy, sub: CopySubscription, deal: Deal) -> None:
    ex = _claim(db, sub, deal, "open")
    if ex is not None:
        _send_open(db, gw, strategy, sub, deal, ex)


def _send_open(db: Session, gw: MT5Gateway, strategy: CopyStrategy, sub: CopySubscription, deal: Deal,
               ex: CopyExecution) -> None:
    max_age = timedelta(seconds=get_settings().copy_max_open_age_seconds)
    if utcnow() - deal.time > max_age:
        return _finish(ex, "skipped", message="master deal too old to copy")
    volume = follower_volume(gw, sub, strategy.master_login, Decimal(deal.volume))
    if volume <= 0:
        return _finish(ex, "skipped", message="volume below minimum lot")
    side = BUY if deal.action == DEAL_BUY else SELL
    try:
        res = gw.open_position(sub.follower_login, deal.symbol, side, volume,
                               comment=f"{_marker(ex)} {strategy.id}:{deal.position_id}")
    except MT5Error as exc:
        return _finish(ex, "failed", volume, exc.retcode, str(exc))
    if res.retcode == RET_TIMEOUT:
        return _finish(ex, "unknown", volume, res.retcode, res.comment or "dealer timeout")
    if not res.ok:
        return _finish(ex, "failed", volume, res.retcode, res.comment)
    _opened(db, sub, deal, ex, res.volume or volume, res.position_id, res.retcode, res.deal_ticket)


def _opened(db: Session, sub: CopySubscription, deal: Deal, ex: CopyExecution, filled: Decimal, position_id: int,
            retcode=None, follower_deal=None, message: str = "") -> None:
    _finish(ex, "done", filled, retcode, message, follower_deal=follower_deal)
    side = BUY if deal.action == DEAL_BUY else SELL
    link = db.scalar(select(CopyLink).where(CopyLink.subscription_id == sub.id,
                                            CopyLink.master_position_id == deal.position_id))
    if link is None:
        db.add(CopyLink(subscription_id=sub.id, master_position_id=deal.position_id,
                        follower_position_id=position_id, symbol=deal.symbol, side=side,
                        master_volume_open=deal.volume, follower_volume_open=filled))
    else:  # scaling into an existing master position
        link.master_volume_open += deal.volume
        link.follower_volume_open += filled
        link.is_open = True


def _close_volume(link: CopyLink, deal: Deal) -> Decimal:
    master_open = Decimal(link.master_volume_open)
    fraction = min(Decimal(deal.volume) / master_open, Decimal(1)) if master_open > 0 else Decimal(1)
    volume = round_lots(Decimal(link.follower_volume_open) * fraction)
    remainder = Decimal(link.follower_volume_open) - volume
    if fraction >= 1 or remainder < Decimal(get_settings().copy_min_lot):
        volume = Decimal(link.follower_volume_open)  # close fully rather than leave dust
    return volume


def _open_link(db: Session, sub: CopySubscription, deal: Deal) -> CopyLink | None:
    return db.scalar(select(CopyLink).where(CopyLink.subscription_id == sub.id,
                                            CopyLink.master_position_id == deal.position_id,
                                            CopyLink.is_open.is_(True)))


def _copy_close(db: Session, gw: MT5Gateway, sub: CopySubscription, deal: Deal) -> None:
    link = _open_link(db, sub, deal)
    if link is None:
        return  # position was opened before the subscription, or the open was skipped
    ex = _claim(db, sub, deal, "close")
    if ex is not None:
        _send_close(db, gw, sub, deal, ex, link)


def _send_close(db: Session, gw: MT5Gateway, sub: CopySubscription, deal: Deal, ex: CopyExecution,
                link: CopyLink) -> None:
    volume = _close_volume(link, deal)
    try:
        res = gw.close_position(sub.follower_login, link.follower_position_id, link.symbol, link.side, volume,
                                comment=f"{_marker(ex)} close {deal.position_id}")
    except MT5Error as exc:
        return _finish(ex, "failed", volume, exc.retcode, str(exc))
    if res.retcode == RET_TIMEOUT:
        return _finish(ex, "unknown", volume, res.retcode, res.comment or "dealer timeout")
    if not res.ok:
        return _finish(ex, "failed", volume, res.retcode, res.comment)
    _closed(ex, link, deal, volume, res.retcode, res.deal_ticket)


def _closed(ex: CopyExecution, link: CopyLink, deal: Deal, volume: Decimal, retcode=None, follower_deal=None,
            message: str = "") -> None:
    _finish(ex, "done", volume, retcode, message, follower_deal=follower_deal)
    link.master_volume_open = max(Decimal(link.master_volume_open) - Decimal(deal.volume), Decimal(0))
    link.follower_volume_open = Decimal(link.follower_volume_open) - volume
    if link.master_volume_open <= 0 or link.follower_volume_open <= 0:
        link.is_open = False


def reconcile(db: Session, gw: MT5Gateway, strategy: CopyStrategy) -> int:
    """Settle the strategy's "unknown" executions (dealer timeouts) from the follower's positions.

    open: a position whose comment starts with the execution's marker means it was filled;
          otherwise it is sent again.
    close: the linked position gone or reduced by the volume means it was closed; otherwise
          it is sent again. Returns how many were settled."""
    gw = gw.on(strategy.server_id)
    ids = db.scalars(select(CopyExecution.id).join(CopySubscription, CopySubscription.id == CopyExecution.subscription_id)
                     .where(CopySubscription.strategy_id == strategy.id, CopyExecution.status == "unknown")
                     .order_by(CopyExecution.id)).all()
    settled = 0
    for ex_id in ids:
        ex = db.scalar(select(CopyExecution).where(CopyExecution.id == ex_id, CopyExecution.status == "unknown")
                       .with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if ex is None:
            continue  # settled meanwhile, or another process is on it
        sub = db.get(CopySubscription, ex.subscription_id)
        deal = db.scalar(select(Deal).where(Deal.server_id == strategy.server_id, Deal.login == strategy.master_login,
                                            Deal.ticket == ex.master_deal_ticket))
        try:
            positions = gw.get_positions([ex.follower_login])
        except MT5Error as exc:
            log.warning("cannot reconcile copy execution %s yet: %s", ex.id, exc)
            db.rollback()
            break
        if deal is None:
            _finish(ex, "failed", retcode=ex.retcode, message="master deal not found; check the follower manually")
        elif ex.action == "open":
            mine = next((p for p in positions if (p.comment or "").split(" ", 1)[0] == _marker(ex)), None)
            if mine is not None:
                _opened(db, sub, deal, ex, Decimal(mine.volume), mine.position_id, message="confirmed after timeout")
            else:
                _send_open(db, gw, strategy, sub, deal, ex)
        else:
            link = _open_link(db, sub, deal)
            if link is None:
                _finish(ex, "skipped", retcode=ex.retcode, message="link already closed")
            volume = Decimal(ex.volume)
            pos = next((p for p in positions if p.position_id == link.follower_position_id), None)
            if pos is None or Decimal(pos.volume) <= Decimal(link.follower_volume_open) - volume:
                _closed(ex, link, deal, volume, message="confirmed after timeout")
            else:
                _send_close(db, gw, sub, deal, ex, link)
        settled += ex.status != "unknown"
        db.commit()
    return settled


def process_master_deal(db: Session, gw: MT5Gateway, strategy: CopyStrategy, deal: Deal) -> None:
    if strategy.status == StrategyStatus.closed:
        return
    gw = gw.on(strategy.server_id)
    reconcile(db, gw, strategy)  # earlier timeouts first, so a close finds the position it closes
    subs = db.scalars(select(CopySubscription).where(CopySubscription.strategy_id == strategy.id,
                                                     CopySubscription.status != SubscriptionStatus.stopped))
    for sub in list(subs):
        if deal.entry == ENTRY_IN:
            if strategy.status == StrategyStatus.active and sub.status == SubscriptionStatus.active:
                _copy_open(db, gw, strategy, sub, deal)
        elif deal.entry in CLOSING_ENTRIES:
            _copy_close(db, gw, sub, deal)  # paused subscriptions still mirror closes
        db.commit()


def handle_deals(db: Session, deals: list[Deal]) -> None:
    trade_deals = [d for d in deals if d.action in TRADE_ACTIONS]
    if not trade_deals:
        return
    masters = list({(d.server_id, d.login) for d in trade_deals})
    strategies = {(s.server_id, s.master_login): s for s in db.scalars(select(CopyStrategy).where(
        tuple_(CopyStrategy.server_id, CopyStrategy.master_login).in_(masters)))}
    if not strategies:
        return
    for deal in trade_deals:
        strategy = strategies.get((deal.server_id, deal.login))
        if strategy:
            try:
                gw = get_gateway(strategy.server_id)  # followers share the master's server
            except MT5Error as exc:
                log.error("cannot copy deal %s of strategy %s: %s", deal.ticket, strategy.id, exc)
                continue
            process_master_deal(db, gw, strategy, deal)


# ---------------------------------------------------------------- performance fee


def settle_fee(db: Session, gw: MT5Gateway, sub: CopySubscription) -> Decimal:
    """Charge the performance fee on equity above the high-water mark."""
    strategy = db.get(CopyStrategy, sub.strategy_id)
    gw = gw.on(sub.server_id)
    equity = gw.get_account(sub.follower_login).equity
    profit = Decimal(equity) - Decimal(sub.high_water_mark)
    fee = wallet.money(profit * Decimal(strategy.performance_fee_pct) / 100) if profit > 0 else Decimal("0")
    now = utcnow()
    if fee > 0:
        gw.balance_operation(sub.follower_login, -fee, f"Perf fee strategy {strategy.id}")
        wallet.post(db, strategy.owner_client_id, fee, WalletTxKind.copy_fee, ref_type="copy_subscription",
                    ref_id=sub.id, comment=f"Performance fee from {sub.follower_login}")
        sub.fees_paid = wallet.money(sub.fees_paid + fee)
        sub.high_water_mark = wallet.money(equity - fee)
        audit(db, None, "copy.fee", "copy_subscription", sub.id, {"equity": equity, "fee": fee})
    sub.last_fee_at = now
    db.commit()
    return fee


def settle_all_fees_due(db: Session, gw: MT5Gateway | None = None, period_days: int = 30) -> dict:
    cutoff = utcnow() - timedelta(days=period_days)
    out = {}
    for sub in list(db.scalars(select(CopySubscription).where(CopySubscription.status != SubscriptionStatus.stopped))):
        if sub.last_fee_at is None:
            sub.last_fee_at = sub.created_at  # fee period starts at subscription
            db.commit()
        if sub.last_fee_at > cutoff:
            continue
        try:
            out[sub.id] = settle_fee(db, gw or get_gateway(sub.server_id), sub)
        except (MT5Error, ServiceError) as exc:
            db.rollback()
            log.error("fee settlement failed for subscription %s: %s", sub.id, exc)
    return out


def ensure_owner(strategy: CopyStrategy, client: Client) -> None:
    if strategy.owner_client_id != client.id:
        raise Forbidden("Not your strategy")


def set_strategy_status(db: Session, actor, strategy: CopyStrategy, status: StrategyStatus) -> CopyStrategy:
    before, strategy.status = strategy.status, status
    audit(db, actor, "copy.strategy.status", "copy_strategy", strategy.id, {"before": before, "after": status})
    db.commit()
    return strategy
