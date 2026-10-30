"""In-process event bus for newly ingested deals.

Handlers receive (session, new_deals) after the deals are committed and own their
own commits. A failing handler is logged and rolled back without affecting others.

Delivery gap: handlers run in memory right after the commit, with no outbox. If the
process dies between the deal commit and a handler finishing, or a handler fails, that
handler does not see those deals again (the poller re-reads them but they are no longer
new). Handlers therefore must be idempotent and have their own catch-up where it
matters (commissions.recalculate re-derives IB commissions for a period).
"""

import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.models import Deal

log = logging.getLogger(__name__)

DealHandler = Callable[[Session, list[Deal]], None]
_handlers: dict[str, DealHandler] = {}


def register(name: str, handler: DealHandler) -> None:
    _handlers[name] = handler


def unregister(name: str) -> None:
    _handlers.pop(name, None)


def publish(db: Session, deals: list[Deal]) -> None:
    for name, handler in list(_handlers.items()):
        try:
            handler(db, deals)
        except Exception:
            log.exception("deal handler %s failed for %d deal(s) (tickets %s)", name, len(deals),
                          [d.ticket for d in deals[:20]])
            try:
                db.rollback()
            except Exception:  # noqa: BLE001  - a broken connection must not stop the next handler
                log.exception("rollback after deal handler %s failed", name)


def register_default_handlers() -> None:
    from app.services import commissions, copy_trading

    register("copy_trading", copy_trading.handle_deals)
    register("ib_commissions", commissions.handle_deals)
