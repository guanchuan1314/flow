"""Closed positions, rebuilt from their deals: the deals that opened a position (in) and those that closed it (out).

Flow keeps no table of them. A position counts as closed when deals took volume out of it and it is not among the open
positions, so one that is partly closed stays with the open ones until the rest goes. The exact copies of deals Compact
removed count too, so compacting an account does not change this list.
"""

from sqlalchemy import BigInteger, Integer, Numeric, and_, case, cast, exists, func, select, union_all
from sqlalchemy.orm import Session

from app.models import CompactedRecord, Deal, Position
from app.models.trading import ENTRY_IN

TRADES = (0, 1)  # buy, sell


def _deals():
    """Trade deals, from the deals table and from the copies of compacted ones."""
    field = lambda key, kind=Numeric: cast(CompactedRecord.data[key].as_string(), kind).label(key)  # noqa: E731
    return union_all(
        select(Deal.server_id, Deal.position_id, Deal.login, Deal.symbol, Deal.time, Deal.action, Deal.entry,
               Deal.volume, Deal.price, Deal.profit, Deal.commission, Deal.swap, Deal.fee, Deal.comment)
        .where(Deal.action.in_(TRADES), Deal.position_id != 0),
        select(CompactedRecord.server_id, field("position_id", BigInteger), CompactedRecord.login, CompactedRecord.symbol,
               CompactedRecord.time, CompactedRecord.action, field("entry", Integer), field("volume"), field("price"),
               field("profit"), field("commission"), field("swap"), field("fee"), CompactedRecord.comment)
        .where(CompactedRecord.kind == "deal", CompactedRecord.action.in_(TRADES),
               cast(CompactedRecord.data["position_id"].as_string(), BigInteger) != 0),
    ).subquery("trade_deals")


def query(narrow=lambda q, deals: q, server: int | None = None, position: int | None = None, closed_from=None,
          closed_to=None):
    """One row per closed position, the last closed first. `narrow(query, deals)` filters the deals."""
    d = _deals()
    opening, closing = d.c.entry == ENTRY_IN, d.c.entry != ENTRY_IN
    volume_in = func.sum(case((opening, d.c.volume), else_=0))
    volume_out = func.sum(case((closing, d.c.volume), else_=0))
    closed_at = func.max(case((closing, d.c.time)))

    def average(side, total):
        return func.sum(case((side, d.c.price * d.c.volume), else_=0)) / func.nullif(total, 0)

    q = select(
        d.c.server_id, d.c.position_id, func.min(d.c.login).label("login"), func.min(d.c.symbol).label("symbol"),
        # the deal that closes a buy is a sell: without its opening deal, the side comes from the closing one
        func.coalesce(func.min(case((opening, d.c.action))), 1 - func.min(d.c.action)).label("action"),
        func.min(case((opening, d.c.time))).label("opened_at"), closed_at.label("closed_at"),
        volume_out.label("volume"), average(opening, volume_in).label("price_open"),
        average(closing, volume_out).label("price_close"), func.sum(d.c.commission).label("commission"),
        func.sum(d.c.swap).label("swap"), func.sum(d.c.fee).label("fee"), func.sum(d.c.profit).label("profit"),
    ).group_by(d.c.server_id, d.c.position_id).having(volume_out > 0)
    q = narrow(q.where(~exists().where(and_(Position.server_id == d.c.server_id, Position.position_id == d.c.position_id,
                                            Position.is_open.is_(True)))), d.c)
    if server is not None:
        q = q.where(d.c.server_id == server)
    if position is not None:
        q = q.where(d.c.position_id == position)
    if closed_from is not None:
        q = q.having(closed_at >= closed_from)
    if closed_to is not None:
        q = q.having(closed_at <= closed_to)
    return q.order_by(closed_at.desc(), d.c.position_id.desc())


def totals(db: Session, q) -> tuple:
    """Lots, commission, swap and profit of all the rows of query()."""
    rows = q.order_by(None).subquery()
    return db.execute(select(func.coalesce(func.sum(rows.c.volume), 0), func.coalesce(func.sum(rows.c.commission), 0),
                             func.coalesce(func.sum(rows.c.swap), 0), func.coalesce(func.sum(rows.c.profit), 0))).one()
