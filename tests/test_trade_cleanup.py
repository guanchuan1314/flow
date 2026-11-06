"""Trades > Orders or Deals > Controls: delete on MT5 and in the CRM, keeping what belongs to an open position."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import ServiceError
from app.db import utcnow
from app.models import Deal, MT5Server, Order, Position, SyncRun
from app.mt5 import BUY, MT5Error
from app.mt5.gateway import MT5Deal, MT5Order, MT5Position
from app.services import trade_cleanup
from tests.test_web import admin_web, form_post  # noqa: F401

LOGIN = 700001
URL = "/admin/trades/orders/controls"
OPEN_POS, CLOSED_POS = 5001, 5002
FILLED_OPEN, FILLED_CLOSED, PENDING, CANCELLED, REJECTED, EXPIRED_OPEN = 1, 2, 3, 4, 5, 6
DEAL_OPEN, DEAL_CLOSED, DEAL_BALANCE = 11, 12, 13


@pytest.fixture
def trades(db, gw, monkeypatch):
    """Orders and deals of one account, on MT5 and in the CRM: position 5001 is open, 5002 is closed."""
    monkeypatch.setattr(trade_cleanup, "visible_logins", lambda me: [LOGIN])
    server, now = db.get(MT5Server, 1), utcnow()
    orders = [(FILLED_OPEN, 4, OPEN_POS, False), (FILLED_CLOSED, 4, CLOSED_POS, False), (PENDING, 1, 0, True),
              (CANCELLED, 2, 0, False), (REJECTED, 5, OPEN_POS, False), (EXPIRED_OPEN, 6, OPEN_POS, False)]
    for ticket, state, position, is_open in orders:
        mt5 = MT5Order(ticket=ticket, login=LOGIN, symbol="EURUSD", type=0, state=state, volume_initial=Decimal(1),
                       volume_current=Decimal(0), price_order=Decimal(1), time_setup=now, position_id=position)
        (gw.orders.__setitem__(ticket, mt5) if is_open else gw.order_history.append(mt5))
        db.add(Order(server_id=server.id, ticket=ticket, login=LOGIN, symbol="EURUSD", type=0, state=state,
                     time_setup=now, position_id=position, is_open=is_open, synced_at=now))
    for ticket, action, position in ((DEAL_OPEN, 0, OPEN_POS), (DEAL_CLOSED, 0, CLOSED_POS), (DEAL_BALANCE, 2, 0)):
        gw.deals.append(MT5Deal(ticket=ticket, login=LOGIN, time=now, time_msc=0, action=action, entry=0,
                                symbol="EURUSD", volume=Decimal(1), price=Decimal(1), profit=Decimal(0),
                                position_id=position))
        db.add(Deal(server_id=server.id, ticket=ticket, login=LOGIN, time=now, action=action, entry=0, symbol="EURUSD",
                    position_id=position, synced_at=now))
    gw.positions[OPEN_POS] = MT5Position(position_id=OPEN_POS, login=LOGIN, symbol="EURUSD", action=BUY,
                                         volume=Decimal(1), price_open=Decimal(1), price_current=Decimal(1),
                                         sl=Decimal(0), tp=Decimal(0), profit=Decimal(0), swap=Decimal(0),
                                         time_create=now)
    db.add(Position(server_id=server.id, position_id=OPEN_POS, login=LOGIN, symbol="EURUSD", action=0,
                    volume=Decimal(1), price_open=Decimal(1), time_create=now, is_open=True, synced_at=now))
    db.commit()
    return server


def tickets(db, model):
    db.expire_all()
    return set(db.scalars(select(model.ticket)))


def mt5_orders(gw):
    return set(gw.orders) | {o.ticket for o in gw.order_history}


def test_orders_of_an_open_position_stay_unless_they_ended(db, gw, trades):
    r = trade_cleanup.run(db, None, trades, "orders", filters={"login": str(LOGIN)})
    assert (r["done"], r["kept"], r["failed"]) == (5, 1, 0)
    assert tickets(db, Order) == mt5_orders(gw) == {FILLED_OPEN}


def test_deals_of_an_open_position_and_money_deals_stay(db, gw, trades):
    r = trade_cleanup.run(db, None, trades, "deals", tickets=[DEAL_OPEN, DEAL_CLOSED, DEAL_BALANCE])
    assert (r["done"], r["kept"]) == (1, 2)
    assert tickets(db, Deal) == {d.ticket for d in gw.deals} == {DEAL_OPEN, DEAL_BALANCE}


def test_a_position_only_mt5_knows_about_still_protects(db, gw, trades):
    """The CRM's copy says closed, MT5 says open: MT5 wins."""
    db.get(Position, (trades.id, OPEN_POS)).is_open = False
    db.commit()
    r = trade_cleanup.run(db, None, trades, "deals", tickets=[DEAL_OPEN])
    assert (r["done"], r["kept"]) == (0, 1)
    assert DEAL_OPEN in tickets(db, Deal)


def test_what_mt5_refuses_stays_in_the_crm(db, gw, trades, monkeypatch):
    monkeypatch.setattr(gw, "delete_orders", lambda t: {CANCELLED: "No permission"})
    r = trade_cleanup.run(db, None, trades, "orders", tickets=[CANCELLED, EXPIRED_OPEN])
    assert (r["done"], r["failed"], r["errors"]) == (1, 1, [f"{CANCELLED}: No permission"])
    assert CANCELLED in tickets(db, Order) and EXPIRED_OPEN not in tickets(db, Order)


def test_when_mt5_is_unreachable_nothing_is_deleted(db, gw, trades, monkeypatch):
    def down(logins):
        raise MT5Error("Network error")
    monkeypatch.setattr(gw, "get_positions", down)
    r = trade_cleanup.run(db, None, trades, "orders", tickets=[CANCELLED])
    assert (r["done"], r["failed"]) == (0, 1)
    assert CANCELLED in tickets(db, Order) and CANCELLED in mt5_orders(gw)


def test_delete_all_needs_a_filter(db, gw, trades):
    with pytest.raises(ServiceError, match="at least one filter"):
        trade_cleanup.run(db, None, trades, "orders", filters={"status": "", "login": ""})


def test_filters(db, gw, trades):
    def found(what, **filters):
        model, query = (Order, trade_cleanup.orders_query) if what == "orders" else (Deal, trade_cleanup.deals_query)
        return set(db.scalars(query(trades.id, None, filters).with_only_columns(model.ticket)))
    assert found("orders", status="cancelled") == {CANCELLED}
    assert found("orders", status="open") == {PENDING}
    assert found("orders", status="filled") == {FILLED_OPEN, FILLED_CLOSED}
    assert found("orders", login="7000*", status="rejected") == {REJECTED}
    assert found("orders", login="999") == set()
    assert found("orders", date_to="2000-01-01") == set()
    assert found("orders", date_from="not a date") == set()  # never widens a delete
    assert found("deals", status="open") == {DEAL_OPEN}
    assert found("deals", status="closed") == {DEAL_CLOSED}
    assert found("deals", status="balance") == {DEAL_BALANCE}


def test_page_and_background_run(admin_web, db, gw, trades):  # noqa: F811
    page = admin_web.get(f"{URL}?status=&login={LOGIN}").text
    assert "<strong>6</strong> found · <strong>5</strong> can be deleted · 1 kept" in page
    assert f'name="ticket" value="{FILLED_OPEN}"' not in page and f'name="ticket" value="{CANCELLED}"' in page
    assert "its position is still open" in page and "Delete all 5 that match" in page

    r = form_post(admin_web, URL, f"{URL}/run", {"everything": "1", "status": "cancelled"})
    assert "deleted 1 orders on MT5 and in the CRM" in r.text
    db.expire_all()
    run = db.scalar(select(SyncRun))
    assert (run.kind, run.status) == ("trade_cleanup", "done")
    assert tickets(db, Order) == mt5_orders(gw) == {FILLED_OPEN, FILLED_CLOSED, PENDING, REJECTED, EXPIRED_OPEN}
    audit = admin_web.get("/admin/security/audit").text
    assert "Orders deleted on MT5 and in the CRM" in audit
    assert "Delete orders and deals" in admin_web.get("/admin/import/history").text


@pytest.mark.parametrize("what,done", [("positions", " open positions"),("deals", " new deals"),
                                       ("orders", " open orders, ")])
def test_each_sync_tab_syncs_its_own_data(admin_web, db, gw, trades, what, done):  # noqa: F811
    url = f"/admin/trades/{what}/sync"
    r = form_post(admin_web, url, url, {})
    assert done in r.text and r.url.path == url
    db.expire_all()
    assert (db.scalar(select(SyncRun.kind)), db.scalar(select(SyncRun.status))) == (what, "done")


def test_the_sync_page_also_opens_under_import_and_stays_there(admin_web, db, gw, trades):  # noqa: F811
    url = "/admin/import/orders"
    page = admin_web.get(url).text
    assert "Sync orders now" in page and 'nav-link active" href="/admin/import/orders"' in page
    assert ">Controls<" not in page
    assert admin_web.get("/admin/import/nothing-here").status_code == 404
    r = form_post(admin_web, url, "/admin/trades/orders/sync", {}, headers={"referer": f"http://testserver{url}"})
    assert r.url.path == url


def test_comment_filter_also_limits_delete_all(admin_web, db, gw, trades, monkeypatch):  # noqa: F811
    monkeypatch.setattr("app.web.admin.trading.visible_logins", lambda me: [LOGIN])
    db.get(Order, (trades.id, CANCELLED)).comment = "copy#1 close"
    db.get(Order, (trades.id, REJECTED)).comment = "manual"
    db.get(Deal, (trades.id, DEAL_CLOSED)).comment = "copy#2 close"
    db.commit()

    def found(query, model, **filters):
        return set(db.scalars(query(trades.id, None, filters).with_only_columns(model.ticket)))
    assert found(trade_cleanup.orders_query, Order, comment="copy*") == {CANCELLED}
    assert found(trade_cleanup.orders_query, Order, comment="*CLOSE") == {CANCELLED}
    assert found(trade_cleanup.orders_query, Order, comment="MANUAL") == {REJECTED}
    assert found(trade_cleanup.orders_query, Order, comment="close") == set()  # no wildcard: the whole comment
    assert found(trade_cleanup.deals_query, Deal, comment="*close*") == {DEAL_CLOSED}
    assert f">{CANCELLED}<" in admin_web.get("/admin/trades/orders/data?status=history&comment=copy*").text

    page = admin_web.get(f"{URL}?comment=copy*").text
    assert "<strong>1</strong> found" in page and 'name="comment" value="copy*"' in page
    form_post(admin_web, URL, f"{URL}/run", {"everything": "1", "comment": "copy*"})
    assert tickets(db, Order) == mt5_orders(gw) == {FILLED_OPEN, FILLED_CLOSED, PENDING, REJECTED, EXPIRED_OPEN}


def test_history_tab_shows_only_its_own_runs(admin_web, db, gw, trades):  # noqa: F811
    def runs(url):
        text = admin_web.get(url).text
        return text[text.find('id="sync-runs"'):]
    form_post(admin_web, "/admin/trades/positions/sync", "/admin/trades/positions/sync", {})
    assert "Open positions<div" in runs("/admin/trades/positions/history")
    assert "No synchronizations yet" in runs("/admin/trades/deals/history")
    form_post(admin_web, "/admin/trades/deals/sync", "/admin/trades/sync/all", {})
    full = runs("/admin/trades/deals/history")
    assert "Trades<div" in full and "Open positions<div" not in full
    assert "Open positions<div" not in runs("/admin/trades/positions/history?kind=trades")  # the same filters

    page = admin_web.get("/admin/trades/positions/history").text
    choices = page[page.find('name="kind"'):page.find('name="trigger"')]
    assert ">Open positions<" in choices and ">Trades<" in choices and ">Deals<" not in choices
    assert 'name="trigger"' in page and 'name="status"' in page and "Every sync from MT5" not in page
    assert "Every sync from MT5" in admin_web.get("/admin/import/history").text
    assert 'data-tab="Sync history"' not in admin_web.get("/admin/trades/positions/sync").text  # now a tab of its own

    partial = admin_web.get("/admin/trades/positions/history?partial=1").text
    assert partial.lstrip().startswith('<div id="sync-runs"') and "crm-sidebar" not in partial
    assert admin_web.get("/admin/trades/nothing/history").status_code == 404


def test_sync_page_cards_are_the_same_height(admin_web, trades):  # noqa: F811
    page = admin_web.get("/admin/trades/deals/sync").text
    assert page.count('class="card kpi h-100"') == 4 and 'class="card kpi"' not in page


def test_roles_that_get_the_tool():
    from app.core.permissions import DEFAULT_ROLES

    assert "*" in DEFAULT_ROLES["Admin"]  # trading.* does not cover it: Managers and Dealers need it given to them
    assert not any(p in ("mt5_trades.delete", "mt5_trades.*") for perms in DEFAULT_ROLES.values() for p in perms)
