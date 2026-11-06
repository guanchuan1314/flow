"""Sample custom module: keeps a list of big deals and shows it in the back-office.

It uses every way a module plugs in. Copy this folder to custom/big_deals/ and restart Flow to try it.
"""

from sqlalchemy import text

from .pages import router

MIN_LOTS = 10
KEEP_DAYS = 90


def record(db, deals) -> None:
    """Runs after new deals are saved. ON CONFLICT: a deal seen twice is kept once."""
    for deal in deals:
        if deal.volume >= MIN_LOTS:
            db.execute(text("INSERT INTO custom_big_deals (ticket, login, symbol, lots, time) "
                            "VALUES (:ticket, :login, :symbol, :lots, :time) ON CONFLICT (ticket) DO NOTHING"),
                       dict(ticket=deal.ticket, login=deal.login, symbol=deal.symbol, lots=deal.volume, time=deal.time))
    db.commit()


def forget_old(db) -> None:
    db.execute(text("DELETE FROM custom_big_deals WHERE time < now() - make_interval(days => :days)"), {"days": KEEP_DAYS})
    db.commit()


def setup(crm) -> None:
    crm.add_permission("big_deals.view", "View the list of big deals")
    crm.add_admin_routes(router)
    crm.add_menu("Big deals", "/admin/custom/big_deals", "trophy", "big_deals.view", section="Desk")
    crm.on_deals(record)
    crm.every(3600, forget_old)
