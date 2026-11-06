from fastapi import APIRouter, Depends, Form, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ServiceError, get_or_404
from app.db import get_db
from app.models import (
    AccountStatus, AccountGroup, Client, CompactedRecord, CompactSummary, Deal, MT5Server, Position, Staff, TradingAccount,
)
from app.mt5 import MT5Error, for_account_group, routed
from app.services import accounts as svc
from app.services import compact, mt5_calls, mt5_sync, trader_styles
from app.services.clients import ensure_visible, visible_account, visible_accounts, visible_client_ids, visible_rows
from app.services.servers import pick
from app.web import syncs
from app.web.common import (
    Page, attempt, flash, opt_dec, opt_enum, opt_int, redirect, render, req_dec, staff_perm,
)

router = APIRouter()


COMPACT = "/admin/accounts/compact"


@router.get("/accounts/compact")
def compact_page(request: Request, logins: str = "", group: str = "", date_from: str = "", date_to: str = "",
                 by: list[str] = Query([]), preview: str = "", server: str = "", page: int = 1,
                 me: Staff = Depends(staff_perm("mt5_trades.compact")), db: Session = Depends(get_db)):
    """Choose accounts and how to group: the preview only reads MT5."""
    srv = pick(db, server)
    by = [k for k in by if k in compact.GROUPINGS] if preview else list(compact.GROUPINGS)
    plans, error = [], False
    if preview and srv:
        try:
            gw = mt5_calls.gateway(srv)
            plans = [compact.make_plan(db, gw, srv, login, date_from, date_to, by)
                     for login in compact.choose(db, me, srv, logins, opt_int(group))]
        except (ServiceError, MT5Error) as exc:
            error = True
            flash(request, getattr(exc, "message", None) or str(exc), "danger")
    query = (select(CompactSummary).where(visible_rows(me, CompactSummary),
                                          CompactSummary.server_id == (srv.id if srv else 0))
             .order_by(CompactSummary.updated_at.desc(), CompactSummary.id.desc()))
    groups = db.scalars(select(AccountGroup).where(AccountGroup.server_id == (srv.id if srv else 0))
                        .order_by(AccountGroup.name))
    return render(request, "admin/accounts/compact.html", page=Page(db, query, page), plans=plans, preview=preview, server=srv,
                  error=error, logins=logins, group=group, date_from=date_from, date_to=date_to, by=by,
                  groups=list(groups), groupings=compact.GROUPINGS, comment_for=compact.comment_for)


@router.post("/accounts/compact/run")
def compact_run(request: Request, logins: str = Form(""), date_from: str = Form(""), date_to: str = Form(""),
                by: list[str] = Form([]), server_id: str = Form(""), me: Staff = Depends(staff_perm("mt5_trades.compact")),
                db: Session = Depends(get_db)):
    server = pick(db, server_id)
    if server is None:
        flash(request, "No enabled MT5 server", "danger")
        return redirect(COMPACT)
    try:
        chosen = compact.choose(db, me, server, logins)
    except ServiceError as exc:
        flash(request, exc.message, "danger")
        return redirect(COMPACT)
    return syncs.start(request, db, me, "compact", server.id, f"{COMPACT}?server={server.id}",
                       {"logins": chosen, "date_from": date_from, "date_to": date_to,
                        "by": [k for k in by if k in compact.GROUPINGS]})


@router.get("/accounts/compact/history")
def compact_history(request: Request, trigger: str = "", status: str = "", partial: int = 0, page: int = 1,
                    me: Staff = Depends(staff_perm("mt5_trades.compact")), db: Session = Depends(get_db)):
    return syncs.history_page(request, db, "Compact history", ("compact",), "compact", trigger, status, partial, page)


@router.get("/accounts/compact/{summary_id}")
def compact_detail(request: Request, summary_id: int, page: int = 1, me: Staff = Depends(staff_perm("mt5_trades.compact")),
                   db: Session = Depends(get_db)):
    """The exact deals behind a summary."""
    summary = get_or_404(db, CompactSummary, summary_id)
    visible_account(db, me, f"{summary.server_id}:{summary.login}")
    query = select(CompactedRecord).where(CompactedRecord.summary_id == summary.id).order_by(CompactedRecord.time)
    return render(request, "admin/accounts/compact_detail.html", summary=summary, page=Page(db, query, page))


@router.get("/accounts/trading-accounts")
def list_accounts(request: Request, q: str = "", status: str = "", kind: str = "", origin: str = "", server: str = "",
                  page: int = 1, me: Staff = Depends(staff_perm("accounts.view")), db: Session = Depends(get_db)):
    query = select(TradingAccount).where(TradingAccount.client_id.in_(visible_client_ids(me)))
    if server.isdigit():
        query = query.where(TradingAccount.server_id == int(server))
    if q.strip().isdigit():
        query = query.where(TradingAccount.login == int(q.strip()))
    if status:
        query = query.where(TradingAccount.status == status)
    if kind in ("live", "demo"):
        query = query.where(TradingAccount.is_demo.is_(kind == "demo"))
    if origin == "mt5":
        query = query.where(TradingAccount.source == "mt5")
    elif origin == "missing":
        query = query.where(TradingAccount.on_server.is_(False))
    pg = Page(db, query.order_by(TradingAccount.id.desc()), page)
    clients = {c.id: c for c in db.scalars(select(Client).where(Client.id.in_({a.client_id for a in pg.items})))}
    return render(request, "admin/accounts/list.html", page=pg, clients=clients,
                  styles=trader_styles.badges(db, list(pg.items)), q=q, status=status, kind=kind,
                  origin=origin, statuses=list(AccountStatus), server=server, servers=_server_names(db))


@router.post("/clients/{client_id}/accounts")
def open_account(request: Request, client_id: int, account_group_id: int = Form(...), leverage: str = Form(""),
                 me: Staff = Depends(staff_perm("accounts.manage")), db: Session = Depends(get_db)):
    client = ensure_visible(db, me, client_id)
    try:
        result = svc.open_account(db, for_account_group(db, account_group_id), me, client, account_group_id,
                                  opt_int(leverage))
    except (ServiceError, MT5Error) as exc:
        flash(request, getattr(exc, "message", str(exc)), "danger")
        return redirect(f"/admin/clients/{client_id}?tab=accounts")
    # Rendered directly (not via redirect/flash) so passwords never touch the session cookie.
    return render(request, "admin/accounts/credentials.html", account=result["account"], client=client,
                  master_password=result["master_password"], investor_password=result["investor_password"])


@router.get("/accounts/trading-accounts/{login}")
def account_detail(request: Request, login: str, me: Staff = Depends(staff_perm("accounts.view")),
                   db: Session = Depends(get_db)):
    found = visible_accounts(db, me, login)
    if len(found) > 1:  # an old link, with the login alone
        return render(request, "admin/accounts/choose_server.html", found=found)
    acc = svc.only(found, login)
    positions = db.scalars(select(Position).where(Position.server_id == acc.server_id, Position.login == acc.login,
                                                  Position.is_open.is_(True))
                           .order_by(Position.time_create.desc())).all()
    deals = db.scalars(select(Deal).where(Deal.server_id == acc.server_id, Deal.login == acc.login)
                       .order_by(Deal.time.desc(), Deal.ticket.desc()).limit(100)).all()
    return render(request, "admin/accounts/detail.html", a=acc, owner=db.get(Client, acc.client_id),
                  positions=positions, deals=deals, statuses=list(AccountStatus),
                  style=trader_styles.badges(db, [acc]).get(acc.id))


@router.post("/accounts/trading-accounts/{login}/refresh")
def refresh(request: Request, login: str, me: Staff = Depends(staff_perm("accounts.view")), db: Session = Depends(get_db)):
    acc = visible_account(db, me, login)
    return attempt(request, lambda: svc.refresh_account(db, routed(), acc), "Account refreshed from MT5.",
                   f"/admin/accounts/trading-accounts/{login}")


@router.post("/accounts/trading-accounts/{login}/leverage")
def leverage(request: Request, login: str, leverage: int = Form(...),
             me: Staff = Depends(staff_perm("accounts.manage")), db: Session = Depends(get_db)):
    acc = visible_account(db, me, login)
    return attempt(request, lambda: svc.change_leverage(db, routed(), me, acc, leverage),
                   f"Leverage changed to 1:{leverage}.", f"/admin/accounts/trading-accounts/{login}")


@router.post("/accounts/trading-accounts/{login}/password")
def reset_password(request: Request, login: str, which: str = Form("master"),
                   me: Staff = Depends(staff_perm("accounts.manage")), db: Session = Depends(get_db)):
    acc = visible_account(db, me, login)
    try:
        pwd = svc.reset_password(db, routed(), me, acc, investor=which == "investor")
    except (ServiceError, MT5Error) as exc:
        flash(request, getattr(exc, "message", str(exc)), "danger")
        return redirect(f"/admin/accounts/trading-accounts/{login}")
    return render(request, "admin/accounts/credentials.html", account=acc, client=db.get(Client, acc.client_id),
                  reset_kind=which, new_password=pwd)


@router.post("/accounts/trading-accounts/{login}/trading")
def trading(request: Request, login: str, enabled: str = Form(...),
            me: Staff = Depends(staff_perm("accounts.manage")), db: Session = Depends(get_db)):
    acc = visible_account(db, me, login)
    on = enabled == "1"
    return attempt(request, lambda: svc.set_trading_enabled(db, routed(), me, acc, on),
                   "Trading enabled." if on else "Trading disabled.", f"/admin/accounts/trading-accounts/{login}")


@router.post("/accounts/trading-accounts/{login}/status")
def status(request: Request, login: str, status: str = Form(...),
           me: Staff = Depends(staff_perm("accounts.manage")), db: Session = Depends(get_db)):
    acc = visible_account(db, me, login)

    def run():
        svc.set_status(db, routed(), me, acc, opt_enum(AccountStatus, status, "status"))
    return attempt(request, run, "Account status updated.", f"/admin/accounts/trading-accounts/{login}")


@router.post("/accounts/trading-accounts/{login}/transfer")
def transfer(request: Request, login: str, direction: str = Form(...), amount: str = Form(...),
             me: Staff = Depends(staff_perm("accounts.manage", "finance.create")), db: Session = Depends(get_db)):
    acc = visible_account(db, me, login)
    client = db.get(Client, acc.client_id)

    def run():
        value = req_dec(amount)
        fn = svc.transfer_to_mt5 if direction == "to_mt5" else svc.transfer_from_mt5
        fn(db, routed(), me, client, acc, value)
    return attempt(request, run, "Transfer completed.", f"/admin/accounts/trading-accounts/{login}")


# ---------------------------------------------------------------- account groups
@router.post("/accounts/account-groups/sync")
def groups_sync(request: Request, server_id: str = Form(""), me: Staff = Depends(staff_perm("account_groups.manage")),
                db: Session = Depends(get_db)):
    return syncs.start(request, db, me, "account_groups", opt_int(server_id), f"/admin/accounts/account-groups?server={server_id}")


@router.post("/accounts/trading-accounts/sync")
def accounts_sync(request: Request, server_id: str = Form(""), me: Staff = Depends(staff_perm("accounts.manage")),
                  db: Session = Depends(get_db)):
    return syncs.start(request, db, me, "trading_accounts", opt_int(server_id), f"/admin/accounts/trading-accounts?server={server_id}")


@router.get("/accounts/account-groups")
def groups_list(request: Request, server: str = "", me: Staff = Depends(staff_perm("account_groups.manage")),
                db: Session = Depends(get_db)):
    query = select(AccountGroup).order_by(AccountGroup.id)
    if server.isdigit():
        query = query.where(AccountGroup.server_id == int(server))
    return render(request, "admin/accounts/groups.html", groups=db.scalars(query).all(), server=server,
                  servers=_server_names(db))


def _servers(db: Session):
    return db.scalars(select(MT5Server).where(MT5Server.enabled.is_(True)).order_by(MT5Server.is_demo, MT5Server.name)).all()


def _server_names(db: Session) -> dict:
    return dict(db.execute(select(MT5Server.id, MT5Server.name)).tuples().all())


@router.get("/accounts/account-groups/new")
def group_new(request: Request, me: Staff = Depends(staff_perm("account_groups.manage")), db: Session = Depends(get_db)):
    groups = db.execute(select(AccountGroup.mt5_group, MT5Server.name).join(MT5Server, AccountGroup.server)
                        .where(AccountGroup.on_server.is_(True)).order_by(MT5Server.name, AccountGroup.mt5_group)).all()
    return render(request, "admin/accounts/group_form.html", t=None, servers=_servers(db), known_groups=groups)


@router.get("/accounts/account-groups/{group_id}")
def group_edit(request: Request, group_id: int, me: Staff = Depends(staff_perm("account_groups.manage")),
              db: Session = Depends(get_db)):
    t = get_or_404(db, AccountGroup, group_id)
    mt5_warning = mt5_sync.refresh_group(db, t)  # show MT5's current group settings
    return render(request, "admin/accounts/group_form.html", t=t, servers=_servers(db), mt5_warning=mt5_warning,
                  preview=svc.delete_preview(db, t))


def _group_fields(name, mt5_group, is_demo, currency, leverage_options, default_leverage, min_deposit,
                 demo_initial_balance, spread_markup_points, max_accounts_per_client, enabled, description,
                 server_id, margin_call="", stop_out="", open_on_register="") -> dict:
    try:
        levs = sorted({int(x) for x in leverage_options.replace(";", ",").split(",") if x.strip()})
    except ValueError:
        raise ServiceError("Leverage options must be whole numbers separated by commas, e.g. 50, 100, 200")
    if not levs:
        raise ServiceError("At least one leverage option is required")
    return dict(name=name.strip(), mt5_group=mt5_group.strip(), is_demo=is_demo == "on", currency=currency.strip().upper(),
                leverage_options=levs, default_leverage=opt_int(default_leverage) or levs[0],
                min_deposit=opt_dec(min_deposit) or 0, demo_initial_balance=opt_dec(demo_initial_balance) or 0,
                spread_markup_points=opt_int(spread_markup_points) or 0,
                max_accounts_per_client=opt_int(max_accounts_per_client) or 1, enabled=enabled == "on",
                description=description.strip(), server_id=opt_int(server_id),
                margin_call=opt_dec(margin_call), stop_out=opt_dec(stop_out), open_on_register=open_on_register == "on")


@router.post("/accounts/account-groups/new")
def group_create(request: Request, name: str = Form(...), mt5_group: str = Form(...), is_demo: str = Form(""),
                currency: str = Form("USD"), leverage_options: str = Form(...), default_leverage: str = Form(""),
                min_deposit: str = Form("0"), demo_initial_balance: str = Form("10000"),
                spread_markup_points: str = Form("0"), max_accounts_per_client: str = Form("5"),
                enabled: str = Form(""), description: str = Form(""), server_id: str = Form(""),
                margin_call: str = Form(""), stop_out: str = Form(""), open_on_register: str = Form(""),
                create_on_mt5: str = Form(""), template_group: str = Form(""),
                me: Staff = Depends(staff_perm("account_groups.manage")), db: Session = Depends(get_db)):
    def run():
        template = template_group.strip() if create_on_mt5 == "on" else None
        if create_on_mt5 == "on" and not template:
            raise ServiceError("Choose the existing MT5 group to copy settings from")
        svc.create_account_group(db, me, mt5_template=template, **_group_fields(
            name, mt5_group, is_demo, currency, leverage_options, default_leverage, min_deposit, demo_initial_balance,
            spread_markup_points, max_accounts_per_client, enabled, description, server_id, margin_call, stop_out,
            open_on_register))
    done = "Account group created, and its group created on MT5." if create_on_mt5 == "on" else "Account group created."
    return attempt(request, run, done, "/admin/accounts/account-groups")


@router.post("/accounts/account-groups/{group_id}")
def group_update(request: Request, group_id: int, name: str = Form(...), mt5_group: str = Form(...),
                is_demo: str = Form(""), currency: str = Form("USD"), leverage_options: str = Form(...),
                default_leverage: str = Form(""), min_deposit: str = Form("0"),
                demo_initial_balance: str = Form("10000"), spread_markup_points: str = Form("0"),
                max_accounts_per_client: str = Form("5"), enabled: str = Form(""), description: str = Form(""),
                server_id: str = Form(""), margin_call: str = Form(""), stop_out: str = Form(""),
                open_on_register: str = Form(""), me: Staff = Depends(staff_perm("account_groups.manage")), db: Session = Depends(get_db)):
    ag = get_or_404(db, AccountGroup, group_id)

    def run():
        fields = _group_fields(name, mt5_group, is_demo, currency, leverage_options, default_leverage, min_deposit,
                              demo_initial_balance, spread_markup_points, max_accounts_per_client, enabled, description,
                              server_id, margin_call, stop_out, open_on_register)
        for k in ("margin_call", "stop_out"):
            if fields[k] is None:  # left blank: keep the group's value
                fields.pop(k)
        fields.pop("is_demo")  # existing accounts depend on this
        fields.pop("currency")
        svc.update_account_group(db, me, ag, fields)
    return attempt(request, run, "Account group saved, and its MT5 group updated where needed.", "/admin/accounts/account-groups")


@router.post("/accounts/account-groups/{group_id}/delete")
def group_delete(request: Request, group_id: int, accounts_action: str = Form(""), move_to: str = Form(""),
                confirm_name: str = Form(""), me: Staff = Depends(staff_perm("account_groups.manage")),
                db: Session = Depends(get_db)):
    ag = get_or_404(db, AccountGroup, group_id)
    name = ag.name

    def run():
        if accounts_action == "delete" and confirm_name.strip() != name:
            raise ServiceError(f"Type the account group name ({name}) to confirm deleting its accounts")
        done = svc.delete_account_group(db, me, ag, accounts_action or None, opt_int(move_to))
        return (f" {done['moved']} account(s) moved." if done["moved"] else "") + (
            f" {done['deleted']} MT5 login(s) deleted (kept in the CRM as archived)." if done["deleted"] else "")
    return attempt(request, run, f"Account group {name} deleted, with its MT5 group.{{result}}", "/admin/accounts/account-groups")
