"""MT5 account management: account groups, opening accounts, settings, wallet transfers."""

import logging
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.audit import audit
from app.core.errors import Conflict, Forbidden, NotFound, ServiceError
from app.core.security import generate_mt5_password
from app.db import utcnow
from app.models import (
    AccountPurpose, AccountStatus, AccountGroup, Client, ClientStatus, KycStatus, Staff, TradingAccount,
    WalletTxKind,
)
from app.mt5 import MT5Error, MT5Gateway
from app.services import notifications, wallet
from app.services.mt5_calls import call, gateway

log = logging.getLogger(__name__)


def get_account(db: Session, login: int) -> TradingAccount:
    acc = db.scalar(select(TradingAccount).where(TradingAccount.login == login))
    if acc is None:
        raise NotFound(f"Trading account {login} not found")
    return acc


def get_client_account(db: Session, client: Client, login: int) -> TradingAccount:
    acc = get_account(db, login)
    if acc.client_id != client.id:
        raise Forbidden("Account does not belong to this client")
    return acc


# ---------------------------------------------------------------- account groups


# Account groups mirror their MT5 group (see mt5_sync.GROUP_FIELDS): every create, update and delete
# is applied on MT5 first, and the CRM only saves once MT5 has accepted it.


def create_account_group(db: Session, actor, *, mt5_template: str | None = None, **fields) -> AccountGroup:
    """For an existing MT5 group, or with `mt5_template`, a new group copied from that one.
    Blank margin call / stop-out take the group's values; filled-in ones are written to the group."""
    from app.services.mt5_sync import pull_group, push_group

    if db.scalar(select(AccountGroup).where(AccountGroup.name == fields["name"])):
        raise Conflict("Account group name already exists")
    _check_server(db, fields.get("server_id"), fields.get("is_demo", False))
    if fields.get("default_leverage") not in fields.get("leverage_options", [fields.get("default_leverage")]):
        raise ServiceError("default_leverage must be one of leverage_options")
    if db.scalar(select(AccountGroup.id).where(AccountGroup.server_id == fields["server_id"],
                                              AccountGroup.mt5_group == fields["mt5_group"])):
        raise Conflict(f"An account group already uses group {fields['mt5_group']} on this server")
    gw = gateway(fields["server_id"])
    if mt5_template:
        call(lambda: gw.create_group(fields["mt5_group"], mt5_template, fields.get("currency")), "group creation")
    group = call(lambda: gw.get_group(fields["mt5_group"]), "group lookup")
    if group is None:
        raise ServiceError(f"Group {fields['mt5_group']} doesn't exist on that MT5 server. "
                           "Tick \"Create this group on MT5\" to create it.")
    ag = AccountGroup(**fields)
    if ag.open_on_register and not ag.is_demo:
        raise ServiceError("Only demo account groups can be opened automatically at sign-up")
    if ag.margin_call is None:
        ag.margin_call = group.margin_call
    if ag.stop_out is None:
        ag.stop_out = group.stop_out
    group = call(lambda: push_group(gw, ag), "group update")
    pull_group(ag, group)  # e.g. the currency of an existing group
    ag.source, ag.on_server, ag.synced_at = "crm", True, utcnow()
    db.add(ag)
    db.flush()
    audit(db, actor, "account_group.create", "account_group", ag.id,
          {**fields, "created_on_mt5_from": mt5_template} if mt5_template else fields)
    db.commit()
    return ag


def update_account_group(db: Session, actor, ag: AccountGroup, changes: dict) -> AccountGroup:
    from app.services.mt5_sync import DEMO_GROUP_FIELDS, GROUP_FIELDS, push_group

    changes = {k: v for k, v in changes.items() if v is not None or k not in ("margin_call", "stop_out")}
    moving = any(k in changes and changes[k] != getattr(ag, k) for k in ("server_id", "mt5_group"))
    if moving:
        if db.scalar(select(TradingAccount.id).where(TradingAccount.account_group_id == ag.id)):
            raise ServiceError("Accounts already exist for this type; create a new account group for another "
                               "group or server")
        server_id, group = changes.get("server_id", ag.server_id), changes.get("mt5_group", ag.mt5_group)
        _check_server(db, server_id, ag.is_demo)
        if db.scalar(select(AccountGroup.id).where(AccountGroup.server_id == server_id, AccountGroup.mt5_group == group,
                                                  AccountGroup.id != ag.id)):
            raise Conflict(f"An account group already uses group {group} on this server")
    mapped = GROUP_FIELDS + (DEMO_GROUP_FIELDS if ag.is_demo else ())
    to_mt5 = moving or any(k in changes and changes[k] != getattr(ag, k) for k in mapped)
    for k, v in changes.items():
        setattr(ag, k, v)
    if ag.default_leverage not in ag.leverage_options:
        db.rollback()
        raise ServiceError("default_leverage must be one of leverage_options")
    if ag.open_on_register and not ag.is_demo:
        db.rollback()
        raise ServiceError("Only demo account groups can be opened automatically at sign-up")
    if to_mt5:
        try:
            push_group(gateway(ag.server_id), ag)
        except (MT5Error, ServiceError) as exc:
            db.rollback()  # MT5 refused: keep the CRM as it was
            raise ServiceError(f"MT5 group update failed: {exc}", 502) from exc
        ag.on_server, ag.synced_at = True, utcnow()
    audit(db, actor, "account_group.update", "account_group", ag.id, {**changes, "written_to_mt5": to_mt5})
    db.commit()
    return ag


def group_accounts(db: Session, ag: AccountGroup) -> list[TradingAccount]:
    return list(db.scalars(select(TradingAccount).where(TradingAccount.account_group_id == ag.id)
                           .order_by(TradingAccount.login)))


def delete_preview(db: Session, ag: AccountGroup) -> dict:
    """What deleting the type affects, for the confirmation dialog."""
    from app.models import Position

    accs = group_accounts(db, ag)
    logins = [a.login for a in accs]
    targets = db.scalars(select(AccountGroup).where(AccountGroup.id != ag.id, AccountGroup.server_id == ag.server_id,
                                                   AccountGroup.is_demo == ag.is_demo, AccountGroup.on_server.is_(True))
                         .order_by(AccountGroup.name)).all()
    return {"accounts": len(accs), "balance": sum((a.balance for a in accs), Decimal("0")),
            "equity": sum((a.equity for a in accs), Decimal("0")),
            "positions": db.scalar(select(func.count()).select_from(Position).where(Position.login.in_(logins)))
            if logins else 0, "targets": targets}


def _copy_links(db: Session, logins: list[int]) -> list[str]:
    """Copy-trading / PAMM roles that stop an account from being deleted."""
    from app.models import CopyStrategy, CopySubscription, PammFund, StrategyStatus, SubscriptionStatus

    found = [f"strategy master {s.master_login}" for s in db.scalars(select(CopyStrategy).where(
        CopyStrategy.master_login.in_(logins), CopyStrategy.status != StrategyStatus.closed))]
    found += [f"follower {s.follower_login}" for s in db.scalars(select(CopySubscription).where(
        CopySubscription.follower_login.in_(logins), CopySubscription.status == SubscriptionStatus.active))]
    found += [f"PAMM master {f.master_login}" for f in db.scalars(select(PammFund).where(
        PammFund.master_login.in_(logins), PammFund.status != StrategyStatus.closed))]
    return found


def delete_account_group(db: Session, actor, ag: AccountGroup, accounts_action: str | None = None,
                        move_to_id: int | None = None) -> dict:
    """Delete the type and its MT5 group.

    With accounts on the type, `accounts_action` decides what happens to them first:
      * "move": each account moves to account group `move_to_id` (same server), MT5 group included.
      * "delete": each MT5 login is deleted; the CRM keeps the account, archived, for its history.
        Refused while an account has open positions or copy-trading / PAMM roles.
    """
    from app.models import CommissionRule

    rules = db.scalar(select(func.count()).select_from(CommissionRule).where(CommissionRule.account_group_id == ag.id))
    if rules:
        raise ServiceError(f"{rules} IB commission rule(s) refer to this account group; change them first.")
    accs = group_accounts(db, ag)
    gw = gateway(ag.server_id)
    group = call(lambda: gw.get_group(ag.mt5_group), "group lookup")
    if group is not None:  # MT5 users in the group that the CRM doesn't manage would block the group deletion
        crm_logins = {a.login for a in accs}
        others = [u.login for u in call(lambda: gw.get_users_in_group(ag.mt5_group), "group lookup")
                  if u.login not in crm_logins]
        if others:
            raise ServiceError(f"Group {ag.mt5_group} also has {len(others)} account(s) on MT5 that are not in the "
                               f"CRM (e.g. {others[0]}). Import them (Sync from MT5) or move them in MT5 first.")
    if accs and accounts_action not in ("move", "delete"):
        raise ServiceError(f"{len(accs)} trading account(s) use this account group. Move them to another type or "
                           "delete them, or untick \"Available for new accounts\" instead.")
    done = {"moved": 0, "deleted": 0}
    if accs and accounts_action == "move":
        target = db.get(AccountGroup, move_to_id) if move_to_id else None
        if target is None or target.id == ag.id:
            raise ServiceError("Choose the account group the accounts move to")
        if target.server_id != ag.server_id or target.is_demo != ag.is_demo:
            raise ServiceError("Accounts can only move to a type on the same server, and live to live / demo to demo")
        for acc in accs:
            try:
                gw.update_user(acc.login, group=target.mt5_group)
            except MT5Error as exc:
                raise ServiceError(f"Moved {done['moved']} of {len(accs)} accounts; MT5 refused {acc.login}: {exc}. "
                                   "Nothing else was changed; try again.", 502) from exc
            acc.account_group_id, acc.group = target.id, target.mt5_group
            audit(db, actor, "account.move_type", "trading_account", acc.login, {"from": ag.name, "to": target.name})
            db.commit()  # each move is final on MT5, so record it at once
            done["moved"] += 1
    elif accs and accounts_action == "delete":
        logins = [a.login for a in accs]
        links = _copy_links(db, logins)
        if links:
            raise ServiceError(f"Stop these copy-trading / PAMM roles first: {', '.join(links[:5])}")
        if call(lambda: gw.get_positions(logins), "position request"):
            raise ServiceError("Some of these accounts have open positions on MT5; close them first")
        for acc in accs:
            try:
                gw.delete_user(acc.login)
            except MT5Error as exc:
                raise ServiceError(f"Deleted {done['deleted']} of {len(accs)} accounts; MT5 refused {acc.login}: "
                                   f"{exc}. Try again.", 502) from exc
            audit(db, actor, "account.delete_on_mt5", "trading_account", acc.login,
                  {"account_group": ag.name, "balance": acc.balance, "equity": acc.equity})
            acc.status, acc.on_server, acc.account_group_id = AccountStatus.archived, False, None
            db.commit()
            done["deleted"] += 1
    if group is not None:
        call(lambda: gw.delete_group(ag.mt5_group), "group deletion")
    audit(db, actor, "account_group.delete", "account_group", ag.id,
          {"name": ag.name, "mt5_group": ag.mt5_group, "server_id": ag.server_id,
           "deleted_on_mt5": group is not None, **done})
    db.delete(ag)
    db.commit()
    return done


def _check_server(db: Session, server_id: int | None, is_demo: bool) -> None:
    from app.models import MT5Server

    server = db.get(MT5Server, server_id) if server_id else None
    if server is None:
        raise ServiceError("Choose the MT5 server this account group opens accounts on")
    if server.is_demo and not is_demo:
        raise ServiceError(f"'{server.name}' is a demo server; live account groups must use a live server")


# ---------------------------------------------------------------- accounts


def open_account(db: Session, gw: MT5Gateway, actor, client: Client, account_group_id: int,
                 leverage: int | None = None, purpose: AccountPurpose = AccountPurpose.trading) -> dict:
    ag = db.get(AccountGroup, account_group_id)
    if ag is None or not ag.enabled:
        raise NotFound("Account group not available")
    if client.status in (ClientStatus.suspended, ClientStatus.closed):
        raise Forbidden("Client is not active")
    if not ag.is_demo and get_settings().live_requires_kyc and client.kyc_status != KycStatus.approved:
        raise Forbidden("KYC must be approved before opening a live account")
    leverage = leverage or ag.default_leverage
    if leverage not in ag.leverage_options:
        raise ServiceError(f"Leverage must be one of {ag.leverage_options}")
    count = db.scalar(select(func.count()).select_from(TradingAccount).where(
        TradingAccount.client_id == client.id, TradingAccount.account_group_id == ag.id,
        TradingAccount.status != AccountStatus.archived))
    if count >= ag.max_accounts_per_client:
        raise ServiceError(f"Maximum of {ag.max_accounts_per_client} {ag.name} accounts reached")

    master_pwd, investor_pwd = generate_mt5_password(), generate_mt5_password()
    login = call(lambda: gw.create_user(
        group=ag.mt5_group, leverage=leverage, name=client.full_name, email=client.email,
        phone=client.phone or "", country=client.country or "",
        master_password=master_pwd, investor_password=investor_pwd), "create account")

    clash = db.scalar(select(TradingAccount).where(TradingAccount.login == login))
    if clash is not None:
        # The MT5 user now exists on the server but the CRM cannot track two accounts with one login.
        log.critical("MT5 login %s created on server %s already belongs to server %s in the CRM",
                     login, ag.server_id, clash.server_id)
        raise ServiceError(f"MT5 returned login {login}, which already exists on another server. Give each MT5 "
                           "server a separate login range (MT5 Administrator > Groups), then try again.", 409)
    acc = TradingAccount(login=login, server_id=ag.server_id, client_id=client.id, account_group_id=ag.id,
                         group=ag.mt5_group, leverage=leverage, currency=ag.currency, is_demo=ag.is_demo,
                         purpose=purpose)
    db.add(acc)
    if ag.is_demo and ag.demo_initial_balance > 0:
        call(lambda: gw.balance_operation(login, ag.demo_initial_balance, "Demo initial balance"), "demo deposit")
    audit(db, actor, "account.open", "trading_account", login,
          {"client_id": client.id, "type": ag.name, "leverage": leverage, "purpose": purpose})
    db.commit()
    refresh_account(db, gw, acc)
    notifications.notify_client(client, "New trading account",
                                f"Your {ag.name} account {login} is ready (server group {ag.mt5_group}, "
                                f"leverage 1:{leverage}). Your password was shown once when the account was opened; "
                                "you can reset it at any time in the client portal.")
    # Passwords are returned once and never stored in the CRM.
    return {"account": acc, "master_password": master_pwd, "investor_password": investor_pwd}


def refresh_account(db: Session, gw: MT5Gateway, acc: TradingAccount) -> TradingAccount:
    info = call(lambda: gw.get_account(acc.login), "account request")
    apply_account_info(acc, info)
    db.commit()
    return acc


def apply_account_info(acc: TradingAccount, info) -> None:
    acc.balance, acc.credit, acc.equity = info.balance, info.credit, info.equity
    acc.margin, acc.margin_free, acc.margin_level = info.margin, info.margin_free, info.margin_level
    acc.last_synced_at = utcnow()


def change_leverage(db: Session, gw: MT5Gateway, actor, acc: TradingAccount, leverage: int) -> TradingAccount:
    if acc.status != AccountStatus.active:
        raise ServiceError("Account is not active")
    if acc.account_group is None:  # no group means no leverage options to check against
        raise ServiceError("This account has no account group; leverage can't be changed")
    options = acc.account_group.leverage_options
    if leverage not in options:
        raise ServiceError(f"Leverage must be one of {options}")
    call(lambda: gw.update_user(acc.login, leverage=leverage), "leverage change")
    before, acc.leverage = acc.leverage, leverage
    audit(db, actor, "account.leverage", "trading_account", acc.login, {"before": before, "after": leverage})
    db.commit()
    return acc


def change_group(db: Session, gw: MT5Gateway, actor, acc: TradingAccount, ag: AccountGroup) -> TradingAccount:
    """Move the account to another account group (and its MT5 group) on the same server."""
    if acc.account_group_id == ag.id:
        return acc
    if ag.server_id != acc.server_id:
        raise ServiceError(f"{ag.name} is on another MT5 server")
    if ag.is_demo != acc.is_demo:
        raise ServiceError("Live and demo accounts can't move between live and demo groups")
    if ag.currency != acc.currency:
        raise ServiceError(f"{ag.name} uses {ag.currency}; the account is in {acc.currency}")
    if not ag.on_server:
        raise ServiceError(f"Group {ag.mt5_group} no longer exists on MT5")
    call(lambda: gw.update_user(acc.login, group=ag.mt5_group), "group change")
    before = acc.group
    acc.group, acc.account_group_id = ag.mt5_group, ag.id
    audit(db, actor, "account.group", "trading_account", acc.login, {"before": before, "after": ag.mt5_group})
    db.commit()
    return acc


def reset_password(db: Session, gw: MT5Gateway, actor, acc: TradingAccount, investor: bool = False,
                   new_password: str | None = None) -> str:
    password = new_password or generate_mt5_password()
    call(lambda: gw.change_password(acc.login, password, investor=investor), "password change")
    audit(db, actor, "account.password_reset", "trading_account", acc.login,
          {"type": "investor" if investor else "master"})
    db.commit()
    return password


def set_trading_enabled(db: Session, gw: MT5Gateway, actor, acc: TradingAccount, enabled: bool) -> TradingAccount:
    call(lambda: gw.update_user(acc.login, trading_enabled=enabled), "rights update")
    acc.trading_enabled = enabled
    audit(db, actor, "account.trading_enabled", "trading_account", acc.login, {"enabled": enabled})
    db.commit()
    return acc


def set_status(db: Session, gw: MT5Gateway, actor, acc: TradingAccount, status: AccountStatus) -> TradingAccount:
    if status == AccountStatus.archived:
        info = call(lambda: gw.get_account(acc.login), "account request")
        if info.equity > 0 or gw.get_positions([acc.login]):
            raise ServiceError("Account must have zero equity and no open positions to archive")
    call(lambda: gw.update_user(acc.login, enabled=status == AccountStatus.active), "rights update")
    before, acc.status = acc.status, status
    audit(db, actor, "account.status", "trading_account", acc.login, {"before": before, "after": status})
    db.commit()
    return acc


# ---------------------------------------------------------------- transfers


def _staff_id(actor) -> int | None:
    return actor.id if isinstance(actor, Staff) else None


def _staff_transfer(db: Session, actor, client: Client, acc: TradingAccount, direction: str, amount, ticket) -> None:
    """A staff member moving a client's money between wallet and MT5 is audited under its own name."""
    if isinstance(actor, Staff):
        audit(db, actor, f"staff_transfer.{direction}", "trading_account", acc.login,
              {"client_id": client.id, "amount": amount, "deal": ticket})


def _notify_staff_transfer(db: Session, actor, client: Client, acc: TradingAccount, direction: str, amount,
                           ticket) -> None:
    """...and the finance approvers (or support, if there are none) are told by email."""
    if not isinstance(actor, Staff):
        return
    what = "wallet -> MT5" if direction == "to_mt5" else "MT5 -> wallet"
    approvers = [s.email for s in db.scalars(select(Staff).where(Staff.is_active.is_(True), Staff.id != actor.id))
                 if s.has_perm("finance.approve")]
    body = (f"{actor.email} moved {amount} {acc.currency} ({what}) for {client.first_name} {client.last_name} "
            f"({client.email}), MT5 account {acc.login}, deal {ticket}.")
    for to in approvers or [get_settings().support_email]:
        notifications.send_email(to, "Staff transfer of client funds", body)


def _check_transferable(acc: TradingAccount) -> None:
    if acc.is_demo:
        raise ServiceError("Transfers are not allowed for demo accounts")
    if acc.status != AccountStatus.active:
        raise ServiceError("Account is not active")
    if acc.purpose == AccountPurpose.pamm_master:
        raise ServiceError("PAMM master accounts are funded through the PAMM module")


def transfer_to_mt5(db: Session, gw: MT5Gateway, actor, client: Client, acc: TradingAccount, amount) -> dict:
    amount = wallet.money(amount)
    if amount <= 0:
        raise ServiceError("Amount must be positive")
    _check_transferable(acc)
    tx = wallet.post(db, client.id, -amount, WalletTxKind.to_mt5, ref_type="mt5", ref_id=acc.login,
                     comment=f"Transfer to MT5 {acc.login}", staff_id=_staff_id(actor))
    try:
        ticket = gw.balance_operation(acc.login, amount, f"CRM wallet tx#{tx.id}")
    except MT5Error as exc:
        db.rollback()
        raise ServiceError(f"MT5 deposit failed: {exc}", 502) from exc
    tx.ref_id = f"{acc.login}:{ticket}"
    _adjust_copy_hwm(db, acc.login, amount)
    audit(db, actor, "transfer.to_mt5", "trading_account", acc.login, {"amount": amount, "deal": ticket})
    _staff_transfer(db, actor, client, acc, "to_mt5", amount, ticket)
    _commit_after_mt5(db, f"to_mt5 login={acc.login} deal={ticket}")
    _notify_staff_transfer(db, actor, client, acc, "to_mt5", amount, ticket)
    return {"wallet_tx_id": tx.id, "mt5_deal": ticket}


def transfer_from_mt5(db: Session, gw: MT5Gateway, actor, client: Client, acc: TradingAccount, amount) -> dict:
    amount = wallet.money(amount)
    if amount <= 0:
        raise ServiceError("Amount must be positive")
    _check_transferable(acc)
    ticket = call(lambda: gw.balance_operation(acc.login, -amount, "CRM transfer to wallet"), "withdrawal")
    tx = wallet.post(db, client.id, amount, WalletTxKind.from_mt5, ref_type="mt5", ref_id=f"{acc.login}:{ticket}",
                     comment=f"Transfer from MT5 {acc.login}", staff_id=_staff_id(actor))
    _adjust_copy_hwm(db, acc.login, -amount)
    audit(db, actor, "transfer.from_mt5", "trading_account", acc.login, {"amount": amount, "deal": ticket})
    _staff_transfer(db, actor, client, acc, "from_mt5", amount, ticket)
    _commit_after_mt5(db, f"from_mt5 login={acc.login} deal={ticket}")
    _notify_staff_transfer(db, actor, client, acc, "from_mt5", amount, ticket)
    return {"wallet_tx_id": tx.id, "mt5_deal": ticket}


def _adjust_copy_hwm(db: Session, login: int, amount: Decimal) -> None:
    from app.services.copy_trading import adjust_high_water_mark

    adjust_high_water_mark(db, login, amount)


def _commit_after_mt5(db: Session, context: str) -> None:
    """The MT5 side already happened; a failed commit now needs manual reconciliation."""
    try:
        db.commit()
    except Exception:
        log.critical("DB commit failed after MT5 balance operation (%s) - reconcile manually", context)
        raise
