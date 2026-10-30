from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.deps import client_ip
from app.core.errors import ServiceError, get_or_404
from app.db import get_db
from app.models import MT5Server, ServerKind, Staff
from app.mt5 import peek
from app.services import market, mt5_sync
from app.services import servers as svc
from app.web.common import (
    attempt, back, flash, form_rows, opt_enum, opt_int, redirect, render, staff_perm, sync_message,
)

router = APIRouter()
PERM = "mt5_servers.manage"


def _form_values(form) -> dict:
    return dict(
        name=str(form.get("name", "")), kind=opt_enum(ServerKind, str(form.get("kind", "manager")), "server type"),
        address=str(form.get("address", "")), manager_login=opt_int(form.get("manager_login")),
        password=str(form.get("password") or "") or None, terminal_name=str(form.get("terminal_name", "")),
        timeout_ms=(opt_int(form.get("timeout_seconds")) or 30) * 1000, enabled=form.get("enabled") == "on",
        notes=str(form.get("notes", "")),
    )


CONFIG = "/admin/configuration"


def _only(db: Session) -> MT5Server:
    """The MT5 server (enabled or not), or 404 before it is set up."""
    return get_or_404(db, MT5Server, getattr(svc.the_server(db, enabled_only=False), "id", 0), "MT5 server")


IMPORTS = {"account-groups": "groups", "trading-accounts": "accounts"}  # MT5 > Import from MT5 pages


@router.get("/import/{page}")
def import_page(request: Request, page: str, me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    """MT5 > Import from MT5: account groups (MT5 groups) or trading accounts (MT5 users)."""
    if page not in IMPORTS:
        return redirect("/admin/import/trading-accounts")
    return render(request, "admin/servers/import.html", s=svc.the_server(db, enabled_only=False), what=IMPORTS[page])


@router.get("/configuration")
def configuration(request: Request, me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    """The MT5 server's details (the CRM has one): connection, manager login, imports. Before it is set up,
    the same page adds it."""
    server = svc.the_server(db, enabled_only=False)
    if server is None:
        return render(request, "admin/servers/form.html", s=None, simulators_allowed=not get_settings().is_prod)
    return render(request, "admin/servers/form.html", s=server, usage=svc.usage(db).get(server.id, {}),
                  crm_data=svc.crm_data(db, server), connected=bool(peek(server.id) and peek(server.id).connected),
                  simulators_allowed=not get_settings().is_prod)


@router.get("/mt5-servers")
def server_list(request: Request, me: Staff = Depends(staff_perm(PERM))):
    return redirect(CONFIG)  # one server: its page is Configuration


@router.get("/mt5-servers/new")
def server_new(request: Request, me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    existing = svc.the_server(db, enabled_only=False)
    if existing is not None:  # one MT5 server only
        flash(request, svc.ONE_SERVER.format(name=existing.name), "info")
    return redirect(CONFIG)


@router.post("/mt5-servers/new")
async def server_create(request: Request, me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    form = await request.form()
    created = {}

    def run():
        created["s"] = svc.create_server(db, me, **_form_values(form))
    resp = attempt(request, run, None, CONFIG)
    if "s" not in created:
        return resp
    server = created["s"]
    ok, message = svc.test_server(db, me, server)  # test straight away so the admin sees the result
    flash(request, f"Server '{server.name}' saved. Connection test: {message}", "success" if ok else "warning")
    return redirect(CONFIG)


@router.get("/mt5-servers/{server_id}")
def server_edit(request: Request, server_id: int, me: Staff = Depends(staff_perm(PERM))):
    return redirect(CONFIG)


@router.post("/mt5-servers/{server_id}")
async def server_update(request: Request, server_id: int, me: Staff = Depends(staff_perm(PERM)),
                        db: Session = Depends(get_db)):
    server = get_or_404(db, MT5Server, server_id, "MT5 server")
    form = await request.form()
    return attempt(request, lambda: svc.update_server(db, me, server, **_form_values(form)),
                   "Server settings saved. Connections reconnect with the new settings.", CONFIG)


@router.post("/mt5-servers/{server_id}/test")
def server_test(request: Request, server_id: int, me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    server = get_or_404(db, MT5Server, server_id, "MT5 server")
    ok, message = svc.test_server(db, me, server)
    flash(request, f"{server.name}: {message}", "success" if ok else "danger")
    return back(request, CONFIG)


@router.post("/mt5-servers/{server_id}/sync")
def server_sync(request: Request, server_id: int, what: str = "accounts", me: Staff = Depends(staff_perm(PERM)),
                db: Session = Depends(get_db)):
    server = get_or_404(db, MT5Server, server_id, "MT5 server")
    fn = mt5_sync.sync_trading_accounts if what == "accounts" else mt5_sync.sync_account_groups
    try:
        result = fn(db, server, me)
    except ServiceError as exc:
        db.rollback()
        result = f"failed: {exc.message}"
    text, level = sync_message({server.name: result})
    flash(request, text, level)
    return redirect("/admin/import/" + ("account-groups" if what == "groups" else "trading-accounts"))


@router.post("/mt5-servers/{server_id}/delete")
def server_delete(request: Request, server_id: int, purge: str = Form(""), confirm_name: str = Form(""),
                  me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    server = get_or_404(db, MT5Server, server_id, "MT5 server")
    name = server.name

    def run():
        if purge == "on" and confirm_name.strip() != name:
            raise ServiceError(f"Type the server name ({name}) to confirm deleting its CRM data")
        svc.delete_server(db, me, server, purge=purge == "on")
    return attempt(request, run, f"Server '{name}' removed from the CRM. Nothing was changed on the MT5 server.",
                   CONFIG)


# ---------------------------------------------------------------- restart components
@router.get("/mt5-servers/{server_id}/restart")
def restart_moved(server_id: int, me: Staff = Depends(staff_perm("mt5_servers.restart"))):
    return redirect("/admin/restart")


@router.get("/restart")
def restart_page(request: Request, me: Staff = Depends(staff_perm("mt5_servers.restart")),
                 db: Session = Depends(get_db)):
    from app.mt5 import MT5Error
    from app.mt5.gateway import COMPONENT_TYPES
    from app.services import server_health

    server = _only(db)
    components, error = [], None
    try:
        components = server_health.live_components(server)
    except MT5Error as exc:
        error = f"Could not read the server's components from MT5: {exc}"
    return render(request, "admin/servers/restart.html", s=server, components=components, mt5_warning=error,
                  types=COMPONENT_TYPES, effects=server_health.RESTART_EFFECT)


@router.post("/mt5-servers/{server_id}/restart")
def restart_component(request: Request, server_id: int, component_id: int = Form(...), confirm_name: str = Form(""),
                      me: Staff = Depends(staff_perm("mt5_servers.restart")), db: Session = Depends(get_db)):
    from app.services import server_health

    server = get_or_404(db, MT5Server, server_id, "MT5 server")
    page = "/admin/restart"

    def run():
        main = next((c for c in server_health.live_components(server) if c.id == component_id and c.type == 0), None)
        if main is not None and confirm_name.strip() != main.name:
            raise ServiceError(f"Type the main trade server's name ({main.name}) to confirm restarting it")
        return server_health.restart(db, me, server, component_id)
    return attempt(request, run, "{result} is restarting on MT5. Its connections come back within a minute or so; "
                   "Server health shows when it is up again.", page)


# ---------------------------------------------------------------- symbols & pricing
@router.get("/mt5-servers/{server_id}/pricing")
def pricing_moved(server_id: int, me: Staff = Depends(staff_perm("symbols.manage"))):
    return redirect("/admin/pricing")


@router.get("/pricing")
def pricing_page(request: Request, me: Staff = Depends(staff_perm("symbols.manage")), db: Session = Depends(get_db)):
    from app.services import pricing

    server = _only(db)
    pending, error = None, None
    try:
        pending = pricing.preview(db, server)
    except (ServiceError, Exception) as exc:  # noqa: BLE001 - the page still shows the settings
        error = f"Could not read the groups from MT5 ({getattr(exc, 'message', exc)}): what's pending isn't known."
    return render(request, "admin/servers/pricing.html", s=server, p=pricing.settings(server), pending=pending,
                  mt5_warning=error, website=market.website_form(db), card_symbols=market.CARD_SYMBOLS)


@router.post("/pricing/website")
async def website_prices_save(request: Request, me: Staff = Depends(staff_perm("symbols.manage")),
                              db: Session = Depends(get_db)):
    form = await request.form()
    rows = [dict(symbol=s, name=n, category=c)
            for s, n, c in form_rows(form, "web_symbol", "web_name", "web_category")]
    return attempt(request, lambda: market.save_website_symbols(db, me, rows, client_ip(request)),
                   "Website prices saved: {result} symbol(s), live within 30 seconds.", "/admin/pricing#website-prices")


@router.post("/mt5-servers/{server_id}/pricing")
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
    return attempt(request, run, "{result}", "/admin/pricing")


@router.post("/mt5-servers/{server_id}/pricing/apply")
def pricing_apply(request: Request, server_id: int, me: Staff = Depends(staff_perm("symbols.manage")),
                  db: Session = Depends(get_db)):
    from app.services import pricing, symbols

    server = get_or_404(db, MT5Server, server_id, "MT5 server")

    def run():
        r = symbols.sync_symbols(db, server, me)
        return (f"Symbols synced from MT5: {r['symbols']}, {len(r['created'])} new, {len(r['updated'])} updated. "
                + pricing.describe(pricing.apply(db, me, server)))
    return attempt(request, run, "{result}", "/admin/pricing")
