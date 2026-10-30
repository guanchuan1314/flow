"""Promo-code campaigns: deposit bonuses paid as MT5 credit."""

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import Conflict, ServiceError
from app.db import utcnow
from app.models import Campaign, CampaignRedemption, PaymentRequest
from app.mt5 import MT5Gateway
from app.services.wallet import money


def create_campaign(db: Session, actor, **fields) -> Campaign:
    fields["code"] = fields["code"].upper()
    if db.scalar(select(Campaign).where(Campaign.code == fields["code"])):
        raise Conflict("Campaign code already exists")
    c = Campaign(**fields)
    db.add(c)
    db.flush()
    audit(db, actor, "campaign.create", "campaign", c.id, fields)
    db.commit()
    return c


def validate_code(db: Session, code: str, client_id: int, amount: Decimal) -> Campaign:
    c = db.scalar(select(Campaign).where(Campaign.code == code.upper()))
    now = utcnow()
    if c is None or not c.active:
        raise ServiceError("Invalid promo code")
    if (c.starts_at and now < c.starts_at) or (c.ends_at and now > c.ends_at):
        raise ServiceError("Promo code is not currently valid")
    if amount < c.min_deposit:
        raise ServiceError(f"Minimum deposit for this promo is {c.min_deposit}")
    used = db.scalar(select(func.count()).select_from(CampaignRedemption).where(CampaignRedemption.campaign_id == c.id))
    if c.max_redemptions is not None and used >= c.max_redemptions:
        raise ServiceError("Promo code has been fully redeemed")
    if c.once_per_client and db.scalar(select(CampaignRedemption.id).where(
            CampaignRedemption.campaign_id == c.id, CampaignRedemption.client_id == client_id)):
        raise ServiceError("Promo code already used")
    return c


def apply_deposit_bonus(db: Session, gw: MT5Gateway, payment: PaymentRequest) -> CampaignRedemption | None:
    """Credit the bonus for an approved deposit. Caller commits."""
    if not payment.promo_code or not payment.trading_login:
        return None
    c = validate_code(db, payment.promo_code, payment.client_id, payment.amount)
    bonus = money(payment.amount * c.bonus_pct / 100)
    if c.max_bonus > 0:
        bonus = min(bonus, c.max_bonus)
    if bonus <= 0:
        return None
    gw.balance_operation(payment.trading_login, bonus, f"Bonus {c.code}", credit=True)
    r = CampaignRedemption(campaign_id=c.id, client_id=payment.client_id, payment_request_id=payment.id,
                           login=payment.trading_login, bonus_amount=bonus)
    db.add(r)
    return r


def set_active(db: Session, actor, campaign: Campaign, active: bool) -> Campaign:
    campaign.active = active
    audit(db, actor, "campaign.active", "campaign", campaign.id, {"active": active})
    db.commit()
    return campaign
