"""Client portal (server-rendered)."""

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.deps import client_ip
from app.core.errors import Forbidden, ServiceError, get_or_404
from app.core.ratelimit import check_auth_rate
from app.core.security import totp_uri
from app.db import get_db
from app.models import (
    Campaign, Deal,
    AccountPurpose, AccountStatus, AccountGroup, AllocationMode, Client, CopyStrategy, CopySubscription, IBCommission,
    IBProfile, KycDocument, KycSession, PammFund, PammInvestment, PaymentKind, PaymentMethod, PaymentRequest,
    PaymentStatus, StrategyStatus, TradingAccount,
    WalletTransaction,
)
from app.mt5 import MT5Error, for_account_group, routed
from app.services import (
    branding,
    accounts, auth, client_sync, clients, copy_trading, kyc_methods, market, pamm, payment_methods, payments, wallet,
)
from app.web import login_flow
from app.web.common import (
    CLIENT_KEY, attempt, csrf_protect, flash, logout_session, opt_dec, opt_enum, opt_int, qr_data_uri, redirect,
    refresh_fingerprint, render, req_dec, safe_next, web_client,
)

router = APIRouter(prefix="/portal", include_in_schema=False, dependencies=[Depends(csrf_protect)])


def _live_accounts(db: Session, client: Client) -> list[TradingAccount]:
    return db.scalars(select(TradingAccount).where(
        TradingAccount.client_id == client.id, TradingAccount.is_demo.is_(False),
        TradingAccount.status == AccountStatus.active, TradingAccount.purpose == AccountPurpose.trading)).all()


# ---------------------------------------------------------------- auth
@router.get("/login")
def login_page(request: Request, next: str | None = None):
    if request.session.get(f"{CLIENT_KEY}_id"):
        return redirect(safe_next(next, "/portal"))
    return render(request, "portal/login.html", next=next)


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str | None = Form(None),
          db: Session = Depends(get_db)):
    try:
        _, needs_totp = login_flow.password_step(request, db, CLIENT_KEY, auth.authenticate_client, email, password)
    except ServiceError as exc:
        return render(request, "portal/login.html", status_code=401 if exc.status_code < 403 else exc.status_code,
                      error=exc.message, email=email, next=next)
    if needs_totp:
        return render(request, "portal/login.html", need_totp=True, next=next)
    return redirect(safe_next(next, "/portal"))


@router.post("/login/2fa")
def login_2fa(request: Request, totp_code: str = Form(...), next: str | None = Form(None), db: Session = Depends(get_db)):
    try:
        login_flow.totp_step(request, db, CLIENT_KEY, Client, totp_code)
    except ServiceError as exc:
        return render(request, "portal/login.html", status_code=401, error=exc.message, next=next,
                      need_totp=login_flow.has_pending(request, CLIENT_KEY))
    return redirect(safe_next(next, "/portal"))


@router.post("/logout")
def logout(request: Request):
    logout_session(request, CLIENT_KEY)
    flash(request, "You have been signed out.", "info")
    return redirect("/portal/login")


@router.get("/register")
def register_page(request: Request, ref: str = "", utm_source: str = "", utm_medium: str = "", utm_campaign: str = ""):
    return render(request, "portal/register.html", ref=ref, utm=dict(source=utm_source, medium=utm_medium, campaign=utm_campaign))


@router.post("/register")
def register(request: Request, email: str = Form(...), password: str = Form(...), first_name: str = Form(...),
             last_name: str = Form(...), phone: str = Form(""), country: str = Form(""), ref: str = Form(""),
             utm_source: str = Form(""), utm_medium: str = Form(""), utm_campaign: str = Form(""),
             accept_terms: str = Form(""), db: Session = Depends(get_db)):
    ctx = dict(ref=ref, utm=dict(source=utm_source, medium=utm_medium, campaign=utm_campaign),
               form=dict(email=email, first_name=first_name, last_name=last_name, phone=phone, country=country))
    try:
        check_auth_rate("register", client_ip(request))
        if accept_terms != "on":
            raise ServiceError("Please accept the terms and conditions")
        if len(country.strip()) not in (0, 2):
            raise ServiceError("Choose your country")
        client = clients.register_client(db, None, email=email, first_name=first_name.strip(), last_name=last_name.strip(),
                                password=password, phone=phone or None, country=country or None,
                                referral_code=ref.strip() or None, utm_source=utm_source or None,
                                utm_medium=utm_medium or None, utm_campaign=utm_campaign or None, ip=client_ip(request))
    except ServiceError as exc:
        return render(request, "portal/register.html", status_code=400, error=exc.message, **ctx)
    login_flow.password_step(request, db, CLIENT_KEY, auth.authenticate_client, email, password)
    flash(request, "Welcome! Upload your documents to get verified and open a live account.")
    opened = client_sync.open_on_register(db, client)  # demo accounts marked "open at sign-up"
    if opened:
        return render(request, "portal/credentials.html", opened=opened, client=client)
    return redirect("/portal")


@router.get("/forgot-password")
def forgot_page(request: Request):
    return render(request, "portal/forgot.html")


@router.post("/forgot-password")
def forgot(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    try:
        check_auth_rate("forgot", client_ip(request))
        auth.request_password_reset(db, email, client_ip(request))
    except ServiceError as exc:
        return render(request, "portal/forgot.html", status_code=exc.status_code, error=exc.message)
    return render(request, "portal/forgot.html", sent=True)


@router.get("/reset-password")
def reset_page(request: Request, token: str = ""):
    return render(request, "portal/reset.html", token=token)


@router.post("/reset-password")
def reset(request: Request, token: str = Form(...), new_password: str = Form(...), db: Session = Depends(get_db)):
    try:
        check_auth_rate("reset", client_ip(request))
        auth.reset_password(db, token, new_password, client_ip(request))
    except ServiceError as exc:
        return render(request, "portal/reset.html", status_code=400, token=token, error=exc.message)
    flash(request, "Your password has been changed. Please sign in.")
    return redirect("/portal/login")


# ---------------------------------------------------------------- dashboard & profile
@router.get("")
def dashboard(request: Request, client: Client = Depends(web_client), db: Session = Depends(get_db)):
    accs = db.scalars(select(TradingAccount).where(TradingAccount.client_id == client.id,
                                                   TradingAccount.status != AccountStatus.archived)).all()
    w = wallet.get_wallet(db, client.id)
    db.commit()
    pending = db.scalars(select(PaymentRequest).where(PaymentRequest.client_id == client.id,
                                                      PaymentRequest.status == "pending")).all()
    funded = db.scalar(select(PaymentRequest.id).where(PaymentRequest.client_id == client.id,
                                                       PaymentRequest.kind == PaymentKind.deposit,
                                                       PaymentRequest.status == "approved")) is not None
    live = [a for a in accs if not a.is_demo]
    traded = db.scalar(select(Deal.ticket).where(tuple_(Deal.server_id, Deal.login).in_([(a.server_id, a.login) for a in live]
                                                                                        or [(0, 0)]),
                                                 Deal.action.in_((0, 1)))) is not None
    steps = [("Register", "Account created", True, "/portal/profile"),
             ("Verify", "Upload ID & address", client.kyc_status.value == "approved", "/portal/verification"),
             ("Fund", "Make a first deposit", funded, "/portal/wallet?tab=deposit"),
             ("Trade", "Place your first trade", traded, "/portal/platforms")]
    return render(request, "portal/dashboard.html", accounts=accs, wallet=w, pending=pending, steps=steps,
                  quotes=market.quotes()[:6], campaigns=[c for c in db.scalars(select(Campaign).where(Campaign.active.is_(True)))][:1],
                  total_equity=sum((a.equity for a in live), 0), total_live=len(live))


@router.get("/platforms")
def platforms(request: Request, client: Client = Depends(web_client), db: Session = Depends(get_db)):
    accs = db.scalars(select(TradingAccount).where(TradingAccount.client_id == client.id,
                                                   TradingAccount.status == AccountStatus.active)).all()
    return render(request, "portal/platforms.html", accounts=accs)


@router.get("/profile")
def profile(request: Request, client: Client = Depends(web_client)):
    return render(request, "portal/profile.html")


@router.post("/profile")
def update_profile(request: Request, phone: str = Form(""), city: str = Form(""), address: str = Form(""),
                   client: Client = Depends(web_client), db: Session = Depends(get_db)):
    return attempt(request, lambda: clients.update_client(db, client, client, {
        "phone": phone.strip() or None, "city": city.strip() or None, "address": address.strip() or None}),
        "Profile updated.", "/portal/profile")


@router.post("/profile/password")
def change_password(request: Request, current_password: str = Form(...), new_password: str = Form(...),
                    client: Client = Depends(web_client), db: Session = Depends(get_db)):
    def run():
        auth.change_password(db, client, current_password, new_password)
        refresh_fingerprint(request, CLIENT_KEY, client)
    return attempt(request, run, "Password changed.", "/portal/profile")


@router.post("/profile/2fa/start")
def start_2fa(request: Request, client: Client = Depends(web_client), db: Session = Depends(get_db)):
    try:
        data = auth.begin_totp_enrollment(db, client)
    except ServiceError as exc:
        flash(request, exc.message, "danger")
        return redirect("/portal/profile")
    uri = totp_uri(data["secret"], client.email, issuer=branding.current().app_name)
    return render(request, "portal/profile.html", enroll={"secret": data["secret"], "qr": qr_data_uri(uri)})


@router.post("/profile/2fa/confirm")
def confirm_2fa(request: Request, code: str = Form(...), client: Client = Depends(web_client), db: Session = Depends(get_db)):
    return attempt(request, lambda: auth.confirm_totp_enrollment(db, client, code), "Two-factor authentication enabled.",
                   "/portal/profile")


@router.post("/profile/2fa/disable")
def disable_2fa(request: Request, code: str = Form(...), client: Client = Depends(web_client), db: Session = Depends(get_db)):
    def run():
        auth.disable_totp(db, client, code)
        refresh_fingerprint(request, CLIENT_KEY, client)  # other sessions end; this one stays
    return attempt(request, run, "Two-factor authentication disabled.", "/portal/profile")


# ---------------------------------------------------------------- KYC
@router.get("/verification")
def kyc_page(request: Request, client: Client = Depends(web_client), db: Session = Depends(get_db)):
    docs = db.scalars(select(KycDocument).where(KycDocument.client_id == client.id).order_by(KycDocument.id.desc())).all()
    method = kyc_methods.active_method(db)
    checklist = kyc_methods.checklist(db, client, method)
    doc_types = sorted({t for item in checklist for t in item["types"]}) if checklist else sorted(clients.ALLOWED_DOC_TYPES)
    session = db.scalar(select(KycSession).where(KycSession.client_id == client.id).order_by(KycSession.id.desc()))
    return render(request, "portal/kyc.html", docs=docs, doc_types=doc_types, method=method, checklist=checklist,
                  session=session)


@router.post("/verification/start")
def kyc_start(request: Request, client: Client = Depends(web_client), db: Session = Depends(get_db)):
    """Custom KYC provider: create the verification and show the link to the provider's page."""
    return_url = f"{get_settings().public_url.rstrip('/')}/portal/verification"
    try:
        url = kyc_methods.start_session(db, client, return_url)
    except ServiceError as exc:
        flash(request, exc.message, "danger")
        return redirect("/portal/verification")
    method = kyc_methods.active_method(db)
    return render(request, "portal/continue.html", url=url, provider=method.name, title="Identity verification",
                  text="You'll complete the verification on our partner's secure page. You'll come back here afterwards.",
                  button="Start verification")


@router.post("/verification")
def kyc_upload(request: Request, doc_type: str = Form(...), file: UploadFile = File(...),
               client: Client = Depends(web_client), db: Session = Depends(get_db)):
    def run():
        kyc_methods.check_upload_allowed(db, client)
        content = kyc_methods.read_upload(file.file)
        clients.upload_kyc_document(db, client, client, doc_type, file.filename or "upload", content)
    return attempt(request, run, "Document uploaded. We'll review it shortly.", "/portal/verification")


# ---------------------------------------------------------------- accounts
@router.get("/accounts")
def accounts_page(request: Request, client: Client = Depends(web_client), db: Session = Depends(get_db)):
    accs = db.scalars(select(TradingAccount).where(TradingAccount.client_id == client.id,
                                                   TradingAccount.status != AccountStatus.archived)
                      .order_by(TradingAccount.id)).all()
    groups = db.scalars(select(AccountGroup).where(AccountGroup.enabled.is_(True)).order_by(AccountGroup.is_demo, AccountGroup.name)).all()
    return render(request, "portal/accounts.html", accounts=accs, groups=groups)


@router.post("/accounts/open")
def open_account(request: Request, account_group_id: int = Form(...), leverage: str = Form(""),
                 client: Client = Depends(web_client), db: Session = Depends(get_db)):
    try:
        result = accounts.open_account(db, for_account_group(db, account_group_id), client, client, account_group_id,
                                       opt_int(leverage))
    except (ServiceError, MT5Error) as exc:
        flash(request, getattr(exc, "message", str(exc)), "danger")
        return redirect("/portal/accounts")
    return render(request, "portal/credentials.html", account=result["account"],
                  master_password=result["master_password"], investor_password=result["investor_password"])


@router.post("/accounts/{login}/leverage")
def change_leverage(request: Request, login: str, leverage: int = Form(...), client: Client = Depends(web_client),
                    db: Session = Depends(get_db)):
    acc = accounts.get_client_account(db, client, login)
    return attempt(request, lambda: accounts.change_leverage(db, routed(), client, acc, leverage),
                   f"Leverage set to 1:{leverage}.", "/portal/accounts")


@router.post("/accounts/{login}/password")
def reset_mt5_password(request: Request, login: str, which: str = Form("master"), client: Client = Depends(web_client),
                       db: Session = Depends(get_db)):
    acc = accounts.get_client_account(db, client, login)
    try:
        pwd = accounts.reset_password(db, routed(), client, acc, investor=which == "investor")
    except (ServiceError, MT5Error) as exc:
        flash(request, getattr(exc, "message", str(exc)), "danger")
        return redirect("/portal/accounts")
    return render(request, "portal/credentials.html", account=acc, new_password=pwd, reset_kind=which)


# ---------------------------------------------------------------- wallet
@router.get("/wallet")
def wallet_page(request: Request, tab: str = "deposit", client: Client = Depends(web_client), db: Session = Depends(get_db)):
    w = wallet.get_wallet(db, client.id)
    db.commit()
    txs = db.scalars(select(WalletTransaction).where(WalletTransaction.wallet_id == w.id)
                     .order_by(WalletTransaction.id.desc()).limit(50)).all()
    reqs = db.scalars(select(PaymentRequest).where(PaymentRequest.client_id == client.id)
                      .order_by(PaymentRequest.id.desc()).limit(20)).all()
    tab = tab if tab in ("deposit", "withdraw", "transfer", "history") else "deposit"
    return render(request, "portal/wallet.html", wallet=w, txs=txs, requests=reqs, accounts=_live_accounts(db, client),
                  tab=tab, deposit_methods=payment_methods.methods_for(db, PaymentKind.deposit),
                  withdrawal_methods=payment_methods.methods_for(db, PaymentKind.withdrawal))


def _method(db: Session, method_id: str, kind: PaymentKind) -> PaymentMethod:
    m = db.get(PaymentMethod, opt_int(method_id)) if method_id else None
    if m is None or m not in payment_methods.methods_for(db, kind):
        raise ServiceError("Choose a payment method")
    return m


@router.post("/wallet/deposit")
def deposit(request: Request, amount: str = Form(...), method_id: str = Form(""), trading_login: str = Form(""),
            external_ref: str = Form(""), promo_code: str = Form(""), proof: UploadFile | None = File(None),
            client: Client = Depends(web_client), db: Session = Depends(get_db)):
    started = {}

    def run():
        payments.check_kyc(client)
        m = _method(db, method_id, PaymentKind.deposit)
        content = kyc_methods.read_upload(proof.file) if proof is not None and proof.filename else b""
        req = payments.create_request(
            db, client, client, PaymentKind.deposit, req_dec(amount), payment_method=m,
            trading_login=trading_login.strip() or None, promo_code=promo_code.strip() or None,
            external_ref=external_ref.strip() or None, proof=(proof.filename, content) if content else None)
        if m.is_custom:  # the client pays on the provider's page
            back_url = f"{get_settings().public_url.rstrip('/')}/portal/wallet/return/{req.id}"
            payment_methods.start_checkout(db, req, m, client, back_url)
            started["id"] = req.id
        else:
            flash(request, "Deposit request submitted. It will be credited once we receive the funds.")
    resp = attempt(request, run, None, "/portal/wallet?tab=history")
    return redirect(f"/portal/wallet/pay/{started['id']}") if "id" in started else resp


@router.get("/wallet/pay/{payment_id}")
def pay(request: Request, payment_id: int, client: Client = Depends(web_client), db: Session = Depends(get_db)):
    """Link to the provider's payment page (a form may not redirect to another site under our CSP)."""
    req = db.get(PaymentRequest, payment_id)
    if req is None or req.client_id != client.id or not req.gateway_url:
        return redirect("/portal/wallet?tab=history")
    if req.status != PaymentStatus.pending or req.gateway_status not in ("redirected",):
        return redirect(f"/portal/wallet/return/{payment_id}")
    return render(request, "portal/continue.html", url=req.gateway_url, provider=req.method, title="Complete your deposit",
                  text=f"Deposit of {req.amount} {req.currency}. You'll pay on {req.method}'s secure page and come back here.",
                  button=f"Pay {req.amount} {req.currency}")


@router.get("/wallet/return/{payment_id}")
def pay_return(request: Request, payment_id: int, client: Client = Depends(web_client), db: Session = Depends(get_db)):
    req = db.get(PaymentRequest, payment_id)
    if req is not None and req.client_id == client.id:
        if req.status == PaymentStatus.approved:
            flash(request, f"Thank you! Your deposit of {req.amount} {req.currency} has been credited.")
        elif req.status in (PaymentStatus.rejected, PaymentStatus.failed):
            flash(request, f"Your deposit was not completed: {req.review_comment or 'payment failed'}", "danger")
        else:
            flash(request, "Thank you! We'll credit your deposit as soon as the payment is confirmed.")
    return redirect("/portal/wallet?tab=history")


@router.post("/wallet/withdraw")
def withdraw(request: Request, amount: str = Form(...), details: str = Form(""), method_id: str = Form(""),
             client: Client = Depends(web_client), db: Session = Depends(get_db)):
    def run():
        payments.check_kyc(client)
        m = _method(db, method_id, PaymentKind.withdrawal)
        if not details.strip():
            raise ServiceError("Enter the bank account or wallet address to pay to")
        payments.create_request(db, client, client, PaymentKind.withdrawal, req_dec(amount), payment_method=m,
                                details=details.strip()[:500])
    return attempt(request, run, "Withdrawal requested. The amount is held until it is processed.", "/portal/wallet?tab=history")


@router.post("/wallet/transfer")
def transfer(request: Request, login: str = Form(...), direction: str = Form(...), amount: str = Form(...),
             client: Client = Depends(web_client), db: Session = Depends(get_db)):
    acc = accounts.get_client_account(db, client, login)

    def run():
        fn = accounts.transfer_to_mt5 if direction == "to_mt5" else accounts.transfer_from_mt5
        fn(db, routed(), client, client, acc, req_dec(amount))
    return attempt(request, run, "Transfer completed.", "/portal/wallet?tab=transfer")


# ---------------------------------------------------------------- copy trading
@router.get("/copy")
def copy_page(request: Request, client: Client = Depends(web_client), db: Session = Depends(get_db)):
    strategies = db.scalars(select(CopyStrategy).where(CopyStrategy.is_public.is_(True),
                                                       CopyStrategy.status == StrategyStatus.active)).all()
    mine = db.scalars(select(CopySubscription).where(CopySubscription.follower_client_id == client.id)
                      .order_by(CopySubscription.id.desc())).all()
    names = {s.id: s.name for s in db.scalars(select(CopyStrategy))}
    followers = dict(db.execute(select(CopySubscription.strategy_id, func.count()).where(
        CopySubscription.status != "stopped").group_by(CopySubscription.strategy_id)).all())
    my_strategies = db.scalars(select(CopyStrategy).where(CopyStrategy.owner_client_id == client.id)).all()
    busy = {(sub.server_id, sub.follower_login) for sub in mine if sub.status.value != "stopped"}
    following = {sub.strategy_id for sub in mine if sub.status.value != "stopped"}
    free = [a for a in _live_accounts(db, client) if (a.server_id, a.login) not in busy]
    return render(request, "portal/copy.html", strategies=strategies, mine=mine, names=names, followers=followers,
                  accounts=free, following=following, modes=list(AllocationMode), my_strategies=my_strategies)


@router.post("/copy/{strategy_id}/follow")
def follow(request: Request, strategy_id: int, follower_login: str = Form(...), allocation_mode: str = Form("equity_ratio"),
           ratio: str = Form("1"), max_lot: str = Form(""), client: Client = Depends(web_client), db: Session = Depends(get_db)):
    s = get_or_404(db, CopyStrategy, strategy_id, "Strategy")

    def run():
        if not s.is_public:
            raise Forbidden("Strategy is private")
        mode = opt_enum(AllocationMode, allocation_mode, "allocation mode")
        copy_trading.subscribe(db, routed(), client, client, s, follower_login, mode, req_dec(ratio, "Ratio"),
                               opt_dec(max_lot))
    return attempt(request, run, f"You are now copying {s.name}.", "/portal/copy")


@router.post("/copy/subscriptions/{sub_id}/stop")
def unfollow(request: Request, sub_id: int, close_positions: str = Form(""), client: Client = Depends(web_client),
             db: Session = Depends(get_db)):
    sub = get_or_404(db, CopySubscription, sub_id, "Subscription")
    if sub.follower_client_id != client.id:
        raise Forbidden("Not your subscription")
    return attempt(request, lambda: copy_trading.unsubscribe(db, routed(), client, sub, close_positions == "on"),
                   "Copying stopped.", "/portal/copy")


@router.post("/copy/become-master")
def become_master(request: Request, master_login: str = Form(...), name: str = Form(...), description: str = Form(""),
                  performance_fee_pct: str = Form("20"), client: Client = Depends(web_client), db: Session = Depends(get_db)):
    def run():
        fee = opt_dec(performance_fee_pct) or 0
        if not 0 <= fee <= 50:
            raise ServiceError("Performance fee must be between 0 and 50%")
        copy_trading.create_strategy(db, client, client, master_login, name.strip(), description=description.strip(),
                                     performance_fee_pct=fee)
    return attempt(request, run, "Your strategy is live. Followers will copy your trades on that account.", "/portal/copy")


# ---------------------------------------------------------------- PAMM
@router.get("/pamm")
def pamm_page(request: Request, client: Client = Depends(web_client), db: Session = Depends(get_db)):
    funds = db.scalars(select(PammFund).where(PammFund.status == StrategyStatus.active)).all()
    gw = routed()
    statements = []
    for inv in db.scalars(select(PammInvestment).where(PammInvestment.client_id == client.id, PammInvestment.units > 0)):
        fund = db.get(PammFund, inv.fund_id)
        try:
            statements.append({"fund": fund, **pamm.investor_statement(db, gw, fund, client.id)})
        except MT5Error:
            statements.append({"fund": fund, "units": inv.units, "nav": fund.last_nav, "value": None,
                               "net_invested": inv.net_invested, "pnl": None, "share_pct": None})
    w = wallet.get_wallet(db, client.id)
    db.commit()
    return render(request, "portal/pamm.html", funds=funds, statements=statements, wallet=w)


@router.post("/pamm/{fund_id}/invest")
def pamm_invest(request: Request, fund_id: int, amount: str = Form(...), client: Client = Depends(web_client),
                db: Session = Depends(get_db)):
    fund = get_or_404(db, PammFund, fund_id, "Fund")
    return attempt(request, lambda: pamm.invest(db, routed(), client, fund, client, req_dec(amount)),
                   f"Invested in {fund.name}.", "/portal/pamm")


@router.post("/pamm/{fund_id}/withdraw")
def pamm_withdraw(request: Request, fund_id: int, amount: str = Form(""), all_units: str = Form(""),
                  client: Client = Depends(web_client), db: Session = Depends(get_db)):
    fund = get_or_404(db, PammFund, fund_id, "Fund")

    def run():
        if all_units == "on":
            inv = db.scalar(select(PammInvestment).where(PammInvestment.fund_id == fund.id,
                                                         PammInvestment.client_id == client.id))
            if inv is None:
                raise ServiceError("You have no investment in this fund")
            return pamm.withdraw(db, routed(), client, fund, client, units=inv.units)
        return pamm.withdraw(db, routed(), client, fund, client, amount=req_dec(amount))
    return attempt(request, run, "Withdrawal paid to your wallet.", "/portal/pamm")


# ---------------------------------------------------------------- referral
@router.get("/referral")
def referral(request: Request, client: Client = Depends(web_client), db: Session = Depends(get_db)):
    referred = db.scalars(select(Client).where(Client.referred_by_id == client.id).order_by(Client.id.desc()).limit(200)).all()
    ib = db.scalar(select(IBProfile).where(IBProfile.client_id == client.id))
    totals, recent = {}, []
    if ib:
        totals = dict(db.execute(select(IBCommission.status, func.sum(IBCommission.amount))
                                 .where(IBCommission.ib_id == ib.id).group_by(IBCommission.status)).all())
        recent = db.scalars(select(IBCommission).where(IBCommission.ib_id == ib.id)
                            .order_by(IBCommission.id.desc()).limit(50)).all()
    link = f"{get_settings().public_url.rstrip('/')}/portal/register?ref={client.referral_code}"
    return render(request, "portal/referral.html", referred=referred, ib=ib, totals=totals, recent=recent, link=link,
                  qr=qr_data_uri(link))
