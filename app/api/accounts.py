from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import gateway, require
from app.core.errors import get_or_404
from app.db import get_db
from app.models import AccountStatus, AccountGroup, Client, Staff, TradingAccount
from app.mt5 import MT5Gateway, for_account_group
from app.schemas import (
    AccountGroupIn, AccountGroupOut, AccountGroupUpdate, EnabledIn, LeverageIn, OpenAccountOut, PasswordOut,
    PasswordResetIn, StaffOpenAccountIn, StatusIn, TradingAccountOut, TransferIn, TransferOut,
)
from app.services import accounts as svc
from app.services.clients import ensure_visible, visible_account, visible_client_ids

router = APIRouter(tags=["mt5 accounts"])


# ---------------------------------------------------------------- account groups
@router.get("/account-groups", response_model=list[AccountGroupOut])
def list_groups(db: Session = Depends(get_db), _: Staff = Depends(require("accounts.view"))):
    return db.scalars(select(AccountGroup).order_by(AccountGroup.id)).all()


@router.post("/account-groups", response_model=AccountGroupOut, status_code=201)
def create_group(body: AccountGroupIn, db: Session = Depends(get_db), me: Staff = Depends(require("account_groups.manage"))):
    return svc.create_account_group(db, me, **body.model_dump())


@router.patch("/account-groups/{group_id}", response_model=AccountGroupOut)
def update_group(group_id: int, body: AccountGroupUpdate, db: Session = Depends(get_db),
                me: Staff = Depends(require("account_groups.manage"))):
    return svc.update_account_group(db, me, get_or_404(db, AccountGroup, group_id), body.model_dump(exclude_unset=True))


@router.delete("/account-groups/{group_id}", status_code=204)
def delete_group(group_id: int, db: Session = Depends(get_db), me: Staff = Depends(require("account_groups.manage"))):
    svc.delete_account_group(db, me, get_or_404(db, AccountGroup, group_id))


# ---------------------------------------------------------------- accounts
@router.get("/accounts", response_model=list[TradingAccountOut])
def list_accounts(client_id: int | None = None, status: AccountStatus | None = None, is_demo: bool | None = None,
                  limit: int = Query(100, le=1000), offset: int = 0,
                  db: Session = Depends(get_db), me: Staff = Depends(require("accounts.view"))):
    q = select(TradingAccount).where(TradingAccount.client_id.in_(visible_client_ids(me)))
    if client_id is not None:
        q = q.where(TradingAccount.client_id == client_id)
    if status is not None:
        q = q.where(TradingAccount.status == status)
    if is_demo is not None:
        q = q.where(TradingAccount.is_demo.is_(is_demo))
    return db.scalars(q.order_by(TradingAccount.id.desc()).limit(limit).offset(offset)).all()


@router.post("/accounts", response_model=OpenAccountOut, status_code=201)
def open_account(body: StaffOpenAccountIn, db: Session = Depends(get_db), gw: MT5Gateway = Depends(gateway),
                 me: Staff = Depends(require("accounts.manage"))):
    client = ensure_visible(db, me, body.client_id)
    return svc.open_account(db, for_account_group(db, body.account_group_id), me, client, body.account_group_id,
                            body.leverage)


@router.get("/accounts/{login}", response_model=TradingAccountOut)
def get_account(login: int, db: Session = Depends(get_db), me: Staff = Depends(require("accounts.view"))):
    return visible_account(db, me, login)


@router.post("/accounts/{login}/refresh", response_model=TradingAccountOut)
def refresh(login: int, db: Session = Depends(get_db), gw: MT5Gateway = Depends(gateway),
            me: Staff = Depends(require("accounts.view"))):
    return svc.refresh_account(db, gw, visible_account(db, me, login))


@router.put("/accounts/{login}/leverage", response_model=TradingAccountOut)
def leverage(login: int, body: LeverageIn, db: Session = Depends(get_db), gw: MT5Gateway = Depends(gateway),
             me: Staff = Depends(require("accounts.manage"))):
    return svc.change_leverage(db, gw, me, visible_account(db, me, login), body.leverage)


@router.post("/accounts/{login}/password", response_model=PasswordOut)
def reset_password(login: int, body: PasswordResetIn, db: Session = Depends(get_db), gw: MT5Gateway = Depends(gateway),
                   me: Staff = Depends(require("accounts.manage"))):
    pwd = svc.reset_password(db, gw, me, visible_account(db, me, login), body.investor, body.new_password)
    return {"login": login, "password": pwd}


@router.put("/accounts/{login}/trading", response_model=TradingAccountOut)
def set_trading(login: int, body: EnabledIn, db: Session = Depends(get_db), gw: MT5Gateway = Depends(gateway),
                me: Staff = Depends(require("accounts.manage"))):
    return svc.set_trading_enabled(db, gw, me, visible_account(db, me, login), body.enabled)


@router.put("/accounts/{login}/status", response_model=TradingAccountOut)
def set_status(login: int, body: StatusIn, db: Session = Depends(get_db), gw: MT5Gateway = Depends(gateway),
               me: Staff = Depends(require("accounts.manage"))):
    return svc.set_status(db, gw, me, visible_account(db, me, login), body.status)


@router.post("/accounts/transfer/to-mt5", response_model=TransferOut)
def to_mt5(body: TransferIn, db: Session = Depends(get_db), gw: MT5Gateway = Depends(gateway),
           me: Staff = Depends(require("accounts.manage", "finance.create"))):
    acc = visible_account(db, me, body.login)
    return svc.transfer_to_mt5(db, gw, me, db.get(Client, acc.client_id), acc, body.amount)


@router.post("/accounts/transfer/from-mt5", response_model=TransferOut)
def from_mt5(body: TransferIn, db: Session = Depends(get_db), gw: MT5Gateway = Depends(gateway),
             me: Staff = Depends(require("accounts.manage", "finance.create"))):
    acc = visible_account(db, me, body.login)
    return svc.transfer_from_mt5(db, gw, me, db.get(Client, acc.client_id), acc, body.amount)
