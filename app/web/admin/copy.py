from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Forbidden, ServiceError, get_or_404
from app.db import get_db
from app.models import (
    AllocationMode, Client, CopyExecution, CopyStrategy, CopySubscription, PammFund, PammInvestment, PammNavSnapshot,
    PammTransaction, Staff, StrategyStatus,
)
from app.mt5 import MT5Error, routed
from app.services import clients, copy_trading, pamm
from app.web.common import attempt, opt_dec, opt_enum, opt_int, redirect, render, req_dec, staff_perm

router = APIRouter()


def _client(db: Session, me: Staff, client_id) -> Client:
    """The client, if the staff member may see them."""
    cid = opt_int(client_id)
    if cid is None:
        raise ServiceError("Client ID is required")
    return clients.ensure_visible(db, me, cid)


def _pamm_money(me: Staff) -> None:
    """Investing or redeeming on a client's behalf moves their money: it needs finance.approve too."""
    if not me.has_perm("finance.approve"):
        raise Forbidden("Investing or withdrawing for a client also needs the finance.approve permission")


# ---------------------------------------------------------------- MAM
@router.get("/copy")
def strategies(request: Request, me: Staff = Depends(staff_perm("copy.view")), db: Session = Depends(get_db)):
    rows = db.scalars(select(CopyStrategy).order_by(CopyStrategy.id.desc())).all()
    owners = {c.id: c for c in db.scalars(select(Client).where(Client.id.in_({s.owner_client_id for s in rows})))}
    counts = {}
    for sub in db.scalars(select(CopySubscription).where(CopySubscription.status != "stopped")):
        counts[sub.strategy_id] = counts.get(sub.strategy_id, 0) + 1
    return render(request, "admin/copy/strategies.html", rows=rows, owners=owners, counts=counts)


@router.post("/copy/new")
def create_strategy(request: Request, owner_client_id: str = Form(...), master_login: str = Form(...),
                    name: str = Form(...), description: str = Form(""), performance_fee_pct: str = Form("20"),
                    min_investment: str = Form("100"), is_public: str = Form(""),
                    me: Staff = Depends(staff_perm("copy.manage")), db: Session = Depends(get_db)):
    created = {}

    def run():
        fee = opt_dec(performance_fee_pct) or 0
        if not 0 <= fee <= 100:
            raise ServiceError("Performance fee must be between 0 and 100%")
        created["s"] = copy_trading.create_strategy(
            db, me, _client(db, me, owner_client_id), master_login, name.strip(), description=description.strip(),
            performance_fee_pct=fee, min_investment=opt_dec(min_investment) or 0, is_public=is_public == "on")
    resp = attempt(request, run, "Strategy created.", "/admin/copy")
    return redirect(f"/admin/copy/{created['s'].id}") if "s" in created else resp


@router.get("/copy/{strategy_id}")
def strategy_detail(request: Request, strategy_id: int, me: Staff = Depends(staff_perm("copy.view")),
                    db: Session = Depends(get_db)):
    s = get_or_404(db, CopyStrategy, strategy_id)
    subs = db.scalars(select(CopySubscription).where(CopySubscription.strategy_id == s.id)
                      .order_by(CopySubscription.id)).all()
    sub_ids = [x.id for x in subs]
    execs = db.scalars(select(CopyExecution).where(CopyExecution.subscription_id.in_(sub_ids))
                       .order_by(CopyExecution.id.desc()).limit(100)).all() if sub_ids else []
    clients = {c.id: c for c in db.scalars(select(Client).where(
        Client.id.in_({x.follower_client_id for x in subs} | {s.owner_client_id})))}
    return render(request, "admin/copy/strategy.html", s=s, subs=subs, execs=execs, clients=clients,
                  modes=list(AllocationMode), statuses=list(StrategyStatus))


@router.post("/copy/{strategy_id}/status")
def strategy_status(request: Request, strategy_id: int, status: str = Form(...),
                    me: Staff = Depends(staff_perm("copy.manage")), db: Session = Depends(get_db)):
    s = get_or_404(db, CopyStrategy, strategy_id)
    return attempt(request, lambda: copy_trading.set_strategy_status(db, me, s, opt_enum(StrategyStatus, status, "status")),
                   "Strategy status updated.", f"/admin/copy/{strategy_id}")


@router.post("/copy/{strategy_id}/subscribe")
def subscribe(request: Request, strategy_id: int, follower_client_id: str = Form(...), follower_login: str = Form(...),
              allocation_mode: str = Form("equity_ratio"), ratio: str = Form("1"), max_lot: str = Form(""),
              me: Staff = Depends(staff_perm("copy.manage")), db: Session = Depends(get_db)):
    s = get_or_404(db, CopyStrategy, strategy_id)

    def run():
        mode = opt_enum(AllocationMode, allocation_mode, "allocation mode")
        copy_trading.subscribe(db, routed(), me, _client(db, me, follower_client_id), s, follower_login,
                               mode, req_dec(ratio, "Ratio"), opt_dec(max_lot))
    return attempt(request, run, "Follower added.", f"/admin/copy/{strategy_id}")


@router.post("/copy/subscriptions/{sub_id}/stop")
def stop(request: Request, sub_id: int, close_positions: str = Form(""),
         me: Staff = Depends(staff_perm("copy.manage")), db: Session = Depends(get_db)):
    sub = get_or_404(db, CopySubscription, sub_id)

    def run():
        clients.ensure_visible(db, me, sub.follower_client_id)
        copy_trading.unsubscribe(db, routed(), me, sub, close_positions == "on")
    return attempt(request, run, "Subscription stopped.", f"/admin/copy/{sub.strategy_id}")


@router.post("/copy/subscriptions/{sub_id}/settle-fee")
def settle_fee(request: Request, sub_id: int, me: Staff = Depends(staff_perm("copy.manage")),
               db: Session = Depends(get_db)):
    sub = get_or_404(db, CopySubscription, sub_id)
    return attempt(request, lambda: copy_trading.settle_fee(db, routed(), sub),
                   "Performance fee settled: {result}", f"/admin/copy/{sub.strategy_id}")


# ---------------------------------------------------------------- PAMM
@router.get("/pamm")
def funds(request: Request, me: Staff = Depends(staff_perm("copy.view")), db: Session = Depends(get_db)):
    rows = db.scalars(select(PammFund).order_by(PammFund.id.desc())).all()
    managers = {c.id: c for c in db.scalars(select(Client).where(Client.id.in_({f.manager_client_id for f in rows})))}
    return render(request, "admin/copy/funds.html", rows=rows, managers=managers)


@router.post("/pamm/new")
def create_fund(request: Request, manager_client_id: str = Form(...), master_login: str = Form(...),
                name: str = Form(...), description: str = Form(""), performance_fee_pct: str = Form("20"),
                management_fee_pct_annual: str = Form("0"), min_investment: str = Form("100"),
                me: Staff = Depends(staff_perm("copy.manage")), db: Session = Depends(get_db)):
    created = {}

    def run():
        perf, mgmt = opt_dec(performance_fee_pct) or 0, opt_dec(management_fee_pct_annual) or 0
        if not 0 <= perf <= 100 or not 0 <= mgmt <= 10:
            raise ServiceError("Performance fee must be 0-100% and management fee 0-10% per year")
        created["f"] = pamm.create_fund(db, routed(), me, _client(db, me, manager_client_id), master_login,
                                        name.strip(), description=description.strip(), performance_fee_pct=perf,
                                        management_fee_pct_annual=mgmt, min_investment=opt_dec(min_investment) or 0)
    resp = attempt(request, run, "PAMM fund created.", "/admin/pamm")
    return redirect(f"/admin/pamm/{created['f'].id}") if "f" in created else resp


@router.get("/pamm/{fund_id}")
def fund_detail(request: Request, fund_id: int, me: Staff = Depends(staff_perm("copy.view")),
                db: Session = Depends(get_db)):
    fund = get_or_404(db, PammFund, fund_id)
    gw = routed()
    try:
        equity, nav = pamm.current_nav(gw, fund)
        live_error = None
    except MT5Error as exc:
        equity, nav, live_error = None, fund.last_nav, str(exc)
    invs = db.scalars(select(PammInvestment).where(PammInvestment.fund_id == fund.id)
                      .order_by(PammInvestment.units.desc())).all()
    clients = {c.id: c for c in db.scalars(select(Client).where(Client.id.in_({i.client_id for i in invs} | {fund.manager_client_id})))}
    txs = db.scalars(select(PammTransaction).where(PammTransaction.fund_id == fund.id)
                     .order_by(PammTransaction.id.desc()).limit(100)).all()
    inv_clients = {i.id: i.client_id for i in invs}
    navs = db.scalars(select(PammNavSnapshot).where(PammNavSnapshot.fund_id == fund.id)
                      .order_by(PammNavSnapshot.time.desc()).limit(30)).all()
    return render(request, "admin/copy/fund.html", f=fund, equity=equity, nav=nav, live_error=live_error, invs=invs,
                  clients=clients, txs=txs, inv_clients=inv_clients, navs=navs, statuses=list(StrategyStatus))


@router.post("/pamm/{fund_id}/status")
def fund_status(request: Request, fund_id: int, status: str = Form(...),
                me: Staff = Depends(staff_perm("copy.manage")), db: Session = Depends(get_db)):
    fund = get_or_404(db, PammFund, fund_id)
    return attempt(request, lambda: pamm.set_fund_status(db, me, fund, opt_enum(StrategyStatus, status, "status")), "Fund status updated.",
                   f"/admin/pamm/{fund_id}")


@router.post("/pamm/{fund_id}/invest")
def invest(request: Request, fund_id: int, client_id: str = Form(...), amount: str = Form(...),
           me: Staff = Depends(staff_perm("copy.manage")), db: Session = Depends(get_db)):
    fund = get_or_404(db, PammFund, fund_id)

    def run():
        _pamm_money(me)
        return pamm.invest(db, routed(), me, fund, _client(db, me, client_id), req_dec(amount))
    return attempt(request, run, "Investment booked from the client's wallet.", f"/admin/pamm/{fund_id}")


@router.post("/pamm/{fund_id}/withdraw")
def withdraw(request: Request, fund_id: int, client_id: str = Form(...), amount: str = Form(""), all_units: str = Form(""),
             me: Staff = Depends(staff_perm("copy.manage")), db: Session = Depends(get_db)):
    fund = get_or_404(db, PammFund, fund_id)

    def run():
        _pamm_money(me)
        client = _client(db, me, client_id)
        if all_units == "on":
            inv = db.scalar(select(PammInvestment).where(PammInvestment.fund_id == fund.id,
                                                         PammInvestment.client_id == client.id))
            if inv is None:
                raise ServiceError("Client has no investment in this fund")
            return pamm.withdraw(db, routed(), me, fund, client, units=inv.units)
        return pamm.withdraw(db, routed(), me, fund, client, amount=req_dec(amount))
    return attempt(request, run, "Withdrawal paid to the client's wallet.", f"/admin/pamm/{fund_id}")


@router.post("/pamm/{fund_id}/settle-fees")
def settle(request: Request, fund_id: int, me: Staff = Depends(staff_perm("copy.manage")), db: Session = Depends(get_db)):
    fund = get_or_404(db, PammFund, fund_id)
    return attempt(request, lambda: pamm.settle_fees(db, routed(), fund),
                   "Fees settled at NAV {result[nav]}.", f"/admin/pamm/{fund_id}")


@router.post("/pamm/{fund_id}/snapshot")
def snapshot(request: Request, fund_id: int, me: Staff = Depends(staff_perm("copy.manage")), db: Session = Depends(get_db)):
    fund = get_or_404(db, PammFund, fund_id)
    return attempt(request, lambda: pamm.snapshot(db, routed(), fund), "NAV snapshot recorded.",
                   f"/admin/pamm/{fund_id}")
