"""Money-flow hardening: row locks against double spending, provider callbacks, permissions,
uploads, outbound provider calls and copy-trading timeouts."""

import io
import json
import threading
import time
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.core.errors import Forbidden, ServiceError
from app.db import SessionLocal
from app.models import (
    AccountStatus, AuditLog, Client, CopyExecution, CopyLink, KycDocument, KycStatus, PammFund, PaymentKind,
    PaymentRequest, PaymentStatus, Staff, TradingAccount, WalletTxKind,
)
from app.mt5 import BUY
from app.mt5.gateway import TradeResult
from app.services import (
    accounts, clients, copy_trading, integrations, kyc_methods, notifications, pamm, payment_methods, payments,
    staff as staff_svc, sync, wallet,
)
from tests.conftest import STRONG, login_client, login_staff, register

SECRET = "test-secret"


def _signed(api, path, body: dict, secret=SECRET):
    raw = json.dumps(body).encode()
    return api.post(path, content=raw, headers={"Content-Type": "application/json",
                                                "X-Signature": integrations.sign(secret, raw)})


def _staff(db, email, perms) -> Staff:
    role = staff_svc.create_role(db, None, email.split("@")[0], perms)
    return staff_svc.create_staff(db, None, email=email, full_name="Staff", password="Staff-Pass1",
                                  role_ids=[role.id])


def _slow(gw, monkeypatch, seconds=0.4):
    """Widen the race window: every MT5 balance operation takes a while."""
    real = gw.balance_operation

    def slow(*a, **k):
        time.sleep(seconds)
        return real(*a, **k)
    monkeypatch.setattr(gw, "balance_operation", slow)


def _race(fn, n=2) -> list:
    """Run fn(session) in n threads at once, each with its own session; returns results or error messages."""
    out = []

    def run():
        with SessionLocal() as s:
            try:
                out.append(fn(s))
            except ServiceError as exc:
                out.append(exc.message)
    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return out


# ---------------------------------------------------------------- 1. PAMM double withdrawal
def test_pamm_parallel_withdrawals_pay_once(db, funded, gw, monkeypatch):
    mgr, master = funded("pm@x.com", amount=0)
    inv, _ = funded("inv@x.com", amount=0)
    wallet.post(db, inv["id"], 1000, WalletTxKind.deposit)
    db.commit()
    fund = pamm.create_fund(db, gw, None, db.get(Client, mgr["id"]), master, "Fund")
    pamm.invest(db, gw, None, fund, db.get(Client, inv["id"]), 1000)
    _slow(gw, monkeypatch)

    results = _race(lambda s: pamm.withdraw(s, gw, None, s.get(PammFund, fund.id), s.get(Client, inv["id"]),
                                            units=Decimal("1000")).amount)
    assert sorted(map(str, results)) == ["1000.00", "Invalid units: exceeds holding"]
    db.expire_all()
    assert wallet.get_wallet(db, inv["id"]).balance == Decimal("1000")
    assert gw.balance[master] == Decimal("0")


# ---------------------------------------------------------------- 2. payment approve race
def test_parallel_approvals_credit_once(db, funded, gw, admin, monkeypatch):
    c, login = funded("pay@x.com", amount=0)
    client = db.get(Client, c["id"])
    req = payments.create_request(db, client, client, PaymentKind.deposit, 500, trading_login=login)
    _slow(gw, monkeypatch)

    results = _race(lambda s: payments.approve(s, gw, s.get(Staff, admin.id), s.get(PaymentRequest, req.id)).status.value)
    assert sorted(map(str, results)) == ["Request is already approved", "approved"]
    assert gw.balance[login] == Decimal("500")


def test_reject_after_approval_is_refused(db, funded, gw, admin):
    c, login = funded("pay2@x.com", amount=0)
    client = db.get(Client, c["id"])
    req = payments.create_request(db, client, client, PaymentKind.deposit, 100, trading_login=login)
    payments.approve(db, gw, admin, req)
    with pytest.raises(ServiceError, match="already approved"):
        payments.reject(db, admin, req, "too late")


# ---------------------------------------------------------------- 3. auto-credit needs a matching amount
@pytest.fixture
def card(db, monkeypatch):
    monkeypatch.setattr(integrations, "http_post",
                        lambda url, body, headers: (200, json.dumps({"url": "https://psp.example/pay"})))
    return payment_methods.create_method(db, None, name="Card", kind="custom", api_url="https://psp.example/api",
                                         secret=SECRET)


def _deposit(db, api, method, email, amount=100) -> PaymentRequest:
    c = register(api, email)
    client = db.get(Client, c["id"])
    client.kyc_status = KycStatus.approved
    db.commit()
    req = payments.create_request(db, client, client, PaymentKind.deposit, amount, payment_method=method)
    payment_methods.start_checkout(db, req, method, client, "https://crm.example/back")
    return req


def test_auto_credit_requires_amount_field(db):
    with pytest.raises(ServiceError, match="amount field"):
        payment_methods.create_method(db, None, name="X", kind="custom", api_url="https://psp.example/api",
                                      secret=SECRET, amount_field="")


@pytest.mark.parametrize("extra, held", [
    ({}, "paid amount missing"),
    ({"amount": "lots"}, "is not a number"),
    ({"amount": "100.00", "currency": "EUR"}, "currency EUR differs"),
])
def test_unverifiable_paid_callback_is_held(api, db, card, extra, held):
    req = _deposit(db, api, card, "held@x.com")
    r = _signed(api, f"/callbacks/payment/{card.callback_token}", {"reference": req.gateway_ref, "status": "paid",
                                                                   **extra})
    assert held in r.json()["result"]
    db.expire_all()
    assert (req.status, req.gateway_status) == (PaymentStatus.pending, "mismatch")
    assert wallet.get_wallet(db, req.client_id).balance == Decimal("0")


def test_matching_paid_callback_with_currency_credits(api, db, card):
    req = _deposit(db, api, card, "ok@x.com")
    r = _signed(api, f"/callbacks/payment/{card.callback_token}", {"reference": req.gateway_ref, "status": "paid",
                                                                   "amount": "100", "currency": "usd"})
    assert r.json()["result"] == "paid and credited"
    assert wallet.get_wallet(db, req.client_id).balance == Decimal("100")


def test_callback_after_staff_rejection_is_ignored(api, db, card, admin):
    req = _deposit(db, api, card, "late@x.com")
    payments.reject(db, admin, req, "suspicious")
    r = _signed(api, f"/callbacks/payment/{card.callback_token}", {"reference": req.gateway_ref, "status": "paid",
                                                                   "amount": "100"})
    assert r.json()["result"] == "already rejected"
    assert wallet.get_wallet(db, req.client_id).balance == Decimal("0")


# ---------------------------------------------------------------- 4. sensitive method settings need a second permission
def test_payment_method_secret_and_auto_credit_need_finance_approve(db, admin):
    methods_only = _staff(db, "pm-staff@b.com", ["finance.methods"])
    with pytest.raises(Forbidden, match="finance.approve"):
        payment_methods.create_method(db, methods_only, name="PSP", kind="custom", api_url="https://psp.example/api",
                                      secret=SECRET, auto_credit=False)
    m = payment_methods.create_method(db, admin, name="PSP", kind="custom", api_url="https://psp.example/api",
                                      secret=SECRET, auto_credit=False)
    with pytest.raises(Forbidden):
        payment_methods.update_method(db, methods_only, m, {"auto_credit": True})
    with pytest.raises(Forbidden):
        payment_methods.update_method(db, methods_only, m, {"secret": "new"})
    db.refresh(m)
    assert m.auto_credit is False
    payment_methods.update_method(db, methods_only, m, {"description": "Cards"})  # other settings are fine
    payment_methods.update_method(db, admin, m, {"auto_credit": True, "secret": "new"})
    actions = set(db.scalars(select(AuditLog.action).where(AuditLog.entity_id == str(m.id))))
    assert {"payment_method.secret_change", "payment_method.auto_credit_on"} <= actions


def test_kyc_method_secret_and_activation_need_kyc_review(db, admin):
    methods_only = _staff(db, "kyc-staff@b.com", ["kyc.methods"])
    with pytest.raises(Forbidden, match="kyc.review"):
        kyc_methods.create_method(db, methods_only, name="IDV", kind="custom", api_url="https://kyc.example/api",
                                  secret=SECRET)
    m = kyc_methods.create_method(db, admin, name="IDV", kind="custom", api_url="https://kyc.example/api",
                                  secret=SECRET)
    with pytest.raises(Forbidden):
        kyc_methods.activate(db, methods_only, m)
    kyc_methods.activate(db, admin, m)
    assert db.scalar(select(AuditLog).where(AuditLog.action == "kyc_method.auto_approve_on")) is not None


# ---------------------------------------------------------------- 5. API payments go through a configured method
def test_api_payment_needs_an_available_method(api, admin_h, db):
    bank = payment_methods.create_method(db, None, name="Bank", kind="manual", max_amount=Decimal("1000"))
    off = payment_methods.create_method(db, None, name="Old bank", kind="manual", enabled=False)
    c = register(api, "apipay@x.com")
    db.get(Client, c["id"]).kyc_status = KycStatus.approved
    db.commit()
    ch = login_client(api, "apipay@x.com")
    post = lambda body: api.post("/api/portal/payments", json={"kind": "deposit", "amount": "100", **body}, headers=ch)  # noqa: E731
    assert post({"method": "anything"}).status_code == 422  # free-text method is gone
    assert post({"method_id": 9999}).status_code == 400
    assert post({"method_id": off.id}).status_code == 400
    assert "maximum for Bank" in post({"method_id": bank.id, "amount": "5000"}).text  # method rules apply
    r = post({"method_id": bank.id})
    assert r.status_code == 201 and r.json()["method"] == "Bank"


# ---------------------------------------------------------------- 6. uploads
def test_upload_is_read_in_chunks_and_capped():
    assert kyc_methods.read_upload(io.BytesIO(b"x" * 1000), limit=1000) == b"x" * 1000
    with pytest.raises(ServiceError, match="too large"):
        kyc_methods.read_upload(io.BytesIO(b"x" * 1001), limit=1000)


def test_kyc_document_count_is_capped(api, db):
    c = register(api, "docs@x.com")
    ch = login_client(api, "docs@x.com")
    big = b"%PDF-1.4 " + b"0" * (10 * 1024 * 1024)
    r = api.post("/api/portal/kyc", data={"doc_type": "passport"}, files={"file": ("p.pdf", big)}, headers=ch)
    assert r.status_code == 400 and "too large" in r.text
    db.add_all([KycDocument(client_id=c["id"], doc_type="passport", file_path="x", original_name="x.pdf")
                for _ in range(kyc_methods.MAX_PENDING_DOCUMENTS)])
    db.commit()
    r = api.post("/api/portal/kyc", data={"doc_type": "passport"}, files={"file": ("p.pdf", b"%PDF-1.4 x")},
                 headers=ch)
    assert r.status_code == 400 and "waiting for review" in r.text


# ---------------------------------------------------------------- 7. outbound provider calls
def test_provider_urls_are_restricted_in_production(monkeypatch):
    integrations.check_url("http://localhost:8000/sandbox")  # the local sandbox, outside production
    with pytest.raises(ServiceError, match="https"):
        integrations.check_url("ftp://psp.example/api")
    monkeypatch.setattr(get_settings(), "app_env", "production")
    with pytest.raises(ServiceError, match="https"):
        integrations.check_url("http://psp.example/api")
    for url in ("https://127.0.0.1/api", "https://10.1.2.3/api", "https://169.254.169.254/latest",
                "https://[::1]/api", "https://224.0.0.1/"):
        with pytest.raises(ServiceError, match="not a public address"):
            integrations.check_url(url)
    integrations.check_url("https://8.8.8.8/api")


def test_provider_errors_are_not_echoed(db, monkeypatch):
    m = payment_methods.create_method(db, None, name="Card", kind="custom", api_url="https://psp.example/api",
                                      secret=SECRET)
    monkeypatch.setattr(integrations, "http_post", lambda *a: (500, "internal-token=abc123"))
    with pytest.raises(ServiceError) as exc:
        integrations.start(m, {"reference": "R1"})
    assert "HTTP 500" in exc.value.message and "abc123" not in exc.value.message
    monkeypatch.setattr(integrations, "http_post", lambda *a: (200, '{"no": "url", "secret": "abc123"}'))
    with pytest.raises(ServiceError) as exc:
        integrations.start(m, {"reference": "R1"})
    assert "abc123" not in exc.value.message


def test_redirects_are_not_followed():
    handler = integrations._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "http://169.254.169.254/") is None


# ---------------------------------------------------------------- 8. KYC callbacks only for sessions still waiting
def test_kyc_callback_ignored_after_staff_decision_and_on_replay(api, db, admin, monkeypatch):
    monkeypatch.setattr(integrations, "http_post",
                        lambda url, body, headers: (200, json.dumps({"url": "https://kyc.example/v"})))
    m = kyc_methods.create_method(db, None, name="IDV", kind="custom", api_url="https://kyc.example/api",
                                  secret=SECRET)
    kyc_methods.activate(db, None, m)
    client = db.get(Client, register(api, "idv@x.com")["id"])
    from app.models import KycSession

    kyc_methods.start_session(db, client, "https://crm.example/back")
    first = db.scalar(select(KycSession.reference))
    clients.set_kyc_status(db, admin, client, KycStatus.rejected)  # staff decided meanwhile
    r = _signed(api, f"/callbacks/kyc/{m.callback_token}", {"reference": first, "status": "approved"})
    assert r.json()["result"] == "ignored: client already rejected"
    db.expire_all()
    assert client.kyc_status == KycStatus.rejected

    kyc_methods.start_session(db, client, "https://crm.example/back")
    second = db.scalar(select(KycSession.reference).where(KycSession.reference != first))
    assert _signed(api, f"/callbacks/kyc/{m.callback_token}", {"reference": second, "status": "approved"}).status_code == 200
    r = _signed(api, f"/callbacks/kyc/{m.callback_token}", {"reference": second, "status": "rejected"})
    assert r.json()["result"] == "ignored: session already approved"
    r = _signed(api, f"/callbacks/kyc/{m.callback_token}", {"reference": first, "status": "approved"})
    assert r.json()["result"].startswith("ignored")
    db.expire_all()
    assert client.kyc_status == KycStatus.approved


# ---------------------------------------------------------------- 9. leverage
def test_leverage_needs_an_active_account_with_a_group(db, funded, gw):
    _, login = funded("lev@x.com", amount=0)
    acc = db.scalar(select(TradingAccount).where(TradingAccount.login == login))
    accounts.change_leverage(db, gw, None, acc, 200)
    acc.status = AccountStatus.disabled
    db.commit()
    with pytest.raises(ServiceError, match="not active"):
        accounts.change_leverage(db, gw, None, acc, 500)
    acc.status, acc.account_group_id = AccountStatus.active, None
    db.commit()
    db.refresh(acc)
    with pytest.raises(ServiceError, match="no account group"):
        accounts.change_leverage(db, gw, None, acc, 1000)


# ---------------------------------------------------------------- 10. copy / PAMM staff actions
def test_copy_and_pamm_staff_actions_check_client_and_finance(api, db, funded, gw, admin):
    m, master = funded("cm@x.com")
    f, follower = funded("cf@x.com")
    strat = copy_trading.create_strategy(db, None, db.get(Client, m["id"]), master, "Alpha")
    _staff(db, "copier@b.com", ["clients.view", "copy.view", "copy.manage"])
    h = login_staff(api, "copier@b.com", "Staff-Pass1")
    r = api.post(f"/api/copy/strategies/{strat.id}/subscriptions",
                 json={"follower_client_id": f["id"], "follower_login": follower}, headers=h)
    assert r.status_code == 403 and "not assigned" in r.text
    r = api.post("/api/pamm/funds/1/invest", json={"client_id": f["id"], "amount": "100"}, headers=h)
    assert r.status_code == 403 and "finance.approve" in r.text
    r = api.post(f"/api/pamm/funds/1/withdraw/{f['id']}", json={"units": "1"}, headers=h)
    assert r.status_code == 403


# ---------------------------------------------------------------- 11. staff transfers are audited and notified
def test_staff_transfer_is_audited_and_notified(db, funded, gw, admin):
    c, login = funded("tr@x.com", amount=0)
    client = db.get(Client, c["id"])
    wallet.post(db, client.id, 300, WalletTxKind.deposit)
    db.commit()
    acc = db.scalar(select(TradingAccount).where(TradingAccount.login == login))
    notifications.outbox.clear()
    accounts.transfer_to_mt5(db, gw, admin, client, acc, 200)
    accounts.transfer_from_mt5(db, gw, admin, client, acc, 50)
    actions = list(db.scalars(select(AuditLog.action).where(AuditLog.action.like("staff_transfer.%"))))
    assert sorted(actions) == ["staff_transfer.from_mt5", "staff_transfer.to_mt5"]
    sent = [m for m in notifications.outbox if "Staff transfer" in m["Subject"]]
    assert len(sent) == 2 and "200.00" in sent[0].get_content()
    notifications.outbox.clear()
    accounts.transfer_to_mt5(db, gw, client, client, acc, 10)  # the client's own transfer: no staff alert
    assert not notifications.outbox


# ---------------------------------------------------------------- 12. copy-trading dealer timeouts
def _copy_setup(db, funded, gw):
    m, master = funded("tm@x.com", amount=10000)
    f, follower = funded("tf@x.com", amount=10000)
    strat = copy_trading.create_strategy(db, None, db.get(Client, m["id"]), master, "Timeouts")
    copy_trading.subscribe(db, gw, None, db.get(Client, f["id"]), strat, follower)
    sync.full_sync(db, gw)
    return master, follower


def _timeout_once(gw, monkeypatch, follower, executed: bool):
    real, state = gw.open_position, {"hit": False}

    def flaky(login, *a, **k):
        if login == follower and not state["hit"]:
            state["hit"] = True
            if executed:
                real(login, *a, **k)  # filled on MT5, but the answer never arrived
            return TradeResult(retcode=-1, comment="dealer timeout")
        return real(login, *a, **k)
    monkeypatch.setattr(gw, "open_position", flaky)


@pytest.mark.parametrize("executed", [True, False])
def test_dealer_timeout_is_reconciled_not_blindly_retried(db, funded, gw, monkeypatch, executed):
    master, follower = _copy_setup(db, funded, gw)
    _timeout_once(gw, monkeypatch, follower, executed)
    first = gw.open_position(master, "EURUSD", BUY, Decimal("1"))
    sync.sync_deals(db, gw)
    ex = db.scalar(select(CopyExecution))
    assert ex.status == "unknown" and ex.retcode == -1
    assert len(gw.get_positions([follower])) == (1 if executed else 0)

    gw.open_position(master, "XAUUSD", BUY, Decimal("0.1"))  # the next pass reconciles first
    sync.sync_deals(db, gw)
    db.expire_all()
    assert ex.status == "done"
    eurusd = [p for p in gw.get_positions([follower]) if p.symbol == "EURUSD"]
    assert len(eurusd) == 1 and eurusd[0].comment.startswith(f"copy#{ex.id} ")  # opened exactly once
    link = db.scalar(select(CopyLink).where(CopyLink.master_position_id == first.position_id))
    assert link.follower_position_id == eurusd[0].position_id

    gw.close_position(master, first.position_id, "EURUSD", BUY, Decimal("1"))
    sync.sync_deals(db, gw)
    assert [p.symbol for p in gw.get_positions([follower])] == ["XAUUSD"]
    assert db.scalar(select(func.count()).select_from(CopyExecution).where(CopyExecution.status != "done")) == 0
