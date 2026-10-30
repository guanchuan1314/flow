from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import gateway, require
from app.core.errors import get_or_404
from app.db import get_db
from app.models import (
    CopyExecution, CopyStrategy, CopySubscription, PammFund, PammInvestment, PammNavSnapshot,
    PammTransaction, Staff,
)
from app.mt5 import MT5Gateway
from app.schemas import (
    CopyExecutionOut, FundIn, FundOut, NavOut, PammTxOut, RedeemIn, StaffInvestIn, StaffSubscribeIn,
    StrategyIn, StrategyOut, StrategyStatusIn, SubscriptionOut, UnsubscribeIn,
)
from app.services import clients, copy_trading, pamm

router = APIRouter(tags=["copy trading"])
# Moving a client's money in or out of a PAMM fund on their behalf is a finance decision too.
PAMM_MONEY = ("copy.manage", "finance.approve")


# ---------------------------------------------------------------- MAM / copy strategies
@router.get("/copy/strategies", response_model=list[StrategyOut])
def list_strategies(db: Session = Depends(get_db), _: Staff = Depends(require("copy.view"))):
    return db.scalars(select(CopyStrategy).order_by(CopyStrategy.id)).all()


@router.post("/copy/strategies", response_model=StrategyOut, status_code=201)
def create_strategy(body: StrategyIn, db: Session = Depends(get_db), me: Staff = Depends(require("copy.manage"))):
    owner = clients.ensure_visible(db, me, body.owner_client_id)
    return copy_trading.create_strategy(db, me, owner, body.master_login, body.name,
                                        **body.model_dump(exclude={"owner_client_id", "master_login", "name"}))


@router.put("/copy/strategies/{strategy_id}/status", response_model=StrategyOut)
def strategy_status(strategy_id: int, body: StrategyStatusIn, db: Session = Depends(get_db),
                    me: Staff = Depends(require("copy.manage"))):
    return copy_trading.set_strategy_status(db, me, get_or_404(db, CopyStrategy, strategy_id), body.status)


@router.get("/copy/strategies/{strategy_id}/subscriptions", response_model=list[SubscriptionOut])
def list_subscriptions(strategy_id: int, db: Session = Depends(get_db), _: Staff = Depends(require("copy.view"))):
    return db.scalars(select(CopySubscription).where(CopySubscription.strategy_id == strategy_id)).all()


@router.post("/copy/strategies/{strategy_id}/subscriptions", response_model=SubscriptionOut, status_code=201)
def subscribe(strategy_id: int, body: StaffSubscribeIn, db: Session = Depends(get_db),
              gw: MT5Gateway = Depends(gateway), me: Staff = Depends(require("copy.manage"))):
    s = get_or_404(db, CopyStrategy, strategy_id)
    follower = clients.ensure_visible(db, me, body.follower_client_id)
    return copy_trading.subscribe(db, gw, me, follower, s, body.follower_login, body.allocation_mode, body.ratio, body.max_lot)


@router.post("/copy/subscriptions/{sub_id}/stop", response_model=SubscriptionOut)
def stop_subscription(sub_id: int, body: UnsubscribeIn, db: Session = Depends(get_db),
                      gw: MT5Gateway = Depends(gateway), me: Staff = Depends(require("copy.manage"))):
    sub = get_or_404(db, CopySubscription, sub_id)
    clients.ensure_visible(db, me, sub.follower_client_id)
    return copy_trading.unsubscribe(db, gw, me, sub, body.close_positions)


@router.post("/copy/subscriptions/{sub_id}/settle-fee")
def settle_fee(sub_id: int, db: Session = Depends(get_db), gw: MT5Gateway = Depends(gateway),
               _: Staff = Depends(require("copy.manage"))):
    return {"fee": copy_trading.settle_fee(db, gw, get_or_404(db, CopySubscription, sub_id))}


@router.get("/copy/executions", response_model=list[CopyExecutionOut])
def executions(subscription_id: int | None = None, status: str | None = None, limit: int = Query(200, le=2000),
               db: Session = Depends(get_db), _: Staff = Depends(require("copy.view"))):
    q = select(CopyExecution)
    if subscription_id:
        q = q.where(CopyExecution.subscription_id == subscription_id)
    if status:
        q = q.where(CopyExecution.status == status)
    return db.scalars(q.order_by(CopyExecution.id.desc()).limit(limit)).all()


# ---------------------------------------------------------------- PAMM
@router.get("/pamm/funds", response_model=list[FundOut])
def list_funds(db: Session = Depends(get_db), _: Staff = Depends(require("copy.view"))):
    return db.scalars(select(PammFund).order_by(PammFund.id)).all()


@router.post("/pamm/funds", response_model=FundOut, status_code=201)
def create_fund(body: FundIn, db: Session = Depends(get_db), gw: MT5Gateway = Depends(gateway),
                me: Staff = Depends(require("copy.manage"))):
    manager = clients.ensure_visible(db, me, body.manager_client_id)
    return pamm.create_fund(db, gw, me, manager, body.master_login, body.name,
                            **body.model_dump(exclude={"manager_client_id", "master_login", "name"}))


@router.put("/pamm/funds/{fund_id}/status", response_model=FundOut)
def fund_status(fund_id: int, body: StrategyStatusIn, db: Session = Depends(get_db),
                me: Staff = Depends(require("copy.manage"))):
    return pamm.set_fund_status(db, me, get_or_404(db, PammFund, fund_id), body.status)


@router.get("/pamm/funds/{fund_id}/investors")
def fund_investors(fund_id: int, db: Session = Depends(get_db), gw: MT5Gateway = Depends(gateway),
                   _: Staff = Depends(require("copy.view"))):
    fund = get_or_404(db, PammFund, fund_id)
    invs = db.scalars(select(PammInvestment).where(PammInvestment.fund_id == fund_id))
    return [{**pamm.investor_statement(db, gw, fund, i.client_id), "client_id": i.client_id, "is_manager": i.is_manager}
            for i in invs]


@router.post("/pamm/funds/{fund_id}/invest", response_model=PammTxOut)
def invest(fund_id: int, body: StaffInvestIn, db: Session = Depends(get_db), gw: MT5Gateway = Depends(gateway),
           me: Staff = Depends(require(*PAMM_MONEY))):
    client = clients.ensure_visible(db, me, body.client_id)
    return pamm.invest(db, gw, me, get_or_404(db, PammFund, fund_id), client, body.amount)


@router.post("/pamm/funds/{fund_id}/withdraw/{client_id}", response_model=PammTxOut)
def withdraw(fund_id: int, client_id: int, body: RedeemIn, db: Session = Depends(get_db),
             gw: MT5Gateway = Depends(gateway), me: Staff = Depends(require(*PAMM_MONEY))):
    client = clients.ensure_visible(db, me, client_id)
    return pamm.withdraw(db, gw, me, get_or_404(db, PammFund, fund_id), client, body.amount, body.units)


@router.post("/pamm/funds/{fund_id}/settle-fees")
def settle_fund_fees(fund_id: int, db: Session = Depends(get_db), gw: MT5Gateway = Depends(gateway),
                     _: Staff = Depends(require("copy.manage"))):
    return pamm.settle_fees(db, gw, get_or_404(db, PammFund, fund_id))


@router.get("/pamm/funds/{fund_id}/transactions", response_model=list[PammTxOut])
def fund_transactions(fund_id: int, limit: int = Query(200, le=2000), db: Session = Depends(get_db),
                      _: Staff = Depends(require("copy.view"))):
    return db.scalars(select(PammTransaction).where(PammTransaction.fund_id == fund_id)
                      .order_by(PammTransaction.id.desc()).limit(limit)).all()


@router.get("/pamm/funds/{fund_id}/nav", response_model=list[NavOut])
def fund_nav(fund_id: int, limit: int = Query(500, le=5000), db: Session = Depends(get_db),
             _: Staff = Depends(require("copy.view"))):
    return db.scalars(select(PammNavSnapshot).where(PammNavSnapshot.fund_id == fund_id)
                      .order_by(PammNavSnapshot.time.desc()).limit(limit)).all()
