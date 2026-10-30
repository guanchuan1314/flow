"""Automation: rules the CRM's worker checks every few minutes against the CRM's own data (accounts, deals,
payments, KYC), then acts on what matches. Nothing here is an MT5 setting: the rules live in the CRM only.
Some actions (change group, leverage, trading on/off) still write to MT5 through the usual account services.

A rule has a trigger (what to look for, with its params), filters (which accounts: MT5 group masks, live /
demo), an action (with its params) and a cooldown: a target matched again is acted on only after
`cooldown_hours` (0 = once per target, ever). Every action, done or failed, is kept in the run log.
New rules are disabled; **Preview** shows who matches now without doing anything.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import exists, false, func, not_, or_, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import Conflict, Forbidden, ServiceError
from app.db import utcnow
from app.models import (
    AccountGroup, AccountStatus, AutomationRule, AutomationRun, Client, ClientNote, Deal, KycStatus, PaymentKind,
    PaymentRequest, PaymentStatus, Staff, TradingAccount,
)
from app.mt5 import MT5Error, MT5Gateway

log = logging.getLogger(__name__)

EVERY_MINUTES = 5  # how often the worker checks each enabled rule
MAX_PER_RUN = 200  # targets acted on per rule and run; the rest follow on the next run
PREVIEW = 500
KINDS = {"live": "Live accounts", "demo": "Demo accounts", "any": "Live and demo"}

# trigger -> (label, target "account" | "client", {param: (label, type, default)}, help)
TRIGGERS = {
    "inactive": ("Account inactive", "account", {"days": ("Days without a login or trade", "int", 30)},
                 "No terminal connection and no trade for N days (the CRM's account activity)."),
    "trader_style": ("Trader style detected", "account",
                     {"style": ("Style", "style", "ea"), "days": ("Over the last days", "int", 30),
                      "min_deals": ("At least deals", "int", 10)},
                     "The account's trading style from Trader styles, over the last N days."),
    "many_deals": ("Many deals", "account", {"deals": ("At least deals", "int", 100), "hours": ("Within hours", "int", 24)},
                   "N or more buy / sell deals within the last hours."),
    "margin_level": ("Margin level below", "account", {"percent": ("Margin level %", "dec", 150)},
                     "Accounts with open margin whose margin level is below this (as of the last sync)."),
    "equity_below": ("Equity below", "account", {"amount": ("Equity", "dec", 50)},
                     "Accounts with a balance whose equity is below this, in the account's currency."),
    "no_deposit": ("No deposit after registration", "client", {"days": ("Days since registration", "int", 7)},
                   "Clients registered at least N days ago who have never had a deposit approved."),
    "kyc_pending": ("KYC waiting", "client", {"days": ("Days waiting", "int", 2)},
                    "Clients whose KYC has been pending review for N days."),
}
# action -> (label, works on "account" | "any", {param: (label, type, default)})
ACTIONS = {
    "notify_staff": ("Email staff", "any", {"to": ("To (email; empty = the client's account manager or sales)", "str", ""),
                                             "message": ("Message", "text", "")}),
    "email_client": ("Email the client", "any", {"subject": ("Subject", "str", ""), "message": ("Message", "text", "")}),
    "client_note": ("Add a note to the client", "any", {"message": ("Note", "text", "")}),
    "change_group": ("Move to account group (MT5)", "account", {"account_group_id": ("Account group", "group", "")}),
    "change_leverage": ("Change leverage (MT5)", "account", {"leverage": ("Leverage", "int", 100)}),
    "disable_trading": ("Disable trading (MT5)", "account", {}),
    "enable_trading": ("Enable trading (MT5)", "account", {}),
}

# What a staff member needs, on top of automation.manage, to save, enable or run a rule with this action:
# a rule acts on every matching account, whoever's client it is, so writing to MT5 needs the right to
# manage accounts and to see all clients; emailing clients is marketing.
ACTION_PERMISSIONS = {
    "change_group": ("accounts.manage", "clients.view_all"),
    "change_leverage": ("accounts.manage", "clients.view_all"),
    "disable_trading": ("accounts.manage", "clients.view_all"),
    "enable_trading": ("accounts.manage", "clients.view_all"),
    "email_client": ("marketing.manage",),
}


def check_permissions(actor, action: str) -> None:
    """Refuse a staff member who may not have this action done on their behalf (the worker, actor None, may)."""
    if not isinstance(actor, Staff):
        return
    missing = [p for p in ("automation.manage", *ACTION_PERMISSIONS.get(action, ())) if not actor.has_perm(p)]
    if missing:
        raise Forbidden(f"A rule that does '{ACTIONS.get(action, (action,))[0]}' needs the permission "
                        f"{', '.join(missing)}")


# ---------------------------------------------------------------- validation
def _value(kind: str, raw, label: str):
    raw = "" if raw is None else str(raw).strip()
    if kind in ("str", "text"):
        return raw[:2000]
    if kind == "style":
        from app.services import trader_styles

        if raw not in trader_styles.STYLES:
            raise ServiceError(f"{label}: choose a style")
        return raw
    if kind == "group":
        try:
            return int(raw)
        except ValueError:
            raise ServiceError(f"{label}: choose the account group")
    try:
        n = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ServiceError(f"{label}: '{raw}' is not a number")
    if n < 0:
        raise ServiceError(f"{label} can't be negative")
    if kind == "int":
        if n != n.to_integral_value():
            raise ServiceError(f"{label}: a whole number")
        return int(n)
    return str(n)


def clean(trigger: str, params: dict, filters: dict, action: str, action_params: dict, db: Session) -> tuple:
    if trigger not in TRIGGERS:
        raise ServiceError("Choose what to look for")
    if action not in ACTIONS:
        raise ServiceError("Choose what to do")
    t_label, target, t_params, _ = TRIGGERS[trigger]
    a_label, works_on, a_params = ACTIONS[action]
    if works_on == "account" and target != "account":
        raise ServiceError(f"\"{a_label}\" works on trading accounts; \"{t_label}\" finds clients")
    p = {k: _value(kind, params.get(k, default), label) for k, (label, kind, default) in t_params.items()}
    ap = {k: _value(kind, action_params.get(k, default), label) for k, (label, kind, default) in a_params.items()}
    if action in ("client_note", "email_client") and not ap["message"]:
        raise ServiceError("Write the message")
    if action == "email_client" and not ap["subject"]:
        raise ServiceError("Write the email's subject")
    if action == "change_group" and db.get(AccountGroup, ap["account_group_id"]) is None:
        raise ServiceError("Choose the account group to move to")
    kind = filters.get("kind") or "live"
    if kind not in KINDS:
        raise ServiceError("Choose live, demo or both")
    return p, {"groups": str(filters.get("groups") or "").strip(), "kind": kind}, ap


def save_rule(db: Session, actor, rule: AutomationRule | None, *, name: str, trigger: str, params: dict,
              filters: dict, action: str, action_params: dict, cooldown_hours: int,
              enabled: bool | None = None) -> AutomationRule:
    name = name.strip()
    if not name or len(name) > 64:
        raise ServiceError("Name: 1 to 64 characters")
    clash = db.scalar(select(AutomationRule).where(AutomationRule.name == name))
    if clash is not None and clash is not rule:
        raise Conflict(f"A rule named {name} already exists")
    if cooldown_hours < 0:
        raise ServiceError("Cooldown can't be negative")
    p, f, ap = clean(trigger, params, filters, action, action_params, db)
    check_permissions(actor, action)
    if rule is not None:
        check_permissions(actor, rule.action)  # nor rewrite a rule you couldn't have made
    new = rule is None
    if new:
        rule = AutomationRule(name=name, enabled=False)  # new rules start disabled
        db.add(rule)
    rule.name, rule.trigger, rule.params, rule.filters = name, trigger, p, f
    rule.action, rule.action_params, rule.cooldown_hours = action, ap, cooldown_hours
    if enabled is not None and not new:
        rule.enabled = enabled
    db.flush()
    audit(db, actor, "automation.create" if new else "automation.update", "automation_rule", rule.id,
          {"name": name, "trigger": trigger, "params": p, "filters": f, "action": action, "action_params": ap,
           "cooldown_hours": cooldown_hours, "enabled": rule.enabled})
    db.commit()
    return rule


def set_enabled(db: Session, actor, rule: AutomationRule, enabled: bool) -> None:
    if enabled:
        check_permissions(actor, rule.action)
    rule.enabled = enabled
    audit(db, actor, "automation.enable" if enabled else "automation.disable", "automation_rule", rule.id,
          {"name": rule.name})
    db.commit()


def delete_rule(db: Session, actor, rule: AutomationRule) -> None:
    audit(db, actor, "automation.delete", "automation_rule", rule.id, {"name": rule.name})
    db.delete(rule)
    db.commit()


# ---------------------------------------------------------------- matching
def _accounts(rule: AutomationRule):
    """Base query for account triggers, with the rule's filters (group masks in SQL), and the same mask check in
    Python for matches made outside SQL (trader styles)."""
    from app.core import masks as mask_svc

    q = select(TradingAccount).where(TradingAccount.status == AccountStatus.active)
    kind = (rule.filters or {}).get("kind", "live")
    if kind != "any":
        q = q.where(TradingAccount.is_demo.is_(kind == "demo"))
    masks = (rule.filters or {}).get("groups", "")
    if not masks:
        return q, (lambda acc: True)
    group, parts = func.lower(TradingAccount.group), mask_svc.parse(masks)
    q = q.where(or_(false(), *[group.like(mask_svc.to_like(m), escape="!") for m in parts if not m.startswith("!")]),
                *[not_(group.like(mask_svc.to_like(m[1:]), escape="!")) for m in parts if m.startswith("!")])
    return q, (lambda acc: mask_svc.matches(masks, acc.group))


def _cooling_clause(rule: AutomationRule, target):
    """EXISTS: the target (a SQL expression giving "account:<login>" / "client:<id>") is cooling down."""
    q = exists().where(AutomationRun.rule_id == rule.id, AutomationRun.target == target,
                       AutomationRun.outcome == "done")
    if rule.cooldown_hours:
        q = q.where(AutomationRun.at > utcnow() - timedelta(hours=rule.cooldown_hours))
    return q


def _match_query(rule: AutomationRule):
    """(query, detail(row), target expression) for a trigger matched by one SQL query; None for trader styles.
    Each row's first column is the client or the trading account."""
    p, now = rule.params or {}, utcnow()
    if TRIGGERS[rule.trigger][1] == "client":
        target = func.concat("client:", Client.id)
        if rule.trigger == "no_deposit":
            deposited = exists().where(PaymentRequest.client_id == Client.id, PaymentRequest.kind == PaymentKind.deposit,
                                       PaymentRequest.status == PaymentStatus.approved)
            q = (select(Client).where(Client.created_at <= now - timedelta(days=p["days"]), ~deposited)
                 .order_by(Client.created_at, Client.id))
            return q, (lambda r: f"registered {r[0].created_at:%Y-%m-%d}, no deposit"), target
        q = (select(Client).where(Client.kyc_status == KycStatus.pending,  # kyc_pending
                                  Client.updated_at <= now - timedelta(days=p["days"]))
             .order_by(Client.updated_at, Client.id))
        return q, (lambda r: f"KYC pending since {r[0].updated_at:%Y-%m-%d}"), target
    if rule.trigger == "trader_style":
        return None
    q, _ = _accounts(rule)
    target = func.concat("account:", TradingAccount.login)
    if rule.trigger == "inactive":
        last = func.coalesce(TradingAccount.last_activity_at, TradingAccount.created_at)
        return (q.where(last < now - timedelta(days=p["days"])).order_by(last, TradingAccount.login),
                lambda r: f"last activity {(r[0].last_activity_at or r[0].created_at):%Y-%m-%d}", target)
    if rule.trigger == "many_deals":
        counts = (select(Deal.login, func.count().label("n"))
                  .where(Deal.time >= now - timedelta(hours=p["hours"]), Deal.action.in_((0, 1)))
                  .group_by(Deal.login).having(func.count() >= p["deals"]).subquery())
        q = (q.add_columns(counts.c.n).join(counts, counts.c.login == TradingAccount.login)
             .order_by(counts.c.n.desc(), TradingAccount.login))
        return q, (lambda r: f"{r[1]} deals in {p['hours']} h"), target
    if rule.trigger == "margin_level":
        return (q.where(TradingAccount.margin > 0, TradingAccount.margin_level < Decimal(p["percent"]))
                .order_by(TradingAccount.margin_level, TradingAccount.login),
                lambda r: f"margin level {r[0].margin_level}%", target)
    return (q.where(TradingAccount.balance > 0, TradingAccount.equity < Decimal(p["amount"]))  # equity_below
            .order_by(TradingAccount.equity, TradingAccount.login),
            lambda r: f"equity {r[0].equity} {r[0].currency}", target)


def _style_matches(db: Session, rule: AutomationRule) -> list[dict]:
    from app.services import trader_styles

    p = rule.params or {}
    _, keep = _accounts(rule)
    kind = (rule.filters or {}).get("kind", "live")
    rows = []
    for demo in ([False, True] if kind == "any" else [kind == "demo"]):
        rows += [r for r in trader_styles.profiles(db, None, p["days"], p["min_deals"], demo=demo)
                 if r.get("style") == p["style"]]
    return [{"target": f"account:{r['account'].login}", "login": r["account"].login,
             "client_id": r["account"].client_id,
             "detail": f"{trader_styles.STYLES[p['style']]}: {r.get('placed', '?')} deals in {p['days']} days"}
            for r in rows if keep(r["account"])]


def matches_now(db: Session, rule: AutomationRule, due_only: bool = False, limit: int | None = None) -> list[dict]:
    """Who the rule matches now: [{target, login, client_id, detail}]. Reads only.

    due_only leaves out targets cooling down (NOT EXISTS in SQL); limit caps the rows read."""
    found = _match_query(rule)
    if found is None:
        out = _style_matches(db, rule)
        if due_only:
            cooling = _cooling(db, rule, [m["target"] for m in out])
            out = [m for m in out if m["target"] not in cooling]
        return out[:limit] if limit is not None else out
    q, detail, target = found
    if due_only:
        q = q.where(~_cooling_clause(rule, target))
    if limit is not None:
        q = q.limit(limit)
    out = []
    for r in db.execute(q):
        obj = r[0]
        if isinstance(obj, Client):
            out.append({"target": f"client:{obj.id}", "login": None, "client_id": obj.id, "detail": detail(r)})
        else:
            out.append({"target": f"account:{obj.login}", "login": obj.login, "client_id": obj.client_id,
                        "detail": detail(r)})
    return out


def count_matches(db: Session, rule: AutomationRule, due_only: bool = False) -> int:
    """How many targets the rule matches now (due_only: not cooling down), counted in SQL where it can be."""
    found = _match_query(rule)
    if found is None:
        return len(matches_now(db, rule, due_only))
    q, _, target = found
    if due_only:
        q = q.where(~_cooling_clause(rule, target))
    return db.scalar(select(func.count()).select_from(q.order_by(None).subquery())) or 0


def _cooling(db: Session, rule: AutomationRule, targets: list[str] | None = None) -> set[str]:
    """Targets acted on recently enough that they are skipped (cooldown 0: ever), among `targets` if given."""
    q = select(AutomationRun.target).where(AutomationRun.rule_id == rule.id, AutomationRun.outcome == "done")
    if rule.cooldown_hours:
        q = q.where(AutomationRun.at > utcnow() - timedelta(hours=rule.cooldown_hours))
    if targets is not None:
        if not targets:
            return set()
        q = q.where(AutomationRun.target.in_(targets))
    return set(db.scalars(q))


def preview(db: Session, rule: AutomationRule) -> dict:
    """Dry run: who matches now and who would be acted on (not cooling down). Changes nothing."""
    styles = _match_query(rule) is None  # trader styles: matched in Python, so count the list itself
    found = matches_now(db, rule, limit=None if styles else PREVIEW)
    cooling = _cooling(db, rule, [m["target"] for m in found])
    for m in found:
        m["cooling"] = m["target"] in cooling
    if styles:
        return {"matches": found[:PREVIEW], "total": len(found), "due": sum(1 for m in found if not m["cooling"])}
    return {"matches": found, "total": count_matches(db, rule), "due": count_matches(db, rule, due_only=True)}


# ---------------------------------------------------------------- acting
def _render(text: str, acc: TradingAccount | None, client: Client, rule: AutomationRule, detail: str) -> str:
    values = {"{client}": f"{client.first_name} {client.last_name}", "{email}": client.email,
              "{login}": str(acc.login) if acc else "", "{group}": acc.group if acc else "", "{rule}": rule.name,
              "{detail}": detail}
    for key, value in values.items():
        text = text.replace(key, value)
    return text


def _act(db: Session, rule: AutomationRule, m: dict, gw: MT5Gateway | None) -> str:
    from app.services import accounts, notifications

    ap = rule.action_params or {}
    acc = db.scalar(select(TradingAccount).where(TradingAccount.login == m["login"])) if m["login"] else None
    client = db.get(Client, m["client_id"])
    if client is None:
        raise ServiceError("client not found")
    message = _render(ap.get("message", ""), acc, client, rule, m["detail"])

    def mt5_gw():
        if gw is not None:
            return gw
        from app.mt5 import for_login
        return for_login(db, acc.login)

    if rule.action == "notify_staff":
        to = ap.get("to") or ""
        if not to:
            sid = client.account_manager_id or client.sales_staff_id
            to = db.scalar(select(Staff.email).where(Staff.id == sid)) if sid else ""
        if not to:
            raise ServiceError("no staff email: set one on the rule, or assign the client")
        subject = f"Automation: {rule.name}"
        what = f"{client.first_name} {client.last_name} ({client.email})" + (f", account {acc.login}" if acc else "")
        notifications.send_email(to, subject, f"{what}: {m['detail']}.\n\n{message}".strip())
        return f"emailed {to}"
    if rule.action == "email_client":
        notifications.notify_client(client, _render(ap["subject"], acc, client, rule, m["detail"]), message)
        return f"emailed {client.email}"
    if rule.action == "client_note":
        db.add(ClientNote(client_id=client.id, staff_id=None, body=f"[{rule.name}] {message}"))
        return "note added"
    if acc is None:
        raise ServiceError("this action needs a trading account")
    if rule.action == "change_group":
        ag = db.get(AccountGroup, ap["account_group_id"])
        if ag is None:
            raise ServiceError("the account group no longer exists")
        if acc.account_group_id == ag.id:
            return f"already in {ag.name}"
        accounts.change_group(db, mt5_gw(), None, acc, ag)
        return f"moved to {ag.name}"
    if rule.action == "change_leverage":
        if acc.leverage == ap["leverage"]:
            return f"already 1:{acc.leverage}"
        accounts.change_leverage(db, mt5_gw(), None, acc, ap["leverage"])
        return f"leverage 1:{ap['leverage']}"
    enabled = rule.action == "enable_trading"
    if acc.trading_enabled == enabled:
        return "trading already " + ("enabled" if enabled else "disabled")
    accounts.set_trading_enabled(db, mt5_gw(), None, acc, enabled)
    return "trading " + ("enabled" if enabled else "disabled")


def run_rule(db: Session, rule: AutomationRule, actor=None, gw: MT5Gateway | None = None) -> dict:
    """Act on every target the rule matches that isn't cooling down (at most MAX_PER_RUN).
    Returns {matched, done, failed, skipped}."""
    check_permissions(actor, rule.action)
    if _match_query(rule) is None:  # trader styles: matched in Python once (Trader styles caps what it reads)
        found = matches_now(db, rule)
        cooling = _cooling(db, rule, [m["target"] for m in found])
        due = [m for m in found if m["target"] not in cooling]
        matched, due_total, due = len(found), len(due), due[:MAX_PER_RUN]
    else:
        due = matches_now(db, rule, due_only=True, limit=MAX_PER_RUN)
        matched, due_total = count_matches(db, rule), count_matches(db, rule, due_only=True)
    done = failed = 0
    rule_id, rule_name = rule.id, rule.name
    for m in due[:MAX_PER_RUN]:
        try:
            message, outcome = _act(db, rule, m, gw), "done"
            done += 1
        except (ServiceError, MT5Error) as exc:
            db.rollback()
            rule = db.get(AutomationRule, rule_id)
            message, outcome = getattr(exc, "message", None) or str(exc), "failed"
            failed += 1
        db.add(AutomationRun(rule_id=rule_id, target=m["target"], login=m["login"], client_id=m["client_id"],
                             outcome=outcome, message=message[:255]))
        audit(db, actor, f"automation.{outcome}", "automation_rule", rule_id,
              {"rule": rule_name, "target": m["target"], "action": rule.action, "message": message[:200]})
        db.commit()
    result = {"matched": matched, "done": done, "failed": failed, "skipped": max(0, matched - due_total)}
    rule.last_run_at = utcnow()
    rule.last_result = describe(result)
    db.commit()
    return result


def describe(r: dict) -> str:
    text = f"{r['matched']} matched, {r['done']} done"
    if r["failed"]:
        text += f", {r['failed']} failed"
    if r["skipped"]:
        text += f", {r['skipped']} in cooldown"
    return text


def run_due(db: Session, gw: MT5Gateway | None = None) -> int:
    """Worker: run each enabled rule not run in the last EVERY_MINUTES. Returns how many ran."""
    due = utcnow() - timedelta(minutes=EVERY_MINUTES)
    ran = 0
    for rule in db.scalars(select(AutomationRule).where(AutomationRule.enabled.is_(True)).order_by(AutomationRule.id)).all():
        if rule.last_run_at is not None and rule.last_run_at > due:
            continue
        try:
            run_rule(db, rule, None, gw)
        except Exception:  # noqa: BLE001 - one broken rule must not stop the others
            db.rollback()
            log.exception("automation rule %s failed", rule.name)
            rule = db.get(AutomationRule, rule.id)
            rule.last_run_at, rule.last_result = utcnow(), "failed: see the log"
            db.commit()
        ran += 1
    return ran


def runs(db: Session, rule: AutomationRule | None = None, limit: int = 200) -> list[AutomationRun]:
    q = select(AutomationRun).order_by(AutomationRun.at.desc(), AutomationRun.id.desc()).limit(limit)
    if rule is not None:
        q = q.where(AutomationRun.rule_id == rule.id)
    return list(db.scalars(q))
