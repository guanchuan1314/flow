from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import ServiceError, get_or_404
from app.db import get_db
from app.models import MT5Server, ServerKind, Staff
from app.mt5 import peek
from app.services import sync_runs
from app.services import servers as svc
from app.web import syncs
from app.web.common import (
    WebForbidden, attempt, back, flash, form_rows, opt_enum, opt_int, redirect, render, sent_from, staff_perm,
    web_staff,
)

router = APIRouter()
PERM = "mt5_servers.manage"


def _form_values(form) -> dict:
    return dict(
        name=str(form.get("name", "")), kind=opt_enum(ServerKind, str(form.get("kind", "manager")), "server type"),
        address=str(form.get("address", "")), manager_login=opt_int(form.get("manager_login")),
        password=str(form.get("password") or "") or None, terminal_name=str(form.get("terminal_name", "")),
        timeout_ms=(opt_int(form.get("timeout_seconds")) or 30) * 1000, enabled=form.get("enabled") == "on",
        notes=str(form.get("notes", "")), price_feed=form.get("price_feed") == "on",
    )


SERVERS = "/admin/servers/list"


def _chosen(db: Session, server: str) -> MT5Server:
    """The server a page is on (enabled or not), or 404 before one is set up."""
    return get_or_404(db, MT5Server, getattr(svc.pick(db, server, enabled_only=False), "id", 0), "MT5 server")


def _import_page(request: Request, db: Session, what: str, server: str):
    """MT5 > Imports: account groups (MT5 groups) or trading accounts (MT5 users)."""
    return render(request, "admin/servers/import.html", s=svc.pick(db, server, enabled_only=False), what=what)


@router.post("/import/account-groups/sync")
def import_groups_run(request: Request, server_id: str = Form(""), me: Staff = Depends(staff_perm(PERM)),
                      db: Session = Depends(get_db)):
    chosen = syncs.server_id(db, server_id)
    return syncs.start(request, db, me, "account_groups", chosen, f"/admin/import/account-groups?server={chosen or ''}")


@router.post("/import/trading-accounts/sync")
def import_accounts_run(request: Request, server_id: str = Form(""), me: Staff = Depends(staff_perm(PERM)),
                        db: Session = Depends(get_db)):
    chosen = syncs.server_id(db, server_id)
    return syncs.start(request, db, me, "trading_accounts", chosen, f"/admin/import/trading-accounts?server={chosen or ''}")


# ---------------------------------------------------------------- synchronization history
HISTORY_PERMS = ("mt5_servers.manage", "account_groups.manage", "accounts.manage", "accounts.cleanup", "trading.sync",
                 "mt5_trades.delete",
                 "symbols.manage", "liquidity.manage", "routing.manage")


def history_staff(staff: Staff = Depends(web_staff)) -> Staff:
    if not any(staff.has_perm(p) for p in HISTORY_PERMS):
        raise WebForbidden()
    return staff


@router.get("/import/history")
def sync_history(request: Request, kind: str = "", trigger: str = "", status: str = "", partial: int = 0,
                 page: int = 1, me: Staff = Depends(history_staff), db: Session = Depends(get_db)):
    """Every synchronization from MT5: what, which server, started by whom (or the schedule), how it went."""
    return syncs.history_page(request, db, kind=kind, trigger=trigger, status=status, partial=partial, page=page)


@router.get("/import/history/panel")
def sync_panel(request: Request, kinds: str = "", live: int = 0, me: Staff = Depends(history_staff)):
    """A page's progress panel alone (macros.sync_panel): polled every 2 s while one of its syncs runs."""
    wanted = [k for k in kinds.split(",") if k in sync_runs.KINDS]
    return render(request, "admin/servers/sync_panel.html", kinds=wanted, live=bool(live))


@router.get("/import/account-groups")
def import_groups(request: Request, server: str = "", me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    return _import_page(request, db, "groups", server)


@router.get("/import/trading-accounts")
def import_accounts(request: Request, server: str = "", me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    return _import_page(request, db, "accounts", server)


@router.get("/servers/list")
def server_list(request: Request, me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    """Every MT5 server Flow connects to."""
    servers = db.scalars(select(MT5Server).order_by(MT5Server.id)).all()
    return render(request, "admin/servers/list.html", servers=servers, usage=svc.usage(db))


@router.get("/servers/list/new")
def server_new(request: Request, me: Staff = Depends(staff_perm(PERM))):
    return render(request, "admin/servers/form.html", s=None, simulators_allowed=not get_settings().is_prod)


@router.post("/servers/list/new")
async def server_create(request: Request, me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    form = await request.form()
    created = {}

    def run():
        created["s"] = svc.create_server(db, me, **_form_values(form))
    resp = attempt(request, run, None, f"{SERVERS}/new")
    if "s" not in created:
        return resp
    server = created["s"]
    ok, message = svc.test_server(db, me, server)  # test straight away so the admin sees the result
    flash(request, f"Server '{server.name}' saved. Connection test: {message}", "success" if ok else "warning")
    return redirect(SERVERS)


@router.get("/servers/list/{server_id:int}")
def server_edit(request: Request, server_id: int, me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    server = get_or_404(db, MT5Server, server_id, "MT5 server")
    return render(request, "admin/servers/form.html", s=server, usage=svc.usage(db).get(server.id, {}),
                  crm_data=svc.crm_data(db, server), connected=bool(peek(server.id) and peek(server.id).connected),
                  simulators_allowed=not get_settings().is_prod)


@router.post("/servers/list/{server_id:int}")
async def server_update(request: Request, server_id: int, me: Staff = Depends(staff_perm(PERM)),
                        db: Session = Depends(get_db)):
    server = get_or_404(db, MT5Server, server_id, "MT5 server")
    form = await request.form()
    return attempt(request, lambda: svc.update_server(db, me, server, **_form_values(form)),
                   "Server settings saved. Connections reconnect with the new settings.", f"{SERVERS}/{server_id}")


@router.post("/servers/list/{server_id:int}/test")
def server_test(request: Request, server_id: int, me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    server = get_or_404(db, MT5Server, server_id, "MT5 server")
    ok, message = svc.test_server(db, me, server)
    flash(request, f"{server.name}: {message}", "success" if ok else "danger")
    return back(request, SERVERS)


@router.post("/servers/list/{server_id:int}/delete")
def server_delete(request: Request, server_id: int, purge: str = Form(""), confirm_name: str = Form(""),
                  me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    server = get_or_404(db, MT5Server, server_id, "MT5 server")
    name = server.name

    def run():
        if purge == "on" and confirm_name.strip() != name:
            raise ServiceError(f"Type the server name ({name}) to confirm deleting its CRM data")
        svc.delete_server(db, me, server, purge=purge == "on")
    return attempt(request, run, f"Server '{name}' removed from the CRM. Nothing was changed on the MT5 server.",
                   SERVERS)


# ---------------------------------------------------------------- restart components
@router.get("/servers/restart")
def restart_page(request: Request, server: str = "", me: Staff = Depends(staff_perm("mt5_servers.restart")),
                 db: Session = Depends(get_db)):
    from app.mt5 import MT5Error
    from app.mt5.gateway import COMPONENT_TYPES
    from app.services import server_health

    srv = _chosen(db, server)
    components, error = [], None
    try:
        components = server_health.live_components(srv)
    except MT5Error as exc:
        error = f"Could not read the server's components from MT5: {exc}"
    return render(request, "admin/servers/restart.html", s=srv, components=components, mt5_warning=error,
                  types=COMPONENT_TYPES, effects=server_health.RESTART_EFFECT)


@router.post("/servers/restart/{server_id:int}")
def restart_component(request: Request, server_id: int, component_id: int = Form(...), confirm_name: str = Form(""),
                      me: Staff = Depends(staff_perm("mt5_servers.restart")), db: Session = Depends(get_db)):
    from app.services import server_health

    server = get_or_404(db, MT5Server, server_id, "MT5 server")
    page = f"/admin/servers/restart?server={server_id}"

    def run():
        main = next((c for c in server_health.live_components(server) if c.id == component_id and c.type == 0), None)
        if main is not None and confirm_name.strip() != main.name:
            raise ServiceError(f"Type the main trade server's name ({main.name}) to confirm restarting it")
        return server_health.restart(db, me, server, component_id)
    return attempt(request, run, "{result} is restarting on MT5. Its connections come back within a minute or so; "
                   "Server health shows when it is up again.", page)


# ---------------------------------------------------------------- symbols & pricing
PRICING = "/admin/import/pricing"


@router.get("/servers/pricing")
def old_pricing_page():
    return redirect(PRICING)


@router.get("/symbols/sync")
@router.get("/import/pricing")
def pricing_page(request: Request, server: str = "", me: Staff = Depends(staff_perm("symbols.manage")),
                 db: Session = Depends(get_db)):
    from app.services import pricing

    srv = _chosen(db, server)
    pending, error = None, None
    try:
        pending = pricing.preview(db, srv)
    except (ServiceError, Exception) as exc:  # noqa: BLE001 - the page still shows the settings
        error = f"Could not read the groups from MT5 ({getattr(exc, 'message', exc)}): what's pending isn't known."
    return render(request, "admin/servers/pricing.html", s=srv, p=pricing.settings(srv), pending=pending,
                  mt5_warning=error)


@router.post("/import/pricing/{server_id:int}")
async def pricing_save(request: Request, server_id: int, me: Staff = Depends(staff_perm("symbols.manage")),
                       db: Session = Depends(get_db)):
    from app.services import pricing

    server = get_or_404(db, MT5Server, server_id, "MT5 server")
    form = await request.form()

    def run():
        rules = pricing.parse_rules(form_rows(form, "spread_groups", "spread_symbols", "spread_points"),
                                    form_rows(form, "comm_groups", "comm_symbols", "comm_per_lot"),
                                    form_rows(form, "swap_symbols", "swap_long", "swap_short"))
        summary = pricing.save(db, me, server, rules, form.get("auto_enabled") == "on",
                               opt_int(form.get("auto_minutes")))
        return "Saved. " + pricing.describe(summary)
    return attempt(request, run, "{result}", sent_from(request, f"{PRICING}?server={server_id}"))


@router.post("/import/pricing/{server_id:int}/apply")
def pricing_apply(request: Request, server_id: int, me: Staff = Depends(staff_perm("symbols.manage")),
                  db: Session = Depends(get_db)):
    from app.services import pricing, symbols

    server = get_or_404(db, MT5Server, server_id, "MT5 server")

    return syncs.start(request, db, me, "pricing", server.id, sent_from(request, f"{PRICING}?server={server_id}"))
