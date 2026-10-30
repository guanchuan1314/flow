"""Pydantic request/response models."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import (
    AccountPurpose, AccountStatus, AllocationMode, ClientStatus, IBStatus, KycStatus, PaymentKind,
    PaymentStatus, PammTxKind, RuleKind, StrategyStatus, SubscriptionStatus, WalletTxKind,
)


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


PositiveMoney = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2)]


# ---------------------------------------------------------------- auth
class LoginIn(BaseModel):
    email: EmailStr
    password: str
    totp_code: str | None = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    mfa_enrollment_required: bool = False


class TotpEnrollOut(BaseModel):
    secret: str
    otpauth_uri: str


class TotpCodeIn(BaseModel):
    code: str


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class ResetPasswordIn(BaseModel):
    token: str
    new_password: str


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str


# ---------------------------------------------------------------- staff
class RoleIn(BaseModel):
    name: str = Field(min_length=2, max_length=64)
    permissions: list[str]
    description: str = ""


class RoleUpdate(BaseModel):
    permissions: list[str] | None = None
    description: str | None = None


class RoleOut(ORM):
    id: int
    name: str
    description: str
    permissions: list[str]
    is_system: bool


class DepartmentIn(BaseModel):
    name: str


class DepartmentOut(ORM):
    id: int
    name: str


class StaffIn(BaseModel):
    email: EmailStr
    full_name: str
    password: str
    role_ids: list[int] = []
    department_id: int | None = None
    manager_id: int | None = None
    allowed_ips: list[str] = []


class StaffUpdate(BaseModel):
    full_name: str | None = None
    password: str | None = None
    role_ids: list[int] | None = None
    department_id: int | None = None
    manager_id: int | None = None
    allowed_ips: list[str] | None = None
    is_active: bool | None = None


class StaffOut(ORM):
    id: int
    email: str
    full_name: str
    is_active: bool
    department_id: int | None
    manager_id: int | None
    totp_enabled: bool
    allowed_ips: list[str]
    last_login_at: datetime | None
    roles: list[RoleOut]


# ---------------------------------------------------------------- clients
class ClientRegisterIn(BaseModel):
    email: EmailStr
    password: str
    first_name: str = Field(min_length=1, max_length=64)
    last_name: str = Field(min_length=1, max_length=64)
    phone: str | None = None
    country: str | None = Field(default=None, min_length=2, max_length=2)
    referral_code: str | None = None
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None


class ClientCreateIn(BaseModel):
    """Staff-created client / lead (password optional)."""
    email: EmailStr
    first_name: str
    last_name: str
    password: str | None = None
    phone: str | None = None
    country: str | None = Field(default=None, min_length=2, max_length=2)
    referral_code: str | None = None
    lead_source: str | None = None
    sales_staff_id: int | None = None


class ClientUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    country: str | None = Field(default=None, min_length=2, max_length=2)
    city: str | None = None
    address: str | None = None
    date_of_birth: date | None = None
    status: ClientStatus | None = None


class ClientSelfUpdate(BaseModel):
    phone: str | None = None
    city: str | None = None
    address: str | None = None
    date_of_birth: date | None = None


class ClientOut(ORM):
    id: int
    email: str
    first_name: str
    last_name: str
    phone: str | None
    country: str | None
    city: str | None
    address: str | None
    date_of_birth: date | None
    status: ClientStatus
    kyc_status: KycStatus
    sales_staff_id: int | None
    account_manager_id: int | None
    referral_code: str
    referred_by_id: int | None
    lead_source: str | None
    utm_campaign: str | None
    totp_enabled: bool
    created_at: datetime


class AssignIn(BaseModel):
    sales_staff_id: int | None = None
    account_manager_id: int | None = None


class NoteIn(BaseModel):
    body: str = Field(min_length=1)


class NoteOut(ORM):
    id: int
    staff_id: int
    body: str
    created_at: datetime


class KycDocOut(ORM):
    id: int
    client_id: int
    doc_type: str
    original_name: str
    status: KycStatus
    reject_reason: str | None
    reviewed_at: datetime | None
    created_at: datetime


class KycReviewIn(BaseModel):
    approve: bool
    reason: str | None = None


class KycStatusIn(BaseModel):
    status: KycStatus


# ---------------------------------------------------------------- accounts
class AccountGroupIn(BaseModel):
    name: str
    mt5_group: str
    is_demo: bool = False
    currency: str = "USD"
    leverage_options: list[int] = [100, 200, 500]
    default_leverage: int = 100
    min_deposit: Decimal = Decimal("0")
    demo_initial_balance: Decimal = Decimal("10000")
    spread_markup_points: int = 0
    max_accounts_per_client: int = 5
    enabled: bool = True
    description: str = ""
    server_id: int
    margin_call: Decimal | None = None  # None: take the MT5 group's value
    stop_out: Decimal | None = None
    mt5_template: str | None = None  # create mt5_group on MT5 as a copy of this group


class AccountGroupUpdate(BaseModel):
    mt5_group: str | None = None
    margin_call: Decimal | None = None
    stop_out: Decimal | None = None
    demo_initial_balance: Decimal | None = None
    leverage_options: list[int] | None = None
    default_leverage: int | None = None
    min_deposit: Decimal | None = None
    spread_markup_points: int | None = None
    max_accounts_per_client: int | None = None
    enabled: bool | None = None
    description: str | None = None


class AccountGroupOut(ORM):
    id: int
    name: str
    mt5_group: str
    is_demo: bool
    currency: str
    leverage_options: list[int]
    default_leverage: int
    min_deposit: Decimal
    spread_markup_points: int
    max_accounts_per_client: int
    enabled: bool
    description: str
    server_id: int
    margin_call: Decimal | None
    stop_out: Decimal | None
    on_server: bool


class OpenAccountIn(BaseModel):
    account_group_id: int
    leverage: int | None = None


class StaffOpenAccountIn(OpenAccountIn):
    client_id: int


class TradingAccountOut(ORM):
    id: int
    login: int
    client_id: int
    account_group_id: int | None
    group: str
    leverage: int
    currency: str
    is_demo: bool
    status: AccountStatus
    trading_enabled: bool
    purpose: AccountPurpose
    balance: Decimal
    credit: Decimal
    equity: Decimal
    margin: Decimal
    margin_free: Decimal
    margin_level: Decimal
    last_synced_at: datetime | None


class OpenAccountOut(BaseModel):
    account: TradingAccountOut
    master_password: str
    investor_password: str


class LeverageIn(BaseModel):
    leverage: int


class PasswordResetIn(BaseModel):
    investor: bool = False
    new_password: str | None = None


class PasswordOut(BaseModel):
    login: int
    password: str


class EnabledIn(BaseModel):
    enabled: bool


class StatusIn(BaseModel):
    status: AccountStatus


class TransferIn(BaseModel):
    login: int
    amount: PositiveMoney


class TransferOut(BaseModel):
    wallet_tx_id: int
    mt5_deal: int


# ---------------------------------------------------------------- finance
class WalletOut(ORM):
    id: int
    client_id: int
    currency: str
    balance: Decimal


class WalletTxOut(ORM):
    id: int
    created_at: datetime
    kind: WalletTxKind
    amount: Decimal
    balance_after: Decimal
    ref_type: str | None
    ref_id: str | None
    comment: str


class PaymentIn(BaseModel):
    kind: PaymentKind
    amount: PositiveMoney
    method: str = "manual"
    trading_login: int | None = None
    promo_code: str | None = None
    external_ref: str | None = None
    details: str = ""


class StaffPaymentIn(PaymentIn):
    client_id: int


class PaymentOut(ORM):
    id: int
    client_id: int
    kind: PaymentKind
    amount: Decimal
    currency: str
    method: str
    trading_login: int | None
    status: PaymentStatus
    promo_code: str | None
    external_ref: str | None
    created_by_staff_id: int | None
    reviewed_by_staff_id: int | None
    reviewed_at: datetime | None
    review_comment: str | None
    mt5_deal_ticket: int | None
    created_at: datetime


class ReviewIn(BaseModel):
    comment: str | None = None


# ---------------------------------------------------------------- trading
class DealOut(ORM):
    ticket: int
    login: int
    time: datetime
    action: int
    entry: int
    symbol: str
    volume: Decimal
    price: Decimal
    profit: Decimal
    commission: Decimal
    swap: Decimal
    position_id: int
    comment: str


class PositionOut(ORM):
    position_id: int
    login: int
    symbol: str
    action: int
    volume: Decimal
    price_open: Decimal
    price_current: Decimal
    sl: Decimal
    tp: Decimal
    profit: Decimal
    swap: Decimal
    time_create: datetime
    is_open: bool
    closed_at: datetime | None


class SyncIn(BaseModel):
    logins: list[int] | None = None


class SyncStateOut(ORM):
    login: int
    last_deal_time: datetime | None
    last_deal_sync_at: datetime | None
    last_position_sync_at: datetime | None
    last_error: str | None


# ---------------------------------------------------------------- copy trading
class StrategyIn(BaseModel):
    owner_client_id: int
    master_login: int
    name: str
    description: str = ""
    is_public: bool = True
    performance_fee_pct: Decimal = Field(default=Decimal("20"), ge=0, le=100)
    min_investment: Decimal = Decimal("100")


class ClientStrategyIn(BaseModel):
    master_login: int
    name: str
    description: str = ""
    performance_fee_pct: Decimal = Field(default=Decimal("20"), ge=0, le=50)
    min_investment: Decimal = Decimal("100")


class StrategyOut(ORM):
    id: int
    name: str
    description: str
    owner_client_id: int
    master_login: int
    status: StrategyStatus
    is_public: bool
    performance_fee_pct: Decimal
    min_investment: Decimal


class StrategyStatusIn(BaseModel):
    status: StrategyStatus


class SubscribeIn(BaseModel):
    follower_login: int
    allocation_mode: AllocationMode = AllocationMode.equity_ratio
    ratio: Decimal = Field(default=Decimal("1"), gt=0)
    max_lot: Decimal | None = Field(default=None, gt=0)


class StaffSubscribeIn(SubscribeIn):
    follower_client_id: int


class SubscriptionOut(ORM):
    id: int
    strategy_id: int
    follower_client_id: int
    follower_login: int
    allocation_mode: AllocationMode
    ratio: Decimal
    max_lot: Decimal | None
    status: SubscriptionStatus
    high_water_mark: Decimal
    fees_paid: Decimal


class UnsubscribeIn(BaseModel):
    close_positions: bool = True


class CopyExecutionOut(ORM):
    id: int
    created_at: datetime
    subscription_id: int
    master_deal_ticket: int
    follower_login: int
    action: str
    symbol: str
    volume: Decimal
    status: str
    retcode: int | None
    message: str
    follower_deal_ticket: int | None


class FundIn(BaseModel):
    manager_client_id: int
    master_login: int
    name: str
    description: str = ""
    performance_fee_pct: Decimal = Field(default=Decimal("20"), ge=0, le=100)
    management_fee_pct_annual: Decimal = Field(default=Decimal("0"), ge=0, le=10)
    min_investment: Decimal = Decimal("100")


class FundOut(ORM):
    id: int
    name: str
    description: str
    manager_client_id: int
    master_login: int
    status: StrategyStatus
    performance_fee_pct: Decimal
    management_fee_pct_annual: Decimal
    min_investment: Decimal
    total_units: Decimal
    last_nav: Decimal


class InvestIn(BaseModel):
    amount: PositiveMoney


class StaffInvestIn(InvestIn):
    client_id: int


class RedeemIn(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0)
    units: Decimal | None = Field(default=None, gt=0)


class PammTxOut(ORM):
    id: int
    created_at: datetime
    fund_id: int
    investment_id: int
    kind: PammTxKind
    amount: Decimal
    units: Decimal
    nav: Decimal


class NavOut(ORM):
    time: datetime
    equity: Decimal
    total_units: Decimal
    nav: Decimal


# ---------------------------------------------------------------- marketing
class RuleIn(BaseModel):
    symbol_pattern: str = "*"
    account_group_id: int | None = None
    kind: RuleKind
    value: Decimal
    priority: int = 100


class RuleOut(ORM, RuleIn):
    id: int


class PlanIn(BaseModel):
    name: str
    level_shares: list[float] = [1.0]
    trigger: str = "close"
    min_hold_seconds: int = 0
    rules: list[RuleIn]


class PlanOut(ORM):
    id: int
    name: str
    level_shares: list[float]
    trigger: str
    min_hold_seconds: int
    rules: list[RuleOut]


class IBIn(BaseModel):
    client_id: int
    plan_id: int | None = None
    parent_ib_id: int | None = None


class IBUpdate(BaseModel):
    plan_id: int | None = None
    parent_ib_id: int | None = None
    status: IBStatus | None = None


class IBOut(ORM):
    id: int
    client_id: int
    parent_ib_id: int | None
    plan_id: int | None
    status: IBStatus


class CommissionOut(ORM):
    id: int
    created_at: datetime
    deal_ticket: int
    ib_id: int
    level: int
    trader_client_id: int
    login: int
    symbol: str
    volume: Decimal
    amount: Decimal
    status: str
    paid_at: datetime | None


class CampaignIn(BaseModel):
    name: str
    code: str = Field(min_length=3, max_length=32)
    bonus_pct: Decimal = Field(default=Decimal("0"), ge=0, le=500)
    max_bonus: Decimal = Decimal("0")
    min_deposit: Decimal = Decimal("0")
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    max_redemptions: int | None = None
    once_per_client: bool = True
    active: bool = True


class CampaignOut(ORM, CampaignIn):
    id: int


class RecalcIn(BaseModel):
    date_from: datetime
    date_to: datetime


# ---------------------------------------------------------------- security
class AuditOut(ORM):
    id: int
    created_at: datetime
    actor_type: str
    actor_id: int | None
    action: str
    entity: str | None
    entity_id: str | None
    details: dict
    ip: str | None


class LoginAttemptOut(ORM):
    id: int
    created_at: datetime
    principal_type: str
    email: str
    ip: str | None
    success: bool
    reason: str | None
