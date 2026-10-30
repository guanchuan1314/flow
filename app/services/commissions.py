"""IB / referral commissions computed from synced deals.

For each trade deal of a referred client, the direct IB's plan picks the first matching
rule (by priority) and computes a rebate. The rebate is split up the IB tree by the
plan's `level_shares`. Rows are unique per (deal, IB), so recalculation is idempotent.
"""

import logging
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.masks import matches
from app.core.errors import Conflict, ServiceError
from app.db import utcnow
from app.models import (
    AccountGroup, Client, CommissionPlan, CommissionRule, Deal, IBCommission, IBProfile, IBStatus,
    RuleKind, TradingAccount, WalletTxKind,
)
from app.models.trading import CLOSING_ENTRIES, ENTRY_IN, TRADE_ACTIONS
from app.services import wallet

log = logging.getLogger(__name__)
MAX_DEPTH = 10


# ---------------------------------------------------------------- admin


def create_plan(db: Session, actor, name: str, level_shares: list[float], rules: list[dict],
                trigger: str = "close", min_hold_seconds: int = 0) -> CommissionPlan:
    if db.scalar(select(CommissionPlan).where(CommissionPlan.name == name)):
        raise Conflict("Plan name already exists")
    if not level_shares or any(s < 0 for s in level_shares) or sum(level_shares) > 1.0000001:
        raise ServiceError("level_shares must be non-negative and sum to at most 1")
    if trigger not in ("close", "open", "both"):
        raise ServiceError("trigger must be close, open or both")
    plan = CommissionPlan(name=name, level_shares=level_shares, trigger=trigger, min_hold_seconds=min_hold_seconds,
                          rules=[CommissionRule(**r) for r in rules])
    db.add(plan)
    db.flush()
    audit(db, actor, "commission_plan.create", "commission_plan", plan.id,
          {"level_shares": level_shares, "rules": rules})
    db.commit()
    return plan


def make_ib(db: Session, actor, client: Client, plan_id: int | None, parent_ib_id: int | None = None) -> IBProfile:
    if db.scalar(select(IBProfile).where(IBProfile.client_id == client.id)):
        raise Conflict("Client is already an IB")
    if parent_ib_id is None and client.referred_by_id:
        parent = db.scalar(select(IBProfile).where(IBProfile.client_id == client.referred_by_id))
        parent_ib_id = parent.id if parent else None
    ib = IBProfile(client_id=client.id, plan_id=plan_id, parent_ib_id=parent_ib_id)
    db.add(ib)
    db.flush()
    audit(db, actor, "ib.create", "ib", ib.id, {"client_id": client.id, "plan_id": plan_id, "parent": parent_ib_id})
    db.commit()
    return ib


# ---------------------------------------------------------------- calculation


def ib_chain(db: Session, direct: IBProfile) -> list[IBProfile]:
    chain, seen, node = [], set(), direct
    while node is not None and node.id not in seen and len(chain) < MAX_DEPTH:
        chain.append(node)
        seen.add(node.id)
        node = db.get(IBProfile, node.parent_ib_id) if node.parent_ib_id else None
    return chain


def match_rule(plan: CommissionPlan, symbol: str, account_group_id: int | None) -> CommissionRule | None:
    for rule in sorted(plan.rules, key=lambda r: r.priority):
        if matches(rule.symbol_pattern, symbol) and rule.account_group_id in (None, account_group_id):
            return rule
    return None


def rule_amount(rule: CommissionRule, deal: Deal, account_group: AccountGroup | None) -> Decimal:
    lots = Decimal(deal.volume)
    if rule.kind == RuleKind.per_lot:
        return rule.value * lots
    if rule.kind == RuleKind.commission_pct:
        return abs(Decimal(deal.commission)) * rule.value / 100
    if rule.kind == RuleKind.spread_share:
        markup = Decimal(account_group.spread_markup_points if account_group else 0)
        if not markup or not deal.tick_size:
            return Decimal("0")
        point = Decimal(1).scaleb(-deal.digits)
        point_value_per_lot = Decimal(deal.tick_value) * point / Decimal(deal.tick_size)
        return markup * point_value_per_lot * lots * rule.value
    return Decimal("0")


def _triggers(plan: CommissionPlan, deal: Deal) -> bool:
    if deal.entry == ENTRY_IN:
        return plan.trigger in ("open", "both")
    return deal.entry in CLOSING_ENTRIES and plan.trigger in ("close", "both")


def _held_long_enough(db: Session, plan: CommissionPlan, deal: Deal) -> bool:
    if not plan.min_hold_seconds or deal.entry == ENTRY_IN:
        return True
    opened = db.scalar(select(func.min(Deal.time)).where(Deal.server_id == deal.server_id,
                                                         Deal.position_id == deal.position_id, Deal.entry == ENTRY_IN))
    return opened is None or (deal.time - opened).total_seconds() >= plan.min_hold_seconds


def calculate_for_deal(db: Session, deal: Deal) -> list[IBCommission]:
    if deal.action not in TRADE_ACTIONS or deal.volume <= 0:
        return []
    acc = db.scalar(select(TradingAccount).where(TradingAccount.login == deal.login))
    if acc is None or acc.is_demo:
        return []
    client = db.get(Client, acc.client_id)
    if client is None or client.referred_by_id is None:
        return []
    direct = db.scalar(select(IBProfile).where(IBProfile.client_id == client.referred_by_id))
    if direct is None or direct.plan is None:
        return []
    plan = direct.plan
    if not _triggers(plan, deal) or not _held_long_enough(db, plan, deal):
        return []
    rule = match_rule(plan, deal.symbol, acc.account_group_id)
    if rule is None:
        return []
    total = rule_amount(rule, deal, acc.account_group)
    if total <= 0:
        return []
    out = []
    for level, (ib, share) in enumerate(zip(ib_chain(db, direct), plan.level_shares), start=1):
        if ib.status != IBStatus.active or ib.client_id == client.id:
            continue
        amount = wallet.money(total * Decimal(str(share)))
        if amount <= 0:
            continue
        exists = db.scalar(select(IBCommission.id).where(IBCommission.server_id == deal.server_id,
                                                         IBCommission.deal_ticket == deal.ticket, IBCommission.ib_id == ib.id))
        if exists:
            continue
        row = IBCommission(server_id=deal.server_id, deal_ticket=deal.ticket, ib_id=ib.id, level=level, trader_client_id=client.id,
                           login=deal.login, symbol=deal.symbol, volume=deal.volume, amount=amount, rule_id=rule.id)
        db.add(row)
        out.append(row)
    return out


def handle_deals(db: Session, deals: list[Deal]) -> None:
    created = 0
    for deal in deals:
        created += len(calculate_for_deal(db, deal))
    if created:
        db.commit()


def recalculate(db: Session, date_from, date_to) -> int:
    deals = db.scalars(select(Deal).where(Deal.time >= date_from, Deal.time <= date_to,
                                          Deal.action.in_(TRADE_ACTIONS)).order_by(Deal.time))
    created = sum(len(calculate_for_deal(db, d)) for d in deals)
    db.commit()
    return created


# ---------------------------------------------------------------- payout


def payout(db: Session, actor, ib_id: int | None = None) -> dict:
    """Credit pending commissions to each IB's wallet."""
    q = select(IBCommission).where(IBCommission.status == "pending")
    if ib_id is not None:
        q = q.where(IBCommission.ib_id == ib_id)
    per_ib: dict[int, list[IBCommission]] = {}
    for row in db.scalars(q.with_for_update()):
        per_ib.setdefault(row.ib_id, []).append(row)
    now, result = utcnow(), {}
    for iid, rows in per_ib.items():
        ib = db.get(IBProfile, iid)
        total = wallet.money(sum((r.amount for r in rows), Decimal("0")))
        if total <= 0:
            continue
        tx = wallet.post(db, ib.client_id, total, WalletTxKind.ib_commission, ref_type="ib_payout",
                         ref_id=iid, comment=f"IB commission payout ({len(rows)} deals)")
        for r in rows:
            r.status, r.paid_at = "paid", now
        result[iid] = {"amount": total, "deals": len(rows), "wallet_tx_id": tx.id}
    audit(db, actor, "ib.payout", "ib", ib_id, {"payouts": result})
    db.commit()
    return result


def update_ib(db: Session, actor, ib: IBProfile, changes: dict) -> IBProfile:
    parent_id = changes.get("parent_ib_id", ib.parent_ib_id)
    if parent_id is not None:
        if parent_id == ib.id:
            raise ServiceError("An IB cannot be its own parent")
        node, seen = db.get(IBProfile, parent_id), set()
        if node is None:
            raise ServiceError("Parent IB not found", 404)
        while node is not None and node.id not in seen:  # refuse cycles in the tree
            if node.id == ib.id:
                raise ServiceError("That parent would create a loop in the IB tree")
            seen.add(node.id)
            node = db.get(IBProfile, node.parent_ib_id) if node.parent_ib_id else None
    if changes.get("plan_id") is not None and db.get(CommissionPlan, changes["plan_id"]) is None:
        raise ServiceError("Commission plan not found", 404)
    for key, value in changes.items():
        setattr(ib, key, value)
    audit(db, actor, "ib.update", "ib", ib.id, changes)
    db.commit()
    return ib
