from decimal import Decimal

from sqlalchemy import select

from app.models import Role
from tests.conftest import approve_kyc, login_client, login_staff, open_live, register


# ---------------------------------------------------------------- clients
def test_referral_registration(api, admin_h):
    parent = register(api, "parent@x.com")
    child = register(api, "child@x.com", ref=parent["referral_code"], utm_campaign="summer")
    assert child["referred_by_id"] == parent["id"]
    assert child["lead_source"] == "referral"
    assert api.post("/api/auth/client/register", json={
        "email": "bad@x.com", "password": "Client-Pass1", "first_name": "A", "last_name": "B",
        "referral_code": "NOPE1234"}).status_code == 400
    sources = api.get("/api/marketing/sources", headers=admin_h).json()
    assert any(s["utm_campaign"] == "summer" for s in sources)


def test_sales_sees_only_assigned_clients(api, admin_h, db):
    role = db.scalar(select(Role).where(Role.name == "Sales"))
    sales = api.post("/api/staff", json={"email": "s@b.com", "full_name": "S", "password": "Sales-Pass1",
                                         "role_ids": [role.id]}, headers=admin_h).json()
    mine, other = register(api, "mine@x.com"), register(api, "other@x.com")
    api.put(f"/api/clients/{mine['id']}/assignment", json={"sales_staff_id": sales["id"]}, headers=admin_h)
    sales_h = login_staff(api, "s@b.com", "Sales-Pass1")
    ids = [c["id"] for c in api.get("/api/clients", headers=sales_h).json()]
    assert ids == [mine["id"]]
    assert api.get(f"/api/clients/{other['id']}", headers=sales_h).status_code == 403


def test_business_development_sees_their_referral_tree(api, admin_h, db, account_groups):
    from app.models import Staff
    from app.services.clients import staff_referral_code

    role = db.scalar(select(Role).where(Role.name == "Business Development"))
    bd = api.post("/api/staff", json={"email": "bd@b.com", "full_name": "Bea Dev", "password": "Sales-Pass1",
                                      "role_ids": [role.id]}, headers=admin_h).json()
    code = staff_referral_code(db, db.get(Staff, bd["id"]))
    ib = register(api, "ib@x.com", ref=code.lower())  # their link: the first IB signs up with it
    assert ib["sales_staff_id"] == bd["id"] and ib["referred_by_id"] is None
    child = register(api, "child@x.com", ref=ib["referral_code"])
    grandchild = register(api, "grand@x.com", ref=child["referral_code"])
    other = register(api, "other@x.com")
    # someone below moved to another sales owner is still in the tree
    api.put(f"/api/clients/{grandchild['id']}/assignment", json={"sales_staff_id": None}, headers=admin_h)
    login = open_live(api, admin_h, grandchild["id"], account_groups["demo"])

    bd_h = login_staff(api, "bd@b.com", "Sales-Pass1")
    ids = {c["id"] for c in api.get("/api/clients", headers=bd_h).json()}
    assert ids == {ib["id"], child["id"], grandchild["id"]}
    assert api.get(f"/api/clients/{other['id']}", headers=bd_h).status_code == 403
    assert api.get(f"/api/accounts/{login}", headers=bd_h).status_code == 200


def test_business_development_tagged_links_trace_sign_ups(api, admin_h, db):
    import pytest

    from app.core.errors import Conflict, ServiceError
    from app.models import Client, Staff
    from app.services.clients import create_referral_link, delete_referral_link

    role = db.scalar(select(Role).where(Role.name == "Business Development"))
    bd_id = api.post("/api/staff", json={"email": "bd2@b.com", "full_name": "Bo Dev", "password": "Sales-Pass1",
                                         "role_ids": [role.id]}, headers=admin_h).json()["id"]
    bd = db.get(Staff, bd_id)
    expo = create_referral_link(db, bd, " Dubai  expo ")
    fb = create_referral_link(db, bd, "Facebook group")
    assert expo.tag == "Dubai expo" and expo.code != fb.code
    with pytest.raises(Conflict):
        create_referral_link(db, bd, "Dubai expo")
    a = register(api, "expo1@x.com", ref=expo.code)
    register(api, "expo2@x.com", ref=expo.code)
    assert a["sales_staff_id"] == bd_id and a["lead_source"] == "business development"
    assert db.get(Client, a["id"]).referral_link_id == expo.id
    with pytest.raises(ServiceError):  # people signed up with it: it stays
        delete_referral_link(db, bd, expo.id)
    delete_referral_link(db, bd, fb.id)
    assert api.post("/api/auth/client/register", json={
        "email": "late@x.com", "password": "Client-Pass1", "first_name": "A", "last_name": "B",
        "referral_code": fb.code}).status_code == 400


def test_kyc_upload_and_review(api, admin_h):
    c = register(api, "kyc@x.com")
    ch = login_client(api, "kyc@x.com")
    r = api.post("/api/portal/kyc", data={"doc_type": "passport"},
                 files={"file": ("passport.pdf", b"%PDF-1.4 test", "application/pdf")}, headers=ch)
    assert r.status_code == 201, r.text
    doc = r.json()
    assert api.get("/api/portal/me", headers=ch).json()["kyc_status"] == "pending"
    bad = api.post("/api/portal/kyc", data={"doc_type": "passport"},
                   files={"file": ("evil.exe", b"MZ", "application/octet-stream")}, headers=ch)
    assert bad.status_code == 400
    assert api.post(f"/api/clients/kyc/{doc['id']}/review", json={"approve": False}, headers=admin_h).status_code == 400
    r = api.post(f"/api/clients/kyc/{doc['id']}/review", json={"approve": True}, headers=admin_h)
    assert r.json()["status"] == "approved"
    approve_kyc(api, admin_h, c["id"])
    assert api.get("/api/portal/me", headers=ch).json()["kyc_status"] == "approved"


# ---------------------------------------------------------------- accounts
def test_live_account_requires_kyc(api, admin_h, account_groups):
    c = register(api, "nokyc@x.com")
    r = api.post("/api/accounts", json={"client_id": c["id"], "account_group_id": account_groups["live"]}, headers=admin_h)
    assert r.status_code == 403
    # demo is fine without KYC, and gets its initial balance
    ch = login_client(api, "nokyc@x.com")
    r = api.post("/api/portal/accounts", json={"account_group_id": account_groups["demo"]}, headers=ch)
    assert r.status_code == 201, r.text
    assert Decimal(r.json()["account"]["balance"]) == Decimal("10000")


def test_account_management(api, admin_h, account_groups, gw):
    c = register(api, "acc@x.com")
    approve_kyc(api, admin_h, c["id"])
    r = api.post("/api/accounts", json={"client_id": c["id"], "account_group_id": account_groups["live"],
                                        "leverage": 200}, headers=admin_h)
    body = r.json()
    login = body["account"]["login"]
    assert gw.check_password(login, body["master_password"])
    assert gw.users[login].group == "real\\standard" and gw.users[login].leverage == 200

    assert api.put(f"/api/accounts/{login}/leverage", json={"leverage": 300}, headers=admin_h).status_code == 400
    assert api.put(f"/api/accounts/{login}/leverage", json={"leverage": 500}, headers=admin_h).status_code == 200
    assert gw.users[login].leverage == 500

    pwd = api.post(f"/api/accounts/{login}/password", json={"investor": True}, headers=admin_h).json()["password"]
    assert gw.check_password(login, pwd, investor=True)

    api.put(f"/api/accounts/{login}/trading", json={"enabled": False}, headers=admin_h)
    assert gw.users[login].trading_enabled is False
    assert not gw.open_position(login, "EURUSD", 0, Decimal("0.1")).ok


def test_archive_requires_empty_account(api, admin_h, funded):
    _, login = funded("arch@x.com", amount=100)
    assert api.put(f"/api/accounts/{login}/status", json={"status": "archived"}, headers=admin_h).status_code == 400


# ---------------------------------------------------------------- finance
def test_deposit_maker_checker_and_promo(api, admin_h, db, account_groups, gw):
    c = register(api, "dep@x.com")
    approve_kyc(api, admin_h, c["id"])
    login = open_live(api, admin_h, c["id"], account_groups["live"])
    api.post("/api/campaigns", json={"name": "W", "code": "WELCOME50", "bonus_pct": "50", "max_bonus": "300",
                                     "min_deposit": "100"}, headers=admin_h)
    r = api.post("/api/payments", json={"client_id": c["id"], "kind": "deposit", "amount": "1000",
                                        "trading_login": login, "promo_code": "welcome50"}, headers=admin_h)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    # maker cannot approve own request
    assert api.post(f"/api/payments/{pid}/approve", json={}, headers=admin_h).status_code == 403

    role = db.scalar(select(Role).where(Role.name == "Finance"))
    api.post("/api/staff", json={"email": "fin@b.com", "full_name": "F", "password": "Finance-Pass1",
                                 "role_ids": [role.id]}, headers=admin_h)
    fin_h = login_staff(api, "fin@b.com", "Finance-Pass1")
    r = api.post(f"/api/payments/{pid}/approve", json={"comment": "ok"}, headers=fin_h)
    assert r.status_code == 200, r.text
    assert gw.balance[login] == Decimal("1000")
    assert gw.credit[login] == Decimal("300")  # 50% capped at 300
    # once-per-client promo
    r = api.post("/api/payments", json={"client_id": c["id"], "kind": "deposit", "amount": "500",
                                        "trading_login": login, "promo_code": "WELCOME50"}, headers=admin_h)
    assert r.status_code == 400


def test_wallet_flow_and_withdrawal_hold(api, admin_h, account_groups, gw, db):
    from app.services import payment_methods

    bank = payment_methods.create_method(db, None, name="Bank", kind="manual", allow_withdrawal=True).id
    c = register(api, "wal@x.com")
    approve_kyc(api, admin_h, c["id"])
    login = open_live(api, admin_h, c["id"], account_groups["live"])
    ch = login_client(api, "wal@x.com")

    # client requests a wallet deposit; staff approves (client is the maker, so admin may check)
    pid = api.post("/api/portal/payments", json={"kind": "deposit", "amount": "500", "method_id": bank},
                   headers=ch).json()["id"]
    api.post(f"/api/payments/{pid}/approve", json={}, headers=admin_h)
    assert Decimal(api.get("/api/portal/wallet", headers=ch).json()["balance"]) == Decimal("500")

    assert api.post("/api/portal/transfer/to-mt5", json={"login": login, "amount": "600"}, headers=ch).status_code == 400
    assert api.post("/api/portal/transfer/to-mt5", json={"login": login, "amount": "400"}, headers=ch).status_code == 200
    assert gw.balance[login] == Decimal("400")
    api.post("/api/portal/transfer/from-mt5", json={"login": login, "amount": "150"}, headers=ch)
    assert gw.balance[login] == Decimal("250")
    assert Decimal(api.get("/api/portal/wallet", headers=ch).json()["balance"]) == Decimal("250")

    # withdrawal holds funds immediately; rejection refunds them
    wid = api.post("/api/portal/payments", json={"kind": "withdrawal", "amount": "200", "method_id": bank},
                   headers=ch).json()["id"]
    assert Decimal(api.get("/api/portal/wallet", headers=ch).json()["balance"]) == Decimal("50")
    assert api.post(f"/api/payments/{wid}/reject", json={}, headers=admin_h).status_code == 400
    api.post(f"/api/payments/{wid}/reject", json={"comment": "bank details mismatch"}, headers=admin_h)
    assert Decimal(api.get("/api/portal/wallet", headers=ch).json()["balance"]) == Decimal("250")

    txs = api.get("/api/portal/wallet/transactions", headers=ch).json()
    assert Decimal(txs[0]["balance_after"]) == Decimal("250")
    assert sum(Decimal(t["amount"]) for t in txs) == Decimal("250")  # ledger reconciles


def test_other_clients_account_forbidden(api, funded):
    funded("owner@x.com")
    _, other_login = funded("victim@x.com")
    ch = login_client(api, "owner@x.com")
    r = api.post("/api/portal/transfer/from-mt5", json={"login": other_login, "amount": "10"}, headers=ch)
    assert r.status_code == 403
