"""Import from MT5 servers into the CRM.

* sync_account_groups    : MT5 groups      -> account_groups   (one per server + group)
* sync_trading_accounts : MT5 users       -> trading_accounts (+ clients for unknown owners)

Both are idempotent and safe to run repeatedly (manually or on a schedule). A group that
disappeared from MT5 is flagged `on_server = False`; so is an account, which then goes from
Flow once MT5 confirms it does not have it (services.account_removal).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import masks
from app.core.audit import audit
from app.core.errors import ServiceError
from app.db import utcnow
from app.models import (
    AccountStatus, AccountGroup, Client, ClientNote, ClientStatus, MT5Server, TradingAccount,
)
from app.mt5 import MT5Error, MT5Gateway, get_gateway
from app.mt5.gateway import MT5Group, MT5User
from app.services import account_removal
from app.services import clients as client_svc
from app.services import sync
from app.services import sync_runs

log = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
COMMIT_EVERY = 500
IMPORT_CHUNK = 5000  # MT5 users per query when matching them with CRM accounts and clients


def excluded(group: str) -> bool:
    return masks.matches(masks.excluded_groups(), group)


def _gw(server: MT5Server, gw: MT5Gateway | None) -> MT5Gateway:
    if not server.enabled:
        raise ServiceError(f"Server '{server.name}' is disabled")
    try:
        return gw or get_gateway(server.id)
    except MT5Error as exc:
        raise ServiceError(f"Cannot reach MT5 server '{server.name}': {exc}", 502) from exc


# ---------------------------------------------------------------- account groups
def _group_name(db: Session, server: MT5Server, group: str) -> str:
    """Readable unique name: real\\standard -> 'Standard (real)'; add the server on clashes."""
    parts = [p for p in re.split(r"[\\/]", group) if p]
    base = parts[-1].replace("_", " ").replace("-", " ").title() if parts else group
    if len(parts) > 1:
        base = f"{base} ({parts[0]})"
    name = base[:64]
    for candidate in (name, f"{base} – {server.name}"[:64]):
        if not db.scalar(select(AccountGroup.id).where(AccountGroup.name == candidate)):
            return candidate
    n = 2
    while db.scalar(select(AccountGroup.id).where(AccountGroup.name == f"{name[:58]} #{n}")):
        n += 1
    return f"{name[:58]} #{n}"


def _new_group(db: Session, server: MT5Server, g: MT5Group, now: datetime) -> AccountGroup:
    is_demo = server.is_demo or g.name.lower().startswith("demo")
    leverage = g.demo_leverage if is_demo and g.demo_leverage else 100
    return AccountGroup(
        server_id=server.id, name=_group_name(db, server, g.name), mt5_group=g.name, is_demo=is_demo,
        currency=(g.currency or "USD")[:3].upper(), leverage_options=sorted({leverage, 100}),
        default_leverage=leverage, demo_initial_balance=g.demo_deposit if is_demo and g.demo_deposit else 10000,
        margin_call=g.margin_call, stop_out=g.stop_out,
        enabled=False,  # imported groups are not offered to clients until an admin reviews them
        description=f"Imported from MT5 group {g.name}"[:255], source="mt5", on_server=True, synced_at=now)


# ---------------------------------------------------------------- account group <-> group settings
# Which account group fields mirror the MT5 group. Everything else (name, deposit minimum, leverage
# choices, spread markup, availability) exists only in the CRM.
GROUP_FIELDS = ("currency", "margin_call", "stop_out")
DEMO_GROUP_FIELDS = ("default_leverage", "demo_initial_balance")  # demo groups only


def pull_group(ag: AccountGroup, g: MT5Group) -> list[str]:
    """Copy the group's settings onto its account group; returns the fields that changed."""
    values = {"currency": (g.currency or ag.currency)[:3].upper(), "margin_call": g.margin_call, "stop_out": g.stop_out}
    if ag.is_demo:
        if g.demo_leverage:
            values["default_leverage"] = g.demo_leverage
            if g.demo_leverage not in ag.leverage_options:
                values["leverage_options"] = sorted({*ag.leverage_options, g.demo_leverage})
        if g.demo_deposit:
            values["demo_initial_balance"] = g.demo_deposit
    changed = [k for k, v in values.items() if getattr(ag, k) != v]
    for k in changed:
        setattr(ag, k, values[k])
    return changed


def push_group(gw: MT5Gateway, ag: AccountGroup) -> MT5Group:
    """Write the account group's settings to its group (only the ones that differ)."""
    g = gw.get_group(ag.mt5_group)
    if g is None:
        raise MT5Error(f"Group {ag.mt5_group} not found on MT5", 13)
    wanted = {"margin_call": ag.margin_call, "stop_out": ag.stop_out}
    if ag.is_demo:
        wanted.update(demo_leverage=ag.default_leverage, demo_deposit=ag.demo_initial_balance)
    diff = {k: v for k, v in wanted.items() if v is not None and Decimal(str(getattr(g, k))) != Decimal(str(v))}
    return gw.update_group(ag.mt5_group, **diff) if diff else g


def refresh_group(db: Session, ag: AccountGroup) -> str | None:
    """Re-read the type's group so the edit form shows MT5's current values. Returns a warning, or None."""
    try:
        g = get_gateway(ag.server_id).get_group(ag.mt5_group)
    except MT5Error as exc:
        return f"Could not read the group from MT5 ({exc}). The values below are from the last sync."
    if g is None:
        return f"Group {ag.mt5_group} no longer exists on MT5. Delete this account group, or create the group again."
    if pull_group(ag, g) or not ag.on_server:
        ag.on_server, ag.synced_at = True, utcnow()
        db.commit()
    return None


def sync_account_groups(db: Session, server: MT5Server, actor=None, gw: MT5Gateway | None = None) -> dict:
    gw = _gw(server, gw)
    try:
        groups = [g for g in gw.get_groups("*") if not excluded(g.name)]
    except MT5Error as exc:
        raise ServiceError(f"Could not read groups from '{server.name}': {exc}", 502) from exc
    now = utcnow()
    existing = {t.mt5_group: t for t in db.scalars(select(AccountGroup).where(AccountGroup.server_id == server.id))}
    created, updated, missing = [], [], []
    on_mt5 = set()
    for g in groups:
        on_mt5.add(g.name)
        ag = existing.get(g.name)
        if ag is None:
            ag = _new_group(db, server, g, now)
            db.add(ag)
            db.flush()
            created.append(ag.name)
            continue
        if pull_group(ag, g) or not ag.on_server:  # changes made in MT5 Administrator
            updated.append(ag.name)
        ag.on_server, ag.synced_at = True, now
    for group, ag in existing.items():
        if group not in on_mt5 and ag.on_server and not excluded(group):
            ag.on_server, ag.enabled = False, False  # accounts could no longer be opened in it
            missing.append(ag.name)
    server.groups_synced_at = now
    summary = {"groups": len(groups), "created": created, "updated": updated, "missing": missing}
    server.last_sync_summary = {**(server.last_sync_summary or {}), "account_groups": summary, "at": now.isoformat()}
    audit(db, actor, "mt5_sync.account_groups", "mt5_server", server.id,
          {"groups": len(groups), "created": len(created), "updated": len(updated), "missing": len(missing)})
    db.commit()
    log.info("server %s: %d groups, %d new account groups, %d missing", server.name, len(groups), len(created),
             len(missing))
    return summary


# ---------------------------------------------------------------- trading accounts
def split_name(user: MT5User) -> tuple[str, str]:
    """(first, last) from MT5 name fields, which servers fill inconsistently:
    some put the whole name in FirstName (LastName empty, or repeating the surname)."""
    first, last = user.first_name.strip(), user.last_name.strip()
    full = (user.name or "").strip()
    if first and last and first != last and first.endswith(last):
        first = first[: -len(last)].strip()  # FirstName holds the full name
    if not last:
        first, _, last = (first or full).partition(" ")
    if not first:
        first, _, rest = full.partition(" ")
        last = last or rest
    return (first.strip() or "MT5")[:64], (last.strip() or str(user.login))[:64]


def _clients_by_email(db: Session, users: list[MT5User]) -> dict[str, Client]:
    """Existing clients for these MT5 users' emails, by lower-cased email: one query (uses the lower(email) index)."""
    emails = {e for e in ((u.email or "").strip().lower() for u in users) if EMAIL_RE.match(e)}
    found: dict[str, Client] = {}
    for chunk in sync._chunks(sorted(emails), IMPORT_CHUNK):
        for client in db.scalars(select(Client).where(func.lower(Client.email).in_(chunk)).order_by(Client.id.desc())):
            found[client.email.lower()] = client  # the oldest client wins if two differ only in case
    return found


def _owner(db: Session, server: MT5Server, user: MT5User, now: datetime,
           known: dict[str, Client] | None = None) -> tuple[Client, bool]:
    """Existing client with the account's email, or a new one. Returns (client, created).

    `known` (from _clients_by_email) saves a query per user; clients created here are added to it."""
    email = (user.email or "").strip().lower()
    if EMAIL_RE.match(email):
        if known is not None:
            client = known.get(email)
        else:
            client = db.scalar(select(Client).where(func.lower(Client.email) == email).order_by(Client.id))
        if client is not None:
            return client, False
    else:
        email = f"mt5-{server.id}-{user.login}@import.invalid"  # MT5 user without a usable email
        existing = db.scalar(select(Client).where(Client.email == email))
        if existing is not None:
            return existing, False
    first, last = split_name(user)
    country = user.country.strip().upper() if len(user.country.strip()) == 2 else None
    client = Client(email=email, first_name=first, last_name=last, phone=(user.phone or None),
                    country=country, city=(user.city or None), address=(user.address or None),
                    status=ClientStatus.active, referral_code=client_svc._unique_referral_code(db),
                    lead_source="mt5-import")
    db.add(client)
    db.flush()
    details = [f"Created by import of MT5 account {user.login} on {server.name}."]
    if user.country and not country:
        details.append(f"MT5 country: {user.country}")
    if user.comment:
        details.append(f"MT5 comment: {user.comment}")
    if email.endswith("@import.invalid"):
        details.append("No valid email on MT5: update the email before the client can use the portal.")
    db.add(ClientNote(client_id=client.id, staff_id=None, body="\n".join(details)))
    if known is not None and not email.endswith("@import.invalid"):
        known[email] = client
    return client, True


def sync_trading_accounts(db: Session, server: MT5Server, actor=None, gw: MT5Gateway | None = None) -> dict:
    gw = _gw(server, gw)
    types_summary = sync_account_groups(db, server, actor, gw)
    now = utcnow()
    groups = {t.mt5_group: t for t in db.scalars(select(AccountGroup).where(
        AccountGroup.server_id == server.id, AccountGroup.on_server.is_(True)))}
    created, updated, clients_created, clients_linked, failed_groups = 0, 0, 0, 0, []
    seen: set[int] = set()
    new_logins: list[int] = []
    pending = 0
    for n, (group, ag) in enumerate(groups.items()):
        sync_runs.report(n, len(groups))  # progress: MT5 groups read
        if excluded(group):
            continue
        try:
            users = gw.get_users_in_group(group)
        except MT5Error as exc:
            failed_groups.append(f"{group}: {exc}")
            continue
        for chunk in sync._chunks(users, IMPORT_CHUNK):  # one query per chunk for accounts, one for owners
            accounts = {a.login: a for a in db.scalars(select(TradingAccount).where(
                TradingAccount.server_id == server.id, TradingAccount.login.in_([u.login for u in chunk])))}
            owners = _clients_by_email(db, [u for u in chunk if u.login not in accounts])
            for user in chunk:
                seen.add(user.login)
                acc = accounts.get(user.login)
                status = AccountStatus.active if user.enabled else AccountStatus.disabled
                if acc is None:
                    client, is_new = _owner(db, server, user, now, owners)
                    clients_created += is_new
                    clients_linked += not is_new
                    acc = TradingAccount(
                        login=user.login, server_id=server.id, client_id=client.id, account_group_id=ag.id,
                        group=group, leverage=user.leverage or ag.default_leverage, currency=ag.currency,
                        is_demo=ag.is_demo, status=status, trading_enabled=user.trading_enabled, source="mt5",
                        on_server=True, last_seen_at=now,
                        created_at=user.registration or now,  # deal history is fetched from this date
                        last_access_at=user.last_access, last_activity_at=user.last_access or user.registration or now)
                    db.add(acc)
                    created += 1
                    new_logins.append(user.login)
                else:
                    changed = (acc.group, acc.account_group_id, acc.leverage, acc.trading_enabled, acc.on_server) != (
                        group, ag.id, user.leverage or acc.leverage, user.trading_enabled, True)
                    acc.group, acc.account_group_id = group, ag.id
                    acc.leverage = user.leverage or acc.leverage
                    acc.trading_enabled, acc.on_server, acc.last_seen_at = user.trading_enabled, True, now
                    if user.last_access and user.last_access != acc.last_access_at:
                        acc.last_access_at = user.last_access
                        acc.last_activity_at = max(acc.last_activity_at or user.last_access, user.last_access)
                    if acc.status != AccountStatus.archived and acc.status != status:
                        acc.status, changed = status, True
                    updated += changed
                pending += 1
                if pending >= COMMIT_EVERY:
                    db.commit()
                    pending = 0
    missing = 0
    if not failed_groups and seen:  # only flag accounts as gone when every group was read successfully
        for acc in db.scalars(select(TradingAccount).where(TradingAccount.server_id == server.id,
                                                           TradingAccount.on_server.is_(True),
                                                           TradingAccount.status != AccountStatus.archived)):
            if acc.login not in seen:
                acc.on_server = False
                missing += 1
    db.commit()
    removal = account_removal.remove_gone(db, server, gw, actor)
    if new_logins:  # balances and equity for the newly imported accounts straight away
        sync.sync_accounts(db, gw, new_logins, server.id)
    summary = {"removed": removal["removed"], "clients_removed": removal["clients"], "removal_error": removal["error"],
               "seen": len(seen), "created": created, "updated": updated, "clients_created": clients_created,
               "clients_linked": clients_linked, "missing": missing,
               "failed_groups": failed_groups, "account_groups": types_summary}
    server.accounts_synced_at = now
    server.last_sync_summary = {**{k: v for k, v in summary.items() if k != "account_groups"},
                                "account_groups": types_summary, "at": now.isoformat()}
    audit(db, actor, "mt5_sync.trading_accounts", "mt5_server", server.id,
          {k: (len(v) if isinstance(v, list) else v) for k, v in summary.items() if k != "account_groups"})
    db.commit()
    log.info("server %s: %d accounts seen, %d imported, %d updated, %d missing", server.name,
             len(seen), created, updated, missing)
    return summary


def sync_all(db: Session, actor=None, accounts: bool = True) -> dict[str, dict | str]:
    """Run the import for every enabled server; one failing server does not stop the others."""
    results = {}
    for server in db.scalars(select(MT5Server).where(MT5Server.enabled.is_(True)).order_by(MT5Server.id)).all():
        try:
            results[server.name] = (sync_trading_accounts if accounts else sync_account_groups)(db, server, actor)
        except ServiceError as exc:
            db.rollback()
            results[server.name] = f"failed: {exc.message}"
    return results
