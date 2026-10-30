"""Keep each client's MT5 users in step with the CRM client (the CRM is the master for client details).

Every change to a client's details or status is written to all of their MT5 accounts: name, email,
phone, country, city, address, and login rights (a suspended or closed client cannot log in to MT5).
If MT5 cannot be reached the change is still saved in the CRM; the error is shown on the client and
the worker retries until it succeeds.
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import AccountStatus, AccountGroup, Client, ClientStatus, TradingAccount
from app.mt5 import MT5Error, for_account

log = logging.getLogger(__name__)
PLACEHOLDER_EMAIL = "@import.invalid"  # imported MT5 users without an email
# Client fields that exist on the MT5 user.
MT5_FIELDS = {"first_name", "last_name", "email", "phone", "country", "city", "address", "status"}


def mt5_profile(client: Client) -> dict:
    return dict(name=client.full_name, first_name=client.first_name, last_name=client.last_name,
                email="" if client.email.endswith(PLACEHOLDER_EMAIL) else client.email,
                phone=client.phone or "", country=client.country or "", city=client.city or "",
                address=client.address or "")


def push_client(db: Session, client: Client) -> list[str]:
    """Write the client's details to all their MT5 accounts. Returns the errors (empty when all done)."""
    accounts = db.scalars(select(TradingAccount).where(TradingAccount.client_id == client.id,
                                                       TradingAccount.status != AccountStatus.archived,
                                                       TradingAccount.on_server.is_(True))).all()
    blocked = client.status in (ClientStatus.suspended, ClientStatus.closed)
    profile, errors = mt5_profile(client), []
    for acc in accounts:
        try:
            for_account(acc).update_user(acc.login, enabled=not blocked and acc.status == AccountStatus.active,
                                         **profile)
        except MT5Error as exc:
            errors.append(f"{acc.login}: {exc}")
    if errors:
        log.warning("client %s: MT5 update failed for %s", client.id, errors)
    else:
        client.mt5_synced_at = utcnow()
    client.mt5_sync_error = "; ".join(errors)[:255] or None
    db.commit()
    return errors


def retry_failed(db: Session) -> int:
    """Worker: retry clients whose last push failed. Returns how many are now in step."""
    fixed = 0
    for client in db.scalars(select(Client).where(Client.mt5_sync_error.is_not(None)).limit(200)).all():
        fixed += not push_client(db, client)
    return fixed


def open_on_register(db: Session, client: Client) -> list[dict]:
    """Open the accounts marked "open automatically for new clients" (demo groups). Returns the
    open_account results (with the one-time passwords); failures are logged, never block sign-up."""
    from app.mt5 import for_account_group
    from app.services import accounts

    opened = []
    for ag in db.scalars(select(AccountGroup).where(AccountGroup.open_on_register.is_(True),
                                                   AccountGroup.enabled.is_(True), AccountGroup.is_demo.is_(True))):
        try:
            opened.append(accounts.open_account(db, for_account_group(db, ag.id), client, client, ag.id))
        except Exception as exc:  # noqa: BLE001 - registration must succeed even if MT5 is down
            db.rollback()
            log.error("client %s: could not open %s at sign-up: %s", client.id, ag.name, exc)
    return opened
