"""MT5 > Server health, MT5 > Trader styles and MT5 > Demo clean-up: keeping one MT5 server fast for everyone."""

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ServiceError, get_or_404
from app.db import get_db
from app.config import get_settings
from app.models import AccountGroup, MT5Server, Staff
from app.mt5 import routed
from app.services import demo_cleanup, server_health, trader_styles
from app.services.servers import require_server, the_server
from app.web import syncs
from app.web.common import attempt, flash, form_rows, opt_int, redirect, render, staff_perm

router = APIRouter()
SETUP = "/admin/accounts/trader-styles/setup"


# ---------------------------------------------------------------- server health
@router.get("/server-tools/health")
def health_page(request: Request, server: str = "all", only: str = "", me: Staff = Depends(staff_perm("mt5_health.view")),
                db: Session = Depends(get_db)):
    """One server, or every enabled server (server=all, the default) on one page.
    only=warnings: just the servers that need a look, with just their components that do."""
    if server == "all":
        reports = server_health.report_all(db)
    else:
        srv = the_server(db)
        reports = [server_health.report(db, srv)] if srv else []
    shown = reports
    if only == "warnings":
        shown = [{**r, "components": [c for c in r["components"] if c["level"] != "ok"]}
                 for r in reports if r["level"] in ("warning", "danger")]
    return render(request, "admin/server_health.html", chosen=server, only=only,
                  summary=reports, reports=shown)


@router.post("/server-tools/health/refresh")
def health_refresh(request: Request, server_id: str = Form("all"), me: Staff = Depends(staff_perm("mt5_health.view")),
                   db: Session = Depends(get_db)):
    def run():
        srv = require_server(db)
        error = server_health.collect(db, srv).error
        if error:
            raise ServiceError(f"Could not read from MT5: {srv.name}: {error}")
        return "Read from MT5 just now."
    return attempt(request, run, "{result}", f"/admin/server-tools/health?server={server_id or 'all'}")


# ---------------------------------------------------------------- trader styles
def _live_groups(db: Session, server_id: int | None) -> list[AccountGroup]:
    q = select(AccountGroup).where(AccountGroup.is_demo.is_(False))
    if server_id is not None:
        q = q.where(AccountGroup.server_id == server_id)
    return list(db.scalars(q.order_by(AccountGroup.server_id, AccountGroup.name)))


@router.get("/accounts/trader-styles")
def styles_page(request: Request, server: str = "", days: int = 30, min_deals: int = 10, style: str = "",
                demo: str = "", me: Staff = Depends(staff_perm("accounts.view")), db: Session = Depends(get_db)):
    every = server == "all"
    srv = None if every else the_server(db)
    days, min_deals = max(1, min(days, 365)), max(1, min_deals)
    rows = trader_styles.profiles(db, srv, days, min_deals, demo=bool(demo), staff=me) if (srv or every) else []
    names = trader_styles.names(srv)
    totals = {k: sum(1 for r in rows if r["style"] == k) for k in names}
    tag_names = {r["key"]: r["name"] for r in trader_styles.rules(srv) if r["kind"] == "tag"}
    shown = [r for r in rows if not style or r["style"] == style or style in r["tags"]
             or (style == "suggested" and r["suggest"])]
    return render(request, "admin/trader_styles.html", server=srv, rows=shown[:1000],
                  totals=totals, suggested=sum(1 for r in rows if r["suggest"]), total=len(rows), days=days,
                  min_deals=min_deals, style=style, demo=demo, styles=names, tag_names=tag_names,
                  colors=trader_styles.colors(srv), every=every,
                  groups=_live_groups(db, None if every else srv.id) if (srv or every) else [],
                  style_groups=trader_styles.style_groups(db, srv) if srv else {})


@router.get("/accounts/trader-styles/setup")
def styles_setup(request: Request, login: str = "", days: int = 30, me: Staff = Depends(staff_perm("accounts.manage")),
                 db: Session = Depends(get_db)):
    """The styles and tags to detect, with their conditions; `login` tries them on one account."""
    srv = require_server(db)
    days = max(1, min(days, 365))
    rules = trader_styles.rules(srv)
    tried, tried_error = None, None
    if login.strip():
        n = opt_int(login)
        m = trader_styles.metrics(db, srv, days, logins=[n]).get(n)
        if m is None:
            tried_error = f"Account {n} has no buy or sell deal in the CRM in the last {days} days."
        else:
            style, tags = trader_styles.classify(m, rules, 1)
            tried = {"login": n, "metrics": m, "style": style, "tags": tags,
                     "checks": trader_styles.explain(m, [r for r in rules if r.get("enabled", True)])}
    groups = _live_groups(db, srv.id)
    return render(request, "admin/trader_styles_setup.html", server=srv, rules=rules,
                  never_reached=trader_styles.never_reached(rules), metrics=trader_styles.METRICS,
                  ops=trader_styles.OPS, group_names={g.id: g.name for g in groups}, tried=tried,
                  tried_error=tried_error, login=login, days=days)


@router.post("/accounts/trader-styles/setup/reset")
def styles_setup_reset(request: Request, me: Staff = Depends(staff_perm("accounts.manage")),
                       db: Session = Depends(get_db)):
    def run():
        trader_styles.save_rules(db, me, require_server(db), None)
    return attempt(request, run, "Back to the default styles and tags.", SETUP)


def _rule_page(request: Request, db: Session, srv: MT5Server, key: str, rule: dict):
    return render(request, "admin/trader_style_form.html", server=srv, key=key, rule=rule, groups=_live_groups(db, srv.id),
                  metrics=trader_styles.METRICS, ops=trader_styles.OPS, kinds=trader_styles.KINDS,
                  colors=trader_styles.COLORS)


@router.get("/accounts/trader-styles/setup/{key}")
@router.get("/accounts/trader-styles/setup/new")
def styles_rule(request: Request, key: str = "", kind: str = "style", me: Staff = Depends(staff_perm("accounts.manage")),
                db: Session = Depends(get_db)):
    """One style or tag with its conditions."""
    srv = require_server(db)
    new = {"name": "", "kind": kind, "color": "primary", "group_id": None, "enabled": True, "conditions": []}
    rule = next((r for r in trader_styles.rules(srv) if r["key"] == key), None) if key else new
    if rule is None:
        flash(request, "That style no longer exists.", "warning")
        return redirect(SETUP)
    return _rule_page(request, db, srv, key, rule)


@router.post("/accounts/trader-styles/setup/{key}")
@router.post("/accounts/trader-styles/setup/new")
async def styles_rule_save(request: Request, key: str = "", me: Staff = Depends(staff_perm("accounts.manage")),
                           db: Session = Depends(get_db)):
    form = await request.form()
    srv = require_server(db)
    draft = {"name": str(form.get("name", "")), "kind": form.get("kind", ""), "color": form.get("color", ""),
             "group_id": form.get("group_id", ""), "enabled": form.get("enabled") == "on",
             "conditions": [dict(zip(("metric", "op", "value", "value2"), row)) for row in
                            form_rows(form, "cond_metric", "cond_op", "cond_value", "cond_value2")]}
    try:
        rule = trader_styles.save_rule(db, me, srv, key or None, draft)
    except ServiceError as exc:  # the page again, with what was typed
        flash(request, exc.message, "danger")
        return _rule_page(request, db, srv, key, draft)
    flash(request, f"Saved {rule['name']}.")
    return redirect(SETUP)


@router.post("/accounts/trader-styles/setup/{key}/delete")
def styles_rule_delete(request: Request, key: str, me: Staff = Depends(staff_perm("accounts.manage")),
                       db: Session = Depends(get_db)):
    def run():
        trader_styles.delete_rule(db, me, require_server(db), key)
    return attempt(request, run, "Deleted.", SETUP)


@router.post("/accounts/trader-styles/setup/{key}/move")
def styles_rule_move(request: Request, key: str, step: int = Form(...), me: Staff = Depends(staff_perm("accounts.manage")),
                     db: Session = Depends(get_db)):
    def run():
        trader_styles.move_rule(db, me, require_server(db), key, 1 if step > 0 else -1)
    return attempt(request, run, None, SETUP)


@router.post("/accounts/trader-styles/groups")
def styles_groups(request: Request, ea: str = Form(""), manual: str = Form(""),
                  me: Staff = Depends(staff_perm("accounts.manage")), db: Session = Depends(get_db)):
    def run():
        trader_styles.save_style_groups(db, me, require_server(db), {"ea": opt_int(ea), "manual": opt_int(manual)})
    return attempt(request, run, "Saved where each style belongs.", "/admin/accounts/trader-styles")


@router.post("/accounts/trader-styles/move")
def styles_move(request: Request, server_id: str = Form(""), target: str = Form(""), login: list[str] = Form([]),
                back_to: str = Form(""), me: Staff = Depends(staff_perm("accounts.manage")),
                db: Session = Depends(get_db)):
    url = back_to if back_to.startswith("/admin/accounts/trader-styles") else "/admin/accounts/trader-styles"
    logins = [int(x) for x in login if x.strip().isdigit()]
    if not logins:
        flash(request, "Tick the accounts to move first.", "warning")
        return redirect(url)
    try:
        done, failed = trader_styles.move(db, routed(), me, logins, opt_int(target) or 0)
    except ServiceError as exc:
        flash(request, exc.message, "danger")
        return redirect(url)
    if done:
        flash(request, f"Moved {len(done)} account(s) on MT5.")
    if failed:
        flash(request, f"Not moved: {'; '.join(failed[:10])}" + (f" and {len(failed) - 10} more" if len(failed) > 10 else ""),
              "danger")
    return redirect(url)


# ---------------------------------------------------------------- inactive demo accounts (from MT5 servers)
INACTIVE = "/admin/accounts/inactive-accounts"


@router.get("/accounts/inactive-accounts")
def cleanup_page(request: Request, me: Staff = Depends(staff_perm("accounts.cleanup")), db: Session = Depends(get_db)):
    srv = get_or_404(db, MT5Server, getattr(the_server(db), "id", 0))
    mt5_groups, mt5_error = [], None
    try:
        mt5_groups = demo_cleanup.mt5_demo_groups(db, srv)
    except ServiceError as exc:
        mt5_error = exc.message
    return render(request, "admin/demo_cleanup.html", server=srv, cfg=demo_cleanup.settings_of(srv),
                  found=demo_cleanup.scan(db, srv), mt5_groups=mt5_groups, mt5_error=mt5_error,
                  actions=demo_cleanup.ACTIONS, min_days=demo_cleanup.MIN_DAYS, preview=demo_cleanup.PREVIEW,
                  max_per_run=demo_cleanup.MAX_PER_RUN, max_mt5_days=demo_cleanup.MAX_MT5_DAYS,
                  refresh_minutes=get_settings().activity_refresh_minutes)


@router.post("/accounts/inactive-accounts/{server_id:int}/settings")
def cleanup_settings(request: Request, server_id: int, enabled: str = Form(""), days: str = Form("90"),
                     action: str = Form("archive"), groups: str = Form(""),
                     me: Staff = Depends(staff_perm("accounts.cleanup")), db: Session = Depends(get_db)):
    srv = get_or_404(db, MT5Server, server_id)

    def run():
        cfg = demo_cleanup.save_settings(db, me, srv, enabled=bool(enabled), days=opt_int(days) or 0, action=action,
                                         groups=groups)
        return "Saved. " + (f"The clean-up runs once a day and will {cfg['action']} demo accounts inactive for "
                            f"{cfg['days']} days." if cfg["enabled"] else "The daily clean-up is off.")
    return attempt(request, run, "{result}", INACTIVE)


@router.post("/accounts/inactive-accounts/{server_id:int}/mt5-days")
def cleanup_mt5_days(request: Request, server_id: int, group: list[str] = Form([]), days: str = Form(""),
                     me: Staff = Depends(staff_perm("accounts.cleanup")), db: Session = Depends(get_db)):
    srv = get_or_404(db, MT5Server, server_id)
    url = INACTIVE
    try:
        done, failed = demo_cleanup.set_mt5_inactivity(db, me, srv, [g for g in group if g], opt_int(days) if days.strip()
                                                       else -1)
    except ServiceError as exc:
        flash(request, exc.message, "danger")
        return redirect(url)
    if done:
        flash(request, f"MT5 now removes demo accounts inactive for {days} days in: {', '.join(done)}." if int(days)
              else f"MT5 no longer removes inactive demo accounts in: {', '.join(done)}.")
    if failed:
        flash(request, "Not changed: " + "; ".join(failed[:10]), "danger")
    return redirect(url)


@router.post("/accounts/inactive-accounts/{server_id:int}/run")
def cleanup_run(request: Request, server_id: int, action: str = Form(""), login: list[str] = Form([]),
                everything: str = Form(""), me: Staff = Depends(staff_perm("accounts.cleanup")),
                db: Session = Depends(get_db)):
    srv = get_or_404(db, MT5Server, server_id)
    url = INACTIVE
    logins = None if everything else [int(x) for x in login if x.strip().isdigit()]
    if logins == []:
        flash(request, "Tick the accounts first, or use the button for all of them.", "warning")
        return redirect(url)

    return syncs.start(request, db, me, "demo_cleanup", srv.id, url, {"logins": logins, "action": action or None})
