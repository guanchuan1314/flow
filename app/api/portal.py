"""Client-facing API (client portal / mobile app). Everything is scoped to the logged-in client."""

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.deps import current_client, gateway
from app.core.errors import Forbidden, ServiceError, get_or_404
from app.db import get_db
from app.models import (
    AccountGroup, Client, CopyStrategy, CopySubscription, IBCommission, IBProfile, KycDocument, PammFund,
    PaymentKind, PaymentMethod, PaymentRequest, StrategyStatus, TradingAccount, WalletTransaction,
)
from app.mt5 import MT5Gateway, for_account_group
from app.schemas import (
    AccountGroupOut, ClientOut, ClientSelfUpdate, ClientStrategyIn, CommissionOut, FundOut, InvestIn, KycDocOut,
    LeverageIn, OpenAccountIn, OpenAccountOut, PammTxOut, PasswordOut, PasswordResetIn, PaymentOut, PositiveMoney,
    RedeemIn, StrategyOut, SubscribeIn, SubscriptionOut, TradingAccountOut, TransferIn, TransferOut,
    UnsubscribeIn, WalletOut, WalletTxOut,
)
from app.services import accounts, clients, copy_trading, kyc_methods, pamm, payment_methods, payments, wallet

router = APIRouter(prefix="/portal", tags=["client portal"])


class PortalPaymentIn(BaseModel):
    """A client's deposit or withdrawal request, made with one of the enabled payment methods."""

    kind: PaymentKind
    amount: PositiveMoney
    method_id: int
    trading_login: int | None = None
    promo_code: str | None = None
    external_ref: str | None = None
    details: str = ""


@router.get("/me", response_model=ClientOut)
def me(client: Client = Depends(current_client)):
    return client


@router.patch("/me", response_model=ClientOut)
def update_me(body: ClientSelfUpdate, client: Client = Depends(current_client), db: Session = Depends(get_db)):
    return clients.update_client(db, client, client, body.model_dump(exclude_unset=True))


# ---------------------------------------------------------------- KYC
@router.get("/kyc", response_model=list[KycDocOut])
def my_kyc(client: Client = Depends(current_client), db: Session = Depends(get_db)):
    return db.scalars(select(KycDocument).where(KycDocument.client_id == client.id)).all()


@router.post("/kyc", response_model=KycDocOut, status_code=201)
def upload_kyc(doc_type: str = Form(...), file: UploadFile = File(...),
               client: Client = Depends(current_client), db: Session = Depends(get_db)):
    kyc_methods.check_upload_allowed(db, client)
    content = kyc_methods.read_upload(file.file)
    return clients.upload_kyc_document(db, client, client, doc_type, file.filename or "upload", content)


# ---------------------------------------------------------------- accounts
@router.get("/account-groups", response_model=list[AccountGroupOut])
def account_groups(_: Client = Depends(current_client), db: Session = Depends(get_db)):
    return db.scalars(select(AccountGroup).where(AccountGroup.enabled.is_(True))).all()


@router.get("/accounts", response_model=list[TradingAccountOut])
def my_accounts(client: Client = Depends(current_client), db: Session = Depends(get_db)):
    return db.scalars(select(TradingAccount).where(TradingAccount.client_id == client.id)).all()


@router.post("/accounts", response_model=OpenAccountOut, status_code=201)
def open_account(body: OpenAccountIn, client: Client = Depends(current_client), db: Session = Depends(get_db),
                 gw: MT5Gateway = Depends(gateway)):
    return accounts.open_account(db, for_account_group(db, body.account_group_id), client, client,
                                 body.account_group_id, body.leverage)


@router.put("/accounts/{login}/leverage", response_model=TradingAccountOut)
def leverage(login: int, body: LeverageIn, client: Client = Depends(current_client), db: Session = Depends(get_db),
             gw: MT5Gateway = Depends(gateway)):
    return accounts.change_leverage(db, gw, client, accounts.get_client_account(db, client, login), body.leverage)


@router.post("/accounts/{login}/password", response_model=PasswordOut)
def reset_password(login: int, body: PasswordResetIn, client: Client = Depends(current_client),
                   db: Session = Depends(get_db), gw: MT5Gateway = Depends(gateway)):
    acc = accounts.get_client_account(db, client, login)
    return {"login": login, "password": accounts.reset_password(db, gw, client, acc, body.investor, body.new_password)}


# ---------------------------------------------------------------- wallet & money
@router.get("/wallet", response_model=WalletOut)
def my_wallet(client: Client = Depends(current_client), db: Session = Depends(get_db)):
    w = wallet.get_wallet(db, client.id)
    db.commit()
    return w


@router.get("/wallet/transactions", response_model=list[WalletTxOut])
def my_wallet_txs(limit: int = Query(100, le=1000), client: Client = Depends(current_client), db: Session = Depends(get_db)):
    w = wallet.get_wallet(db, client.id)
    return db.scalars(select(WalletTransaction).where(WalletTransaction.wallet_id == w.id)
                      .order_by(WalletTransaction.id.desc()).limit(limit)).all()


@router.post("/transfer/to-mt5", response_model=TransferOut)
def to_mt5(body: TransferIn, client: Client = Depends(current_client), db: Session = Depends(get_db),
           gw: MT5Gateway = Depends(gateway)):
    return accounts.transfer_to_mt5(db, gw, client, client, accounts.get_client_account(db, client, body.login), body.amount)


@router.post("/transfer/from-mt5", response_model=TransferOut)
def from_mt5(body: TransferIn, client: Client = Depends(current_client), db: Session = Depends(get_db),
             gw: MT5Gateway = Depends(gateway)):
    return accounts.transfer_from_mt5(db, gw, client, client, accounts.get_client_account(db, client, body.login), body.amount)


@router.get("/payments", response_model=list[PaymentOut])
def my_payments(client: Client = Depends(current_client), db: Session = Depends(get_db)):
    return db.scalars(select(PaymentRequest).where(PaymentRequest.client_id == client.id)
                      .order_by(PaymentRequest.id.desc())).all()


@router.post("/payments", response_model=PaymentOut, status_code=201)
def request_payment(body: PortalPaymentIn, client: Client = Depends(current_client), db: Session = Depends(get_db)):
    """Manual methods only: a custom (provider) deposit needs the redirect, so it goes through the portal."""
    m = db.get(PaymentMethod, body.method_id)
    if m is None or m not in payment_methods.methods_for(db, body.kind):
        raise ServiceError("Choose an available payment method")
    if m.is_custom and body.kind == PaymentKind.deposit:
        raise ServiceError(f"{m.name} deposits are made in the client portal")
    data = body.model_dump(exclude={"kind", "amount", "method_id"})
    return payments.create_request(db, client, client, body.kind, body.amount, payment_method=m, **data)


# ---------------------------------------------------------------- copy trading
@router.get("/copy/strategies", response_model=list[StrategyOut])
def public_strategies(_: Client = Depends(current_client), db: Session = Depends(get_db)):
    return db.scalars(select(CopyStrategy).where(CopyStrategy.is_public.is_(True),
                                                 CopyStrategy.status == StrategyStatus.active)).all()


@router.post("/copy/strategies", response_model=StrategyOut, status_code=201)
def become_master(body: ClientStrategyIn, client: Client = Depends(current_client), db: Session = Depends(get_db)):
    return copy_trading.create_strategy(db, client, client, body.master_login, body.name,
                                        **body.model_dump(exclude={"master_login", "name"}))


@router.get("/copy/subscriptions", response_model=list[SubscriptionOut])
def my_subscriptions(client: Client = Depends(current_client), db: Session = Depends(get_db)):
    return db.scalars(select(CopySubscription).where(CopySubscription.follower_client_id == client.id)).all()


@router.post("/copy/strategies/{strategy_id}/follow", response_model=SubscriptionOut, status_code=201)
def follow(strategy_id: int, body: SubscribeIn, client: Client = Depends(current_client),
           db: Session = Depends(get_db), gw: MT5Gateway = Depends(gateway)):
    s = get_or_404(db, CopyStrategy, strategy_id)
    if not s.is_public:
        raise Forbidden("Strategy is private")
    return copy_trading.subscribe(db, gw, client, client, s, body.follower_login, body.allocation_mode, body.ratio, body.max_lot)


@router.post("/copy/subscriptions/{sub_id}/stop", response_model=SubscriptionOut)
def unfollow(sub_id: int, body: UnsubscribeIn, client: Client = Depends(current_client),
             db: Session = Depends(get_db), gw: MT5Gateway = Depends(gateway)):
    sub = get_or_404(db, CopySubscription, sub_id)
    if sub.follower_client_id != client.id:
        raise Forbidden("Not your subscription")
    return copy_trading.unsubscribe(db, gw, client, sub, body.close_positions)


# ---------------------------------------------------------------- PAMM
@router.get("/pamm/funds", response_model=list[FundOut])
def funds(_: Client = Depends(current_client), db: Session = Depends(get_db)):
    return db.scalars(select(PammFund).where(PammFund.status == StrategyStatus.active)).all()


@router.post("/pamm/funds/{fund_id}/invest", response_model=PammTxOut)
def invest(fund_id: int, body: InvestIn, client: Client = Depends(current_client), db: Session = Depends(get_db),
           gw: MT5Gateway = Depends(gateway)):
    return pamm.invest(db, gw, client, get_or_404(db, PammFund, fund_id), client, body.amount)


@router.post("/pamm/funds/{fund_id}/withdraw", response_model=PammTxOut)
def withdraw(fund_id: int, body: RedeemIn, client: Client = Depends(current_client), db: Session = Depends(get_db),
             gw: MT5Gateway = Depends(gateway)):
    return pamm.withdraw(db, gw, client, get_or_404(db, PammFund, fund_id), client, body.amount, body.units)


@router.get("/pamm/funds/{fund_id}/statement")
def statement(fund_id: int, client: Client = Depends(current_client), db: Session = Depends(get_db),
              gw: MT5Gateway = Depends(gateway)):
    return pamm.investor_statement(db, gw, get_or_404(db, PammFund, fund_id), client.id)


# ---------------------------------------------------------------- referral / IB
@router.get("/referral")
def referral(client: Client = Depends(current_client), db: Session = Depends(get_db)):
    referred = db.scalar(select(func.count()).select_from(Client).where(Client.referred_by_id == client.id))
    ib = db.scalar(select(IBProfile).where(IBProfile.client_id == client.id))
    out = {"referral_code": client.referral_code, "referral_link": f"/register?ref={client.referral_code}",
           "referred_clients": referred, "is_ib": ib is not None}
    if ib:
        totals = dict(db.execute(select(IBCommission.status, func.sum(IBCommission.amount))
                                 .where(IBCommission.ib_id == ib.id).group_by(IBCommission.status)).all())
        out["commissions"] = {"pending": totals.get("pending", 0), "paid": totals.get("paid", 0)}
    return out


@router.get("/referral/commissions", response_model=list[CommissionOut])
def my_commissions(limit: int = Query(200, le=2000), client: Client = Depends(current_client), db: Session = Depends(get_db)):
    ib = db.scalar(select(IBProfile).where(IBProfile.client_id == client.id))
    if ib is None:
        return []
    return db.scalars(select(IBCommission).where(IBCommission.ib_id == ib.id)
                      .order_by(IBCommission.id.desc()).limit(limit)).all()
