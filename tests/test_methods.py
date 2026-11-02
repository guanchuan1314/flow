"""Payment methods, KYC methods, account group deletion with accounts, and client -> MT5 sync."""

import json
from decimal import Decimal
from urllib.parse import urlparse

import pytest
from sqlalchemy import select

from app.core.errors import ServiceError
from app.models import (
    AccountStatus, AccountGroup, Client, ClientStatus, KycStatus, PaymentRequest, PaymentStatus, ProviderCallback,
    TradingAccount,
)
from app.services import (
    accounts, client_sync, clients, integrations, kyc_methods, payment_methods, wallet,
)
from tests.conftest import STRONG, register
from tests.test_web import admin_web, approve_kyc_direct, csrf_of, form_post, open_live_direct, web_login  # noqa: F401

SECRET = "test-secret"


@pytest.fixture
def provider(monkeypatch):
    """A fake provider API: records the requests and answers with a unique URL."""
    calls = []

    def fake_post(url, body, headers):
        calls.append({"url": url, "body": json.loads(body), "headers": headers})
        return 200, json.dumps({"data": {"checkout": f"https://psp.example/pay/{len(calls)}"}})
    monkeypatch.setattr(integrations, "http_post", fake_post)
    return calls


def _signed(api, path, body: dict, secret=SECRET):
    raw = json.dumps(body).encode()
    return api.post(path, content=raw, headers={"Content-Type": "application/json",
                                                "X-Signature": integrations.sign(secret, raw)})


def _client(api, db, email):
    c = register(api, email)
    approve_kyc_direct(db, c["id"])
    web_login(api, email, STRONG, base="/portal")
    return db.get(Client, c["id"])


# ---------------------------------------------------------------- payment methods
def test_custom_payment_provider_round_trip(api, db, provider):
    m = payment_methods.create_method(db, None, name="Card", kind="custom", api_url="https://psp.example/api",
                                      url_field="data.checkout", secret=SECRET, max_amount=Decimal("5000"))
    client = _client(api, db, "payer@x.com")

    r = form_post(api, "/portal/wallet", "/portal/wallet/deposit", {"method_id": m.id, "amount": "9000"})
    assert "maximum for Card is 5000" in r.text  # method limits apply
    r = form_post(api, "/portal/wallet", "/portal/wallet/deposit", {"method_id": m.id, "amount": "250"})
    req = db.scalar(select(PaymentRequest).where(PaymentRequest.client_id == client.id))
    assert "https://psp.example/pay/1" in r.text and req.gateway_status == "redirected"  # "continue to pay" page
    sent = provider[0]
    assert sent["body"]["reference"] == req.gateway_ref and sent["body"]["amount"] == "250.00"
    assert sent["headers"]["X-Signature"] == integrations.sign(SECRET, json.dumps(
        sent["body"], separators=(",", ":")).encode())
    callback = urlparse(sent["body"]["callback_url"]).path
    assert callback == f"/callbacks/payment/{m.callback_token}"

    assert _signed(api, callback, {"reference": req.gateway_ref, "status": "paid"}, secret="wrong").status_code == 401
    r = _signed(api, callback, {"reference": req.gateway_ref, "status": "paid", "amount": "250.00",
                                "transaction_id": "PSP-998877"})
    assert r.status_code == 200 and r.json()["result"] == "paid and credited"
    db.expire_all()
    assert req.status == PaymentStatus.approved and wallet.get_wallet(db, client.id).balance == Decimal("250")
    # what the provider reported is kept for reconciliation, also when the deposit is credited at once
    assert (req.gateway_status, req.external_ref) == ("paid", "PSP-998877") and req.paid_at is not None
    r = _signed(api, callback, {"reference": req.gateway_ref, "status": "paid", "amount": "250.00"})
    assert r.json()["result"] == "already approved"  # provider retries are harmless
    assert wallet.get_wallet(db, client.id).balance == Decimal("250")
    assert db.scalar(select(ProviderCallback).where(ProviderCallback.signature_ok.is_(False))) is not None
    r = api.get(f"/portal/wallet/return/{req.id}", follow_redirects=True)
    assert "has been credited" in r.text


def test_paid_amount_must_match(api, db, provider):
    m = payment_methods.create_method(db, None, name="Card", kind="custom", api_url="https://psp.example/api",
                                      url_field="data.checkout", secret=SECRET)
    client = _client(api, db, "short@x.com")
    form_post(api, "/portal/wallet", "/portal/wallet/deposit", {"method_id": m.id, "amount": "100"})
    req = db.scalar(select(PaymentRequest).where(PaymentRequest.client_id == client.id))
    r = _signed(api, f"/callbacks/payment/{m.callback_token}", {"reference": req.gateway_ref, "status": "paid",
                                                                "amount": "10"})
    assert "held for review" in r.json()["result"]
    db.expire_all()
    assert (req.status, req.gateway_status) == (PaymentStatus.pending, "mismatch")
    r = _signed(api, f"/callbacks/payment/{m.callback_token}", {"reference": req.gateway_ref, "status": "declined"})
    db.expire_all()
    assert req.status == PaymentStatus.pending  # already held: finance decides


def test_provider_failure_rejects_and_unreachable_provider_fails(api, db, monkeypatch, provider):
    m = payment_methods.create_method(db, None, name="Card", kind="custom", api_url="https://psp.example/api",
                                      url_field="data.checkout", secret=SECRET)
    client = _client(api, db, "fail@x.com")
    form_post(api, "/portal/wallet", "/portal/wallet/deposit", {"method_id": m.id, "amount": "100"})
    req = db.scalar(select(PaymentRequest).where(PaymentRequest.client_id == client.id))
    _signed(api, f"/callbacks/payment/{m.callback_token}",
            {"reference": req.gateway_ref, "status": "declined", "reason": "card expired"})
    db.expire_all()
    assert req.status == PaymentStatus.rejected and "card expired" in req.review_comment

    monkeypatch.setattr(integrations, "http_post", lambda *a: (500, "boom"))
    r = form_post(api, "/portal/wallet", "/portal/wallet/deposit", {"method_id": m.id, "amount": "100"})
    assert "not available right now" in r.text
    assert db.scalar(select(PaymentRequest).where(PaymentRequest.status == PaymentStatus.failed)) is not None


def test_manual_method_with_proof(api, db, admin_web):
    m = payment_methods.create_method(db, None, name="Bank", kind="manual", requires_proof=True,
                                      instructions="IBAN XX00 1234")
    client = _client(api, db, "bank@x.com")
    page = api.get("/portal/wallet")
    assert "IBAN XX00 1234" in page.text
    r = api.post("/portal/wallet/deposit", data={"method_id": m.id, "amount": "300", "_csrf": csrf_of(page)},
                 follow_redirects=True)
    assert "proof of payment" in r.text
    r = api.post("/portal/wallet/deposit", data={"method_id": m.id, "amount": "300", "_csrf": csrf_of(r)},
                 files={"proof": ("receipt.pdf", b"%PDF-1.7 receipt", "application/pdf")}, follow_redirects=True)
    assert "Deposit request submitted" in r.text
    req = db.scalar(select(PaymentRequest).where(PaymentRequest.client_id == client.id))
    assert req.method_id == m.id and req.proof_name == "receipt.pdf"
    r = admin_web.get(f"/admin/payments/{req.id}/proof")
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and "attachment" in r.headers["content-disposition"]


def test_payment_method_admin_screens(admin_web, db):
    r = form_post(admin_web, "/admin/payment-methods/new", "/admin/payment-methods/new", {
        "name": "PSP", "kind": "custom", "allow_deposit": "on", "enabled": "on", "currency": "USD"})
    assert "API URL or a redirect URL template" in r.text  # custom needs the provider settings
    r = form_post(admin_web, "/admin/payment-methods/new", "/admin/payment-methods/new", {
        "name": "PSP", "kind": "custom", "allow_deposit": "on", "enabled": "on", "currency": "USD",
        "redirect_template": "https://psp.example/?ref={reference}&sig={signature}", "secret": "s3cret"})
    m = db.scalar(select(payment_methods.PaymentMethod).where(payment_methods.PaymentMethod.name == "PSP"))
    assert f"/callbacks/payment/{m.callback_token}" in r.text and "s3cret" not in r.text
    old = m.callback_token
    form_post(admin_web, f"/admin/payment-methods/{m.id}", f"/admin/payment-methods/{m.id}/callback-url", {})
    db.refresh(m)
    assert m.callback_token != old
    assert admin_web.get("/admin/payment-methods").status_code == 200


# ---------------------------------------------------------------- KYC methods
def test_only_one_kyc_method_is_active(db):
    a = kyc_methods.create_method(db, None, name="Docs", kind="manual", required_documents=["identity"])
    b = kyc_methods.create_method(db, None, name="Provider", kind="custom", api_url="https://kyc.example/api",
                                  secret=SECRET)
    kyc_methods.activate(db, None, a)
    kyc_methods.activate(db, None, b)
    db.expire_all()
    assert (a.active, b.active) == (False, True) and kyc_methods.active_method(db).id == b.id
    with pytest.raises(ServiceError, match="active KYC method"):
        kyc_methods.delete_method(db, None, b)


def test_manual_kyc_waits_for_all_required_documents(api, db):
    m = kyc_methods.create_method(db, None, name="Docs", kind="manual", required_documents=["identity", "proof_of_address"])
    kyc_methods.activate(db, None, m)
    c = db.get(Client, register(api, "docs@x.com")["id"])
    clients.upload_kyc_document(db, None, c, "passport", "p.pdf", b"%PDF-1.4 x")
    assert c.kyc_status == KycStatus.not_submitted  # address proof still missing
    clients.upload_kyc_document(db, None, c, "proof_of_address", "bill.pdf", b"%PDF-1.4 y")
    assert c.kyc_status == KycStatus.pending


def test_custom_kyc_provider_round_trip(api, db, provider):
    m = kyc_methods.create_method(db, None, name="IDV", kind="custom", api_url="https://kyc.example/api",
                                  url_field="data.checkout", secret=SECRET)
    kyc_methods.activate(db, None, m)
    c = register(api, "idv@x.com")
    web_login(api, "idv@x.com", STRONG, base="/portal")
    assert "Verify my identity" in api.get("/portal/verification").text
    r = form_post(api, "/portal/verification", "/portal/verification/start", {})
    assert "https://psp.example/pay/1" in r.text
    ref = provider[0]["body"]["reference"]
    r = _signed(api, f"/callbacks/kyc/{m.callback_token}", {"reference": ref, "status": "approved"})
    assert r.status_code == 200
    db.expire_all()
    assert db.get(Client, c["id"]).kyc_status == KycStatus.approved


# ---------------------------------------------------------------- callback IP whitelist
def _callback(handler, db, m, body: dict, ip: str):
    """A signed provider callback arriving from `ip`."""
    raw = json.dumps(body).encode()
    return handler(db, m.callback_token, raw, {"X-Signature": integrations.sign(SECRET, raw)}, "application/json", ip)


def test_callback_ip_whitelist_values():
    with pytest.raises(ServiceError, match="is empty"):
        integrations.clean_ips(" \n ")
    with pytest.raises(ServiceError, match="not an IP address"):
        integrations.clean_ips("203.0.113.7\nprovider.example")
    assert integrations.clean_ips("203.0.113.7, 10.1.2.3/24\n10.1.2.3/24") == ["203.0.113.7", "10.1.2.0/24"]
    assert integrations.clean_ips("203.0.113.7\n*") == ["*"]  # "*" allows any address anyway
    assert integrations.clean_ips(["2001:db8::/32"]) == ["2001:db8::/32"]


def test_payment_callbacks_only_from_whitelisted_ips(api, db, provider):
    m = payment_methods.create_method(db, None, name="Card", kind="custom", api_url="https://psp.example/api",
                                      url_field="data.checkout", secret=SECRET)
    assert m.callback_ips == ["*"]  # default: any address
    payment_methods.update_method(db, None, m, {"callback_ips": "203.0.113.0/24\n198.51.100.7"})
    client = _client(api, db, "ipcheck@x.com")
    form_post(api, "/portal/wallet", "/portal/wallet/deposit", {"method_id": m.id, "amount": "100"})
    req = db.scalar(select(PaymentRequest).where(PaymentRequest.client_id == client.id))
    paid = {"reference": req.gateway_ref, "status": "paid", "amount": "100.00"}

    r = _signed(api, f"/callbacks/payment/{m.callback_token}", paid)  # the test client is not on the list
    assert r.status_code == 403 and "not on the callback whitelist" in r.json()["result"]
    assert _callback(payment_methods.handle_callback, db, m, paid, "192.0.2.10")[0] == 403
    db.expire_all()
    assert req.status == PaymentStatus.pending and wallet.get_wallet(db, client.id).balance == 0
    refused = db.scalars(select(ProviderCallback).where(ProviderCallback.method_id == m.id)).all()
    assert len(refused) == 2 and all("whitelist" in c.outcome for c in refused)  # still logged on the method

    assert _callback(payment_methods.handle_callback, db, m, paid, "203.0.113.50") == (200, "paid and credited")
    db.expire_all()
    assert wallet.get_wallet(db, client.id).balance == Decimal("100")


def test_kyc_callbacks_only_from_whitelisted_ips(api, db, provider):
    m = kyc_methods.create_method(db, None, name="IDV", kind="custom", api_url="https://kyc.example/api",
                                  url_field="data.checkout", secret=SECRET, callback_ips="198.51.100.7")
    kyc_methods.activate(db, None, m)
    c = register(api, "kycip@x.com")
    web_login(api, "kycip@x.com", STRONG, base="/portal")
    form_post(api, "/portal/verification", "/portal/verification/start", {})
    approved = {"reference": provider[0]["body"]["reference"], "status": "approved"}
    assert _callback(kyc_methods.handle_callback, db, m, approved, "198.51.100.8")[0] == 403
    db.expire_all()
    assert db.get(Client, c["id"]).kyc_status == KycStatus.pending
    assert _callback(kyc_methods.handle_callback, db, m, approved, "198.51.100.7")[0] == 200
    db.expire_all()
    assert db.get(Client, c["id"]).kyc_status == KycStatus.approved


def test_callback_ip_whitelist_in_the_method_form(admin_web, db):
    r = form_post(admin_web, "/admin/kyc-methods/new", "/admin/kyc-methods/new", {
        "name": "IDV", "kind": "custom", "api_url": "https://kyc.example/api", "secret": "s3cret",
        "callback_ips": "203.0.113.9\nnot-an-ip"})
    assert "is not an IP address" in r.text
    r = form_post(admin_web, "/admin/kyc-methods/new", "/admin/kyc-methods/new", {
        "name": "IDV", "kind": "custom", "api_url": "https://kyc.example/api", "secret": "s3cret",
        "callback_ips": "203.0.113.9\n198.51.100.0/24"})
    m = db.scalar(select(kyc_methods.KycMethod).where(kyc_methods.KycMethod.name == "IDV"))
    assert m.callback_ips == ["203.0.113.9", "198.51.100.0/24"]
    assert "198.51.100.0/24" in admin_web.get(f"/admin/kyc-methods/{m.id}").text


# ---------------------------------------------------------------- account group deletion with accounts
@pytest.fixture
def group_with_accounts(db, gw, account_groups):
    other = AccountGroup(name="Pro", server_id=1, mt5_group="real\\pro", leverage_options=[100], default_leverage=100)
    db.add(other)
    db.commit()
    c = clients.register_client(db, None, email="acc@x.com", first_name="A", last_name="Cc", password=STRONG)
    approve_kyc_direct(db, c.id)
    logins = [open_live_direct(db, gw, c.id, account_groups["live"]) for _ in range(2)]
    return {"type": db.get(AccountGroup, account_groups["live"]), "other": other, "logins": logins}


def test_delete_group_moves_its_accounts(db, gw, group_with_accounts):
    t, other, logins = group_with_accounts["type"], group_with_accounts["other"], group_with_accounts["logins"]
    with pytest.raises(ServiceError, match="2 trading account"):
        accounts.delete_account_group(db, None, t)
    done = accounts.delete_account_group(db, None, t, "move", other.id)
    assert done["moved"] == 2
    assert all(gw.users[x].group == "real\\pro" for x in logins)
    assert {a.account_group_id for a in db.scalars(select(TradingAccount))} == {other.id}
    assert gw.get_group("real\\standard") is None


def test_force_delete_group_deletes_its_accounts(db, gw, group_with_accounts):
    from app.mt5 import BUY
    t, logins = group_with_accounts["type"], group_with_accounts["logins"]
    gw.balance_operation(logins[0], Decimal("100"), "funding")
    gw.open_position(logins[0], "EURUSD", BUY, Decimal("1"))
    with pytest.raises(ServiceError, match="open positions"):
        accounts.delete_account_group(db, None, t, "delete")
    pos = gw.get_positions([logins[0]])[0]
    gw.close_position(logins[0], pos.position_id, pos.symbol, pos.action, pos.volume)
    done = accounts.delete_account_group(db, None, t, "delete")
    assert done["deleted"] == 2 and not any(x in gw.users for x in logins)
    archived = db.scalars(select(TradingAccount)).all()
    assert {(a.status, a.on_server, a.account_group_id) for a in archived} == {(AccountStatus.archived, False, None)}


def test_delete_group_dialog_requires_typed_name(admin_web, db, group_with_accounts):
    t = group_with_accounts["type"]
    page = admin_web.get(f"/admin/accounts/account-groups/{t.id}")
    assert "Force delete all 2 account(s)" in page.text and 'data-bs-target="#delete-type"' in page.text
    r = form_post(admin_web, f"/admin/accounts/account-groups/{t.id}", f"/admin/accounts/account-groups/{t.id}/delete",
                  {"accounts_action": "delete", "confirm_name": "wrong"})
    assert "Type the account group name" in r.text
    r = form_post(admin_web, f"/admin/accounts/account-groups/{t.id}", f"/admin/accounts/account-groups/{t.id}/delete",
                  {"accounts_action": "move", "move_to": group_with_accounts["other"].id})
    assert "2 account(s) moved" in r.text


# ---------------------------------------------------------------- client <-> MT5
def test_client_changes_are_written_to_mt5(db, gw, group_with_accounts):
    login = group_with_accounts["logins"][0]
    c = db.get(Client, db.get(TradingAccount, 1).client_id)
    clients.update_client(db, None, c, {"first_name": "Anna", "phone": "+44 1", "city": "Leeds", "email": "ANNA@x.com"})
    u = gw.users[login]
    assert (u.name, u.first_name, u.phone, u.city, u.email) == ("Anna Cc", "Anna", "+44 1", "Leeds", "anna@x.com")
    clients.update_client(db, None, c, {"status": ClientStatus.suspended})
    assert not gw.users[login].enabled  # a suspended client cannot log in to MT5
    clients.update_client(db, None, c, {"status": ClientStatus.active})
    assert gw.users[login].enabled and c.mt5_sync_error is None and c.mt5_synced_at


def test_failed_mt5_update_is_kept_and_retried(db, gw, monkeypatch, group_with_accounts):
    from app.mt5 import MT5Error
    c = db.get(Client, db.get(TradingAccount, 1).client_id)
    real = gw.update_user

    def down(*a, **kw):
        raise MT5Error("network down")
    monkeypatch.setattr(gw, "update_user", down)
    clients.update_client(db, None, c, {"last_name": "Later"})
    assert c.last_name == "Later" and "network down" in c.mt5_sync_error  # saved in the CRM anyway
    monkeypatch.setattr(gw, "update_user", real)
    assert client_sync.retry_failed(db) == 1
    assert c.mt5_sync_error is None and gw.users[group_with_accounts["logins"][0]].last_name == "Later"


def test_demo_account_opened_at_sign_up(api, db, gw, account_groups):
    demo = db.get(AccountGroup, account_groups["demo"])
    accounts.update_account_group(db, None, demo, {"open_on_register": True})
    page = api.get("/portal/register")
    r = api.post("/portal/register", data={
        "email": "auto@x.com", "password": STRONG, "first_name": "Al", "last_name": "Auto", "country": "GB",
        "accept_terms": "on", "_csrf": csrf_of(page)})
    assert "Your accounts are ready" in r.text and "Trading password" in r.text
    acc = db.scalar(select(TradingAccount).join(Client).where(Client.email == "auto@x.com"))
    assert acc.is_demo and gw.users[acc.login].email == "auto@x.com"
    live = db.get(AccountGroup, account_groups["live"])
    with pytest.raises(ServiceError, match="Only demo"):
        accounts.update_account_group(db, None, live, {"open_on_register": True})


# ---------------------------------------------------------------- sandbox provider, end to end
def test_sandbox_provider_end_to_end(api, db, monkeypatch):
    """The demo's built-in provider, with its HTTP calls routed through the test client."""
    def via_app(url, body, headers):
        r = api.post(urlparse(url).path, content=body, headers=headers)
        return r.status_code, r.text
    monkeypatch.setattr(integrations, "http_post", via_app)
    m = payment_methods.create_method(db, None, name="Sandbox card", kind="custom",
                                      api_url="http://testserver/sandbox/provider/start", secret="sandbox-secret")
    client = _client(api, db, "sbx@x.com")
    r = form_post(api, "/portal/wallet", "/portal/wallet/deposit", {"method_id": m.id, "amount": "75"})
    page_url = urlparse(db.scalar(select(PaymentRequest.gateway_url))).path
    assert page_url in r.text
    page = api.get(page_url)
    assert "Pay 75.00 USD" in page.text
    r = api.post(page_url, data={"decision": "approve"}, follow_redirects=False)
    assert r.status_code == 303  # back to the CRM's return URL
    r = api.get(urlparse(r.headers["location"]).path, follow_redirects=True)
    assert "has been credited" in r.text
    assert wallet.get_wallet(db, client.id).balance == Decimal("75")
