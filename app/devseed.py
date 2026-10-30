"""Realistic demo data for local development (simulated MT5 only).

Everything goes through the normal services, so the data is exactly what real usage
would produce: audit trail, ledgers, copied trades, commissions, NAV history.
"""

import logging
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AllocationMode, KycDocument, KycStatus, MT5Server, PaymentKind, Role, RuleKind, ServerKind, Staff
from app.mt5 import BUY, SELL, for_account_group, get_gateway, routed
from app.services import (
    accounts, campaigns, clients, commissions, copy_trading, pamm, payments, servers, staff, sync,
)

log = logging.getLogger("devseed")

ADMIN_PASSWORD = "Admin-Pass1"
STAFF_PASSWORD = "Staff-Pass1"
CLIENT_PASSWORD = "Client-Pass1"
DOMAIN = "demo-broker.com"

PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj 2 0 obj<</Type/Pages/Count 0/Kids[]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF"
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a1d50000000049454e44ae426082")


def _demo_server(db) -> MT5Server:
    """The simulated MT5 server: live and demo accounts on one server, in real\* and demo\* groups."""
    server = db.scalar(select(MT5Server).order_by(MT5Server.id).limit(1)) or servers.create_server(
        db, None, name="MT5 server (simulated)", kind=ServerKind.simulator, terminal_name="Flow-Server")
    db.commit()
    return server


def seed() -> dict:
    with SessionLocal() as db:
        if db.scalar(select(Staff.id)):
            return {}
        log.info("seeding demo data...")
        staff.seed_roles(db)
        live_server = demo_server = _demo_server(db)  # one MT5 server for live and demo accounts
        gw = routed()  # every login-based call goes to the account's own server
        live_gw = get_gateway(live_server.id)
        role = {r.name: r.id for r in db.scalars(select(Role))}

        # ------------------------------------------------------------ staff
        deps = {n: staff.create_department(db, None, n).id for n in ("Management", "Sales", "Finance", "Support", "Dealing")}
        admin = staff.create_staff(db, None, email=f"admin@{DOMAIN}", full_name="Alex Admin", password=ADMIN_PASSWORD,
                                   role_ids=[role["Admin"]], department_id=deps["Management"])
        mgr = staff.create_staff(db, admin, email=f"manager@{DOMAIN}", full_name="Morgan Lee", password=STAFF_PASSWORD,
                                 role_ids=[role["Manager"]], department_id=deps["Management"])
        sam = staff.create_staff(db, admin, email=f"sam@{DOMAIN}", full_name="Sam Carter", password=STAFF_PASSWORD,
                                 role_ids=[role["Sales"]], department_id=deps["Sales"], manager_id=mgr.id)
        sara = staff.create_staff(db, admin, email=f"sara@{DOMAIN}", full_name="Sara Novak", password=STAFF_PASSWORD,
                                  role_ids=[role["Sales"]], department_id=deps["Sales"], manager_id=mgr.id)
        fin = staff.create_staff(db, admin, email=f"finance@{DOMAIN}", full_name="Fiona Grant", password=STAFF_PASSWORD,
                                 role_ids=[role["Finance"]], department_id=deps["Finance"])
        staff.create_staff(db, admin, email=f"support@{DOMAIN}", full_name="Sunny Park", password=STAFF_PASSWORD,
                           role_ids=[role["Support"]], department_id=deps["Support"])
        staff.create_staff(db, admin, email=f"dealer@{DOMAIN}", full_name="Dan Mercer", password=STAFF_PASSWORD,
                           role_ids=[role["Dealer"]], department_id=deps["Dealing"])

        # ------------------------------------------------------------ products
        groups = {}
        for t in (dict(name="Standard", mt5_group="real\\standard", spread_markup_points=10, leverage_options=[50, 100, 200, 500],
                       default_leverage=200, min_deposit=Decimal("100"), description="No commission, spread from 1.0 pip"),
                  dict(name="ECN", mt5_group="real\\ecn", leverage_options=[50, 100, 200], default_leverage=100,
                       min_deposit=Decimal("1000"), description="Raw spreads + $7 per lot"),
                  dict(name="Demo", mt5_group="demo\\standard", is_demo=True, leverage_options=[100, 500],
                       default_leverage=500, description="Practice with $10,000 virtual funds")):
            t["server_id"] = demo_server.id if t.get("is_demo") else live_server.id
            groups[t["name"]] = accounts.create_account_group(db, admin, **t).id

        premium = commissions.create_plan(db, admin, "Premium IB", [0.7, 0.2, 0.1], [
            {"symbol_pattern": "XAU*", "kind": RuleKind.per_lot, "value": Decimal("8"), "priority": 10},
            {"symbol_pattern": "*", "kind": RuleKind.per_lot, "value": Decimal("6"), "priority": 100}])
        standard_plan = commissions.create_plan(db, admin, "Standard IB", [0.8, 0.2], [
            {"symbol_pattern": "XAU*", "kind": RuleKind.per_lot, "value": Decimal("5"), "priority": 10},
            {"symbol_pattern": "*", "kind": RuleKind.spread_share, "value": Decimal("0.5"), "priority": 100}])
        campaigns.create_campaign(db, admin, name="Welcome bonus", code="WELCOME50", bonus_pct=Decimal("50"),
                                  max_bonus=Decimal("500"), min_deposit=Decimal("100"))
        campaigns.create_campaign(db, admin, name="Gold rush", code="GOLD20", bonus_pct=Decimal("20"),
                                  max_bonus=Decimal("1000"), min_deposit=Decimal("500"), max_redemptions=100)

        # ------------------------------------------------------------ clients
        def person(first, last, country, sales, ref=None, kyc=KycStatus.approved, utm=None, phone="+44 20 7946 0123"):
            c = clients.register_client(db, None, email=f"{first.lower()}.{last.lower().replace(chr(39), '')}@example.com",
                                        first_name=first, last_name=last, password=CLIENT_PASSWORD, country=country,
                                        phone=phone, referral_code=ref.referral_code if ref else None,
                                        utm_source=(utm or {}).get("source"), utm_medium=(utm or {}).get("medium"),
                                        utm_campaign=(utm or {}).get("campaign"))
            clients.assign_client(db, admin, c, sales.id, mgr.id if kyc == KycStatus.approved else None)
            if kyc != KycStatus.not_submitted:
                clients.upload_kyc_document(db, c, c, "passport", f"{last.lower()}_passport.pdf", PDF)
                clients.upload_kyc_document(db, c, c, "proof_of_address", "utility_bill.png", PNG)
            if kyc in (KycStatus.approved, KycStatus.rejected):
                docs = db.scalars(select(KycDocument).where(KycDocument.client_id == c.id)).all()
                for d in docs:
                    clients.review_kyc_document(db, mgr, d, kyc == KycStatus.approved,
                                                None if kyc == KycStatus.approved else "Document is blurry, please re-upload")
                clients.set_kyc_status(db, mgr, c, kyc)
            return c

        def live(c, group_name="Standard", leverage=None):
            return accounts.open_account(db, for_account_group(db, groups[group_name]), sam, c, groups[group_name],
                                         leverage)["account"].login

        def fund_mt5(c, login, amount, promo=None):
            req = payments.create_request(db, c, c, PaymentKind.deposit, Decimal(amount), method="bank",
                                          trading_login=login, external_ref=f"WIRE-{login}-{int(amount)}", promo_code=promo)
            payments.approve(db, None, fin, req, "Funds received")

        def fund_wallet(c, amount):
            req = payments.create_request(db, c, c, PaymentKind.deposit, Decimal(amount), method="bank",
                                          external_ref=f"WIRE-W{c.id}-{int(amount)}")
            payments.approve(db, None, fin, req, "Funds received")

        priya = person("Priya", "Shah", "IN", sam, utm={"source": "partners", "medium": "referral", "campaign": "ib-program"})
        liam = person("Liam", "OConnor", "IE", sam, ref=priya)
        ib1 = commissions.make_ib(db, admin, priya, premium.id)
        ib2 = commissions.make_ib(db, admin, liam, standard_plan.id)  # parent inferred: Priya

        jane = person("Jane", "Doe", "GB", sam, ref=priya, utm={"source": "google", "medium": "cpc", "campaign": "spring-fx"})
        marco = person("Marco", "Rossi", "IT", sam, ref=liam, phone="+39 06 1234 5678")
        kenji = person("Kenji", "Watanabe", "JP", sam, phone="+81 3 1234 5678")
        sofia = person("Sofia", "Lopez", "ES", sam, phone="+34 91 123 4567")
        ahmed = person("Ahmed", "Hassan", "AE", sara, ref=priya, utm={"source": "facebook", "medium": "social", "campaign": "gold-promo"})
        chen = person("Chen", "Wei", "SG", sara, utm={"source": "google", "medium": "cpc", "campaign": "spring-fx"})
        emma = person("Emma", "Schmidt", "DE", sara, kyc=KycStatus.pending, utm={"source": "facebook", "medium": "social", "campaign": "gold-promo"})
        lucas = person("Lucas", "Silva", "BR", sara, kyc=KycStatus.rejected)
        noah = person("Noah", "Kim", "KR", sara, kyc=KycStatus.not_submitted)
        olivia = clients.register_client(db, sara, email="olivia.brown@example.com", first_name="Olivia", last_name="Brown",
                                         country="AU", phone="+61 2 9876 5432", lead_source="trade-show", sales_staff_id=sara.id)
        clients.add_note(db, sara, olivia, "Met at the Sydney trading expo. Interested in gold and copy trading. Call back next week.")
        clients.add_note(db, sam, jane, "Onboarding call done. Wants to copy Kenji's strategy with part of her account.")
        clients.add_note(db, sam, marco, "Prefers ECN. Asked about PAMM performance fees - sent the fund factsheet.")
        clients.add_note(db, sara, lucas, "Passport scan unreadable. Emailed instructions for re-upload.")

        # ------------------------------------------------------------ accounts & money
        jl = live(jane)
        ml = live(marco, "ECN")
        kl = live(kenji, "Standard", 500)
        sl = live(sofia)
        al = live(ahmed)
        cl = live(chen, "ECN")
        for c in (jane, emma, noah):
            accounts.open_account(db, for_account_group(db, groups["Demo"]), c, c, groups["Demo"])
        fund_mt5(jane, jl, 20000, promo="WELCOME50")
        fund_mt5(marco, ml, 12000)
        fund_mt5(kenji, kl, 50000)
        fund_mt5(ahmed, al, 8000, promo="GOLD20")
        fund_mt5(chen, cl, 30000)
        fund_wallet(marco, 5000)
        fund_wallet(chen, 10000)
        fund_wallet(jane, 3000)

        # ------------------------------------------------------------ copy trading
        strat = copy_trading.create_strategy(db, admin, kenji, kl, "Kenji Momentum",
                                             description="Trend-following on majors and gold. Avg. hold 1-3 days.",
                                             performance_fee_pct=Decimal("20"), min_investment=Decimal("500"))
        copy_trading.subscribe(db, gw, jane, jane, strat, jl, AllocationMode.equity_ratio, Decimal("1"))
        copy_trading.subscribe(db, gw, ahmed, ahmed, strat, al, AllocationMode.lot_multiplier, Decimal("0.3"), Decimal("1"))

        # ------------------------------------------------------------ PAMM
        fund = pamm.create_fund(db, gw, admin, sofia, sl, "Lopez Global Macro",
                                description="Diversified macro fund: FX majors and gold, max drawdown target 15%.",
                                performance_fee_pct=Decimal("25"), management_fee_pct_annual=Decimal("2"),
                                min_investment=Decimal("1000"))
        pamm.invest(db, gw, marco, fund, marco, 4000)
        pamm.invest(db, gw, chen, fund, chen, 8000)
        pamm.snapshot(db, gw, fund)

        # ------------------------------------------------------------ trading history
        def trade(login, symbol, side, lots, move):
            res = live_gw.open_position(login, symbol, side, Decimal(lots))
            sync.sync_deals(db, live_gw, server_id=live_server.id)  # copy engine + commissions react
            bid, ask = live_gw.prices[symbol]
            live_gw.set_price(symbol, bid + Decimal(move), ask + Decimal(move))
            return res

        def close(login, res, lots=None):
            pos = live_gw.positions.get(res.position_id)
            if pos:
                live_gw.close_position(login, res.position_id, pos.symbol, pos.action,
                                       Decimal(lots) if lots else pos.volume)
                sync.sync_deals(db, live_gw, server_id=live_server.id)

        t1 = trade(kl, "EURUSD", BUY, "2", "0.0035")
        close(kl, t1, "1")                      # partial close, mirrored on followers
        close(kl, t1)
        t2 = trade(kl, "XAUUSD", BUY, "1", "6.5")
        close(kl, t2)
        t3 = trade(ml, "GBPUSD", SELL, "1.5", "-0.0020")
        close(ml, t3)
        t4 = trade(cl, "XAUUSD", SELL, "2", "-4.2")
        close(cl, t4)
        t5 = trade(sl, "EURUSD", BUY, "3", "0.0018")
        close(sl, t5)
        pamm.snapshot(db, gw, fund)
        commissions.payout(db, fin)             # first payout run
        # open positions left running for the live view
        trade(kl, "GBPUSD", BUY, "1.5", "0.0006")
        trade(kl, "XAUUSD", SELL, "0.8", "-1.1")
        trade(ml, "EURUSD", SELL, "2", "-0.0004")
        trade(cl, "XAUUSD", BUY, "1", "0.9")
        trade(sl, "GBPUSD", SELL, "2", "-0.0007")
        p6 = trade(al, "EURUSD", BUY, "0.5", "0.0002")
        close(al, p6)
        sync.full_sync(db)
        pamm.snapshot(db, gw, fund)

        # ------------------------------------------------------------ open work for the queues
        payments.create_request(db, marco, marco, PaymentKind.deposit, Decimal("2500"), method="bank", trading_login=ml,
                                external_ref="WIRE-88213")
        payments.create_request(db, jane, jane, PaymentKind.withdrawal, Decimal("1200"),
                                details="Barclays, GB33 BUKB 2020 1555 5555 55, Jane Doe")
        payments.create_request(db, chen, chen, PaymentKind.deposit, Decimal("15000"), method="crypto", trading_login=cl,
                                external_ref="USDT-TRC20 7f3a...c91e")
        rej = payments.create_request(db, ahmed, ahmed, PaymentKind.withdrawal, Decimal("900"), trading_login=al,
                                      details="Card ending 4421")
        payments.reject(db, fin, rej, "Withdrawals must go back to the original deposit method (bank transfer)")
        staff_req = payments.create_request(db, sam, chen, PaymentKind.deposit, Decimal("750"), method="card",
                                            trading_login=cl, external_ref="PSP-CH-4471")
        db.commit()
        log.info("demo data seeded")
        return {"strategy": strat.id, "fund": fund.id, "masters": [kl, sl], "traders": [ml, cl],
                "pending_staff_request": staff_req.id}


def ensure_methods() -> list[str]:
    """Demo payment and KYC methods (added once, when none exist yet). The custom ones use the
    built-in sandbox provider, so the whole provider round trip can be tried locally."""
    from app.config import get_settings
    from app.models import KycMethod, PaymentMethod
    from app.services import kyc_methods, payment_methods

    base = get_settings().public_url.rstrip("/")
    added = []
    with SessionLocal() as db:
        if not db.scalar(select(PaymentMethod.id)):
            payment_methods.create_method(
                db, None, name="Bank transfer", kind="manual", icon="bank", sort_order=10,
                description="1-3 business days, no fee", requires_proof=True, allow_withdrawal=True,
                instructions=get_settings().deposit_instructions)
            payment_methods.create_method(
                db, None, name="USDT (TRC20)", kind="manual", icon="currency-bitcoin", sort_order=30,
                description="Arrives after 20 confirmations", allow_withdrawal=True,
                instructions="Send USDT on TRON (TRC20) to TXk3demo0address00000000000000000 and enter the transaction hash.")
            payment_methods.create_method(
                db, None, name="Card (sandbox)", kind="custom", icon="credit-card", sort_order=20,
                description="Visa / Mastercard, instant", api_url=f"{base}/sandbox/provider/start",
                secret="sandbox-secret", min_amount=Decimal("10"), max_amount=Decimal("10000"))
            added.append("payment methods")
        if not db.scalar(select(KycMethod.id)):
            manual = kyc_methods.create_method(
                db, None, name="Document review", kind="manual", required_documents=["identity", "proof_of_address"],
                instructions="Upload a colour photo or scan with all four corners visible. Proof of address: "
                             "issued within the last 3 months.")
            kyc_methods.activate(db, None, manual)
            kyc_methods.create_method(
                db, None, name="Online verification (sandbox)", kind="custom", api_url=f"{base}/sandbox/provider/start",
                secret="sandbox-secret", description="Verify your identity online in about two minutes.")
            added.append("KYC methods")
    return added


def demo_masters(db) -> list[int]:
    from app.models import AccountPurpose, TradingAccount

    return list(db.scalars(select(TradingAccount.login).where(
        TradingAccount.purpose.in_([AccountPurpose.copy_master, AccountPurpose.pamm_master]))))
