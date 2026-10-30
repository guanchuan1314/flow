"""Automation: rules the CRM runs on its own data (nothing synced to MT5)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.errors import ServiceError
from app.db import utcnow
from app.models import (
    AccountGroup, AutomationRule, AutomationRun, Client, ClientNote, KycStatus, PaymentKind, PaymentRequest,
    PaymentStatus, TradingAccount,
)
from app.mt5 import BUY
from app.services import accounts, automation, clients, sync
from app.services import notifications
from tests.test_web import admin_web, form_post  # noqa: F401


def client(db, email, **extra):
    c = clients.register_client(db, None, email=email, first_name="Ann", last_name="Lee", password="Client-Pass1")
    c.kyc_status = KycStatus.approved
    for k, v in extra.items():
        setattr(c, k, v)
    db.commit()
    return c


def account(db, gw, account_groups, email, kind="live"):
    c = client(db, email)
    return accounts.open_account(db, gw, None, c, account_groups[kind])["account"]


def rule(db, trigger, action="client_note", params=None, action_params=None, cooldown=24, enabled=True, **filters):
    n = db.scalar(select(func.count()).select_from(AutomationRule)) + 1
    r = automation.save_rule(db, None, None, name=f"Rule {n}", trigger=trigger, params=params or {},
                             filters={"kind": "live", **filters}, action=action,
                             action_params=action_params or {"message": "Seen: {detail}"}, cooldown_hours=cooldown)
    if enabled:
        automation.set_enabled(db, None, r, True)
    return r


def targets(db, r):
    return {m["target"] for m in automation.matches_now(db, r)}


# ---------------------------------------------------------------- triggers
def test_inactive_accounts(db, gw, account_groups):
    old = account(db, gw, account_groups, "old@x.com")
    new = account(db, gw, account_groups, "new@x.com")
    db.get(TradingAccount, old.id).last_activity_at = utcnow() - timedelta(days=40)
    db.get(TradingAccount, new.id).last_activity_at = utcnow()
    db.commit()
    assert targets(db, rule(db, "inactive", params={"days": 30})) == {f"account:{old.login}"}


def test_many_deals_margin_and_equity(db, gw, account_groups):
    busy = account(db, gw, account_groups, "busy@x.com")
    gw.balance_operation(busy.login, Decimal("1000"), "funding")
    for _ in range(3):
        gw.open_position(busy.login, "EURUSD", BUY, Decimal("0.1"))
    sync.full_sync(db, gw)
    assert targets(db, rule(db, "many_deals", params={"deals": 3, "hours": 1})) == {f"account:{busy.login}"}
    assert targets(db, rule(db, "many_deals", params={"deals": 4, "hours": 1})) == set()

    acc = db.scalar(select(TradingAccount).where(TradingAccount.login == busy.login))
    acc.margin, acc.margin_level, acc.equity = Decimal("100"), Decimal("120"), Decimal("40")
    db.commit()
    assert targets(db, rule(db, "margin_level", params={"percent": 150})) == {f"account:{busy.login}"}
    assert targets(db, rule(db, "equity_below", params={"amount": 50})) == {f"account:{busy.login}"}
    assert targets(db, rule(db, "equity_below", params={"amount": 30})) == set()


def test_filters_kind_and_groups(db, gw, account_groups):
    live = account(db, gw, account_groups, "l@x.com")
    demo = account(db, gw, account_groups, "d@x.com", "demo")
    db.query(TradingAccount).update({TradingAccount.last_activity_at: utcnow() - timedelta(days=99)})
    db.commit()
    assert targets(db, rule(db, "inactive", params={"days": 30})) == {f"account:{live.login}"}
    assert targets(db, rule(db, "inactive", "email_client", params={"days": 30}, kind="demo",
                            action_params={"subject": "Hi", "message": "x"})) == {f"account:{demo.login}"}
    assert len(targets(db, rule(db, "inactive", "notify_staff", params={"days": 30}, kind="any"))) == 2
    assert targets(db, rule(db, "inactive", "change_leverage", params={"days": 30}, kind="any", groups="demo\\*",
                            action_params={"leverage": 100})) == {f"account:{demo.login}"}


def test_trader_style(db, gw, account_groups):
    acc = account(db, gw, account_groups, "ea@x.com")
    gw.balance_operation(acc.login, Decimal("1000"), "funding")
    gw.deal_reasons[acc.login] = 1  # placed by an Expert Advisor
    for _ in range(12):
        gw.open_position(acc.login, "EURUSD", BUY, Decimal("0.1"))
    sync.full_sync(db, gw)
    assert targets(db, rule(db, "trader_style", params={"style": "ea", "days": 30, "min_deals": 10})) == \
        {f"account:{acc.login}"}
    assert targets(db, rule(db, "trader_style", "notify_staff", params={"style": "manual", "days": 30,
                                                                         "min_deals": 10})) == set()


def test_client_triggers(db):
    waiting = client(db, "w@x.com", kyc_status=KycStatus.pending)
    paid = client(db, "p@x.com")
    fresh = client(db, "f@x.com")
    for c in (waiting, paid):
        c.created_at = c.updated_at = utcnow() - timedelta(days=10)
    db.add(PaymentRequest(client_id=paid.id, kind=PaymentKind.deposit, amount=Decimal("100"),
                          status=PaymentStatus.approved))
    db.commit()
    assert targets(db, rule(db, "no_deposit", params={"days": 7})) == {f"client:{waiting.id}"}
    assert targets(db, rule(db, "kyc_pending", "notify_staff", params={"days": 2})) == {f"client:{waiting.id}"}
    assert fresh.id  # registered today: not yet


# ---------------------------------------------------------------- acting
def test_actions_cooldown_and_log(db, gw, account_groups):
    acc = account(db, gw, account_groups, "act@x.com")
    db.get(TradingAccount, acc.id).last_activity_at = utcnow() - timedelta(days=40)
    db.commit()
    r = rule(db, "inactive", params={"days": 30}, action_params={"message": "Quiet: {detail} on {login}"})
    assert automation.run_rule(db, r, None, gw) == {"matched": 1, "done": 1, "failed": 0, "skipped": 0}
    note = db.scalar(select(ClientNote).where(ClientNote.client_id == acc.client_id))
    assert note.body.startswith(f"[{r.name}] Quiet: last activity") and str(acc.login) in note.body
    assert automation.run_rule(db, r, None, gw)["skipped"] == 1  # cooldown: not twice
    run = db.scalar(select(AutomationRun))
    run.at -= timedelta(hours=25)  # after the cooldown it acts again
    db.commit()
    assert automation.run_rule(db, r, None, gw)["done"] == 1

    once = rule(db, "inactive", "disable_trading", params={"days": 30}, cooldown=0, action_params={})
    assert automation.run_rule(db, once, None, gw)["done"] == 1 and gw.users[acc.login].trading_enabled is False
    db.query(AutomationRun).update({AutomationRun.at: utcnow() - timedelta(days=999)})
    db.commit()
    assert automation.run_rule(db, once, None, gw)["skipped"] == 1  # cooldown 0: once per target, ever


def test_mt5_actions_and_failures(db, gw, account_groups):
    acc = account(db, gw, account_groups, "mt5@x.com")
    db.get(TradingAccount, acc.id).last_activity_at = utcnow() - timedelta(days=40)
    ecn = AccountGroup(name="ECN", server_id=1, mt5_group="real\\ecn", leverage_options=[100, 200])
    db.add(ecn)
    db.commit()
    move = rule(db, "inactive", "change_group", params={"days": 30}, action_params={"account_group_id": ecn.id})
    assert automation.run_rule(db, move, None, gw)["done"] == 1 and gw.users[acc.login].group == "real\\ecn"
    lev = rule(db, "inactive", "change_leverage", params={"days": 30}, action_params={"leverage": 300})
    assert automation.run_rule(db, lev, None, gw)["failed"] == 1  # 1:300 isn't offered: refused, logged
    failed = db.scalar(select(AutomationRun).where(AutomationRun.rule_id == lev.id))
    assert failed.outcome == "failed" and "Leverage must be one of" in failed.message
    nobody = rule(db, "kyc_pending", "notify_staff", params={"days": 0})
    c = db.get(Client, acc.client_id)
    c.kyc_status = KycStatus.pending
    db.commit()
    assert automation.run_rule(db, nobody, None, gw)["failed"] == 1  # no email to send to


def test_preview_changes_nothing_and_disabled_rules_never_run(db, gw, account_groups):
    acc = account(db, gw, account_groups, "p@x.com")
    db.get(TradingAccount, acc.id).last_activity_at = utcnow() - timedelta(days=40)
    db.commit()
    notifications.outbox.clear()
    r = rule(db, "inactive", "notify_staff", params={"days": 30}, enabled=False,
             action_params={"to": "dealing@broker.com", "message": "x"})
    p = automation.preview(db, r)
    assert p["total"] == 1 and p["due"] == 1 and not notifications.outbox
    assert db.scalar(select(func.count()).select_from(AutomationRun)) == 0
    assert not r.enabled and automation.run_due(db, gw) == 0  # new rules start disabled
    automation.set_enabled(db, None, r, True)
    assert automation.run_due(db, gw) == 1 and notifications.outbox[-1]["To"] == "dealing@broker.com"
    assert automation.run_due(db, gw) == 0  # not again within EVERY_MINUTES
    assert automation.preview(db, r)["matches"][0]["cooling"]


def test_rule_validation(db):
    with pytest.raises(ServiceError, match="works on trading accounts"):
        rule(db, "no_deposit", "disable_trading", params={"days": 3}, action_params={})
    with pytest.raises(ServiceError, match="not a number"):
        rule(db, "inactive", params={"days": "abc"})
    with pytest.raises(ServiceError, match="Write the message"):
        rule(db, "inactive", params={"days": 3}, action_params={"message": ""})
    with pytest.raises(ServiceError, match="choose a style"):
        rule(db, "trader_style", params={"style": "nope"})


def test_pages(admin_web, db, gw, account_groups):  # noqa: F811
    assert admin_web.get("/admin/automation").status_code == 200
    assert "Account inactive" in admin_web.get("/admin/automation/new").text
    r = form_post(admin_web, "/admin/automation/new", "/admin/automation/new", {
        "name": "Nudge", "trigger": "no_deposit", "t_no_deposit_days": "7", "kind": "live", "groups": "",
        "action": "email_client", "a_email_client_subject": "Ready to trade?", "a_email_client_message": "Hi {client}",
        "cooldown_hours": "0"})
    assert "Rule saved, disabled" in r.text
    saved = db.scalar(select(AutomationRule).where(AutomationRule.name == "Nudge"))
    assert saved.params == {"days": 7} and saved.action_params["subject"] == "Ready to trade?" and not saved.enabled
    page = f"/admin/automation/{saved.id}"
    assert "Nothing matches now" in admin_web.get(page + "?preview=1").text
    assert "Rule enabled" in form_post(admin_web, page, page + "/toggle", {}).text
    assert "Ran now: 0 matched" in form_post(admin_web, page, page + "/run", {}).text
    assert "deleted" in form_post(admin_web, page, page + "/delete", {}).text
