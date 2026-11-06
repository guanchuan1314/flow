"""Accounts that are not on MT5: Flow keeps nothing of them.

After each import of trading accounts, the accounts flagged `on_server = False` are checked with MT5 one by one, and
those it confirms it does not have go from Flow with their deals, orders, positions and compacted history. That also
covers accounts Flow itself deleted or archived on MT5. Deals and orders of an account MT5 still has are never touched
here, even when MT5 no longer has them.

- An account still in a copy trading or PAMM role stays until that is stopped.
- Its client goes too if it was only an imported placeholder with nothing left: no other account, nothing in its
  wallet, and no payment, IB or copy trading record. Real clients stay, as do payments and IB commissions.
"""

import logging

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.models import (
    AutomationRun, Client, CompactedRecord, CompactSummary, Deal, MT5Server, Order, Position, SyncState, TradingAccount,
    Wallet,
)
from app.mt5 import MT5Error, MT5Gateway
from app.mt5.gateway import RET_NOT_FOUND
from app.services.accounts import _copy_links
from app.services.client_sync import PLACEHOLDER_EMAIL
from app.services.compact import saved
from app.services.sync_runs import report

log = logging.getLogger(__name__)

BATCH = 200


def _gone(gw: MT5Gateway, logins: list[int]) -> list[int]:
    """Those MT5 says it does not have. Any other answer raises: not knowing is not a reason to delete."""
    gone = []
    for login in logins:
        try:
            gw.get_user(login)
        except MT5Error as exc:
            if exc.retcode != RET_NOT_FOUND:
                raise
            gone.append(login)
    return gone


def _remove_accounts(db: Session, server: MT5Server, logins: list[int]) -> None:
    def work():
        for model in (CompactedRecord, CompactSummary, Deal, Order, Position, TradingAccount):
            db.execute(delete(model).where(model.server_id == server.id, model.login.in_(logins)))
        for model in (SyncState, AutomationRun):
            db.execute(delete(model).where(model.server_id == server.id, model.login.in_(logins)))
    saved(db, work)


def _remove_placeholder(db: Session, client_id: int) -> bool:
    """Delete an imported placeholder client with nothing left. Anything else pointing at it keeps it."""
    client = db.get(Client, client_id)
    if client is None or not client.email.endswith(PLACEHOLDER_EMAIL):
        return False
    if db.scalar(select(TradingAccount.id).where(TradingAccount.client_id == client_id).limit(1)):
        return False
    if db.scalar(select(Wallet.id).where(Wallet.client_id == client_id, Wallet.balance != 0).limit(1)):
        return False
    db.expunge(client)
    try:
        with db.begin_nested():
            db.execute(delete(Client).where(Client.id == client_id))
    except IntegrityError:  # a payment, IB or copy trading record still needs it
        db.commit()
        return False
    db.commit()
    return True


def remove_gone(db: Session, server: MT5Server, gw: MT5Gateway, actor=None) -> dict:
    """Remove from Flow the accounts flagged as not on MT5, once MT5 has confirmed it."""
    result = {"removed": 0, "clients": 0, "kept": 0, "error": ""}
    flagged = list(db.scalars(select(TradingAccount.login).where(TradingAccount.server_id == server.id,
                                                                 TradingAccount.on_server.is_(False))))
    if not flagged:
        return result
    try:
        gone = _gone(gw, flagged)
    except MT5Error as exc:
        log.warning("server %s: accounts not removed: %s", server.name, exc)
        result["error"] = str(exc)
        return result
    linked = {int(role.rsplit(" ", 1)[1]) for role in _copy_links(db, server.id, gone)} if gone else set()
    gone = [login for login in gone if login not in linked]
    result["kept"] = len(flagged) - len(gone)
    report(0, len(gone))
    for start in range(0, len(gone), BATCH):
        batch = gone[start:start + BATCH]
        clients = set(db.scalars(select(TradingAccount.client_id).where(TradingAccount.server_id == server.id,
                                                                        TradingAccount.login.in_(batch))))
        _remove_accounts(db, server, batch)
        result["removed"] += len(batch)
        result["clients"] += sum(_remove_placeholder(db, c) for c in clients)
        report(start + len(batch), len(gone))
    if gone:
        saved(db, lambda: audit(db, actor, "account.removed_from_flow", "mt5_server", server.id,
                                {"logins": gone[:1000], "accounts": len(gone), "clients": result["clients"]}))
        log.info("server %s: removed %d accounts not on MT5 and %d clients", server.name, len(gone), result["clients"])
    return result
