"""Back-office side of background synchronizations (services.sync_runs): start one from a page, show its
progress on that page (sync_panel in macros.html, kept across refreshes), and say what a run did, there and in
Imports > History."""

from concurrent.futures import TimeoutError as FutureTimeout

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.core.errors import ServiceError
from app.db import SessionLocal
from app.models import MT5Server, Staff, SyncRun
from app.services import demo_cleanup, pricing, sync_runs
from app.web.common import Page, flash, redirect, render, sync_message, templates

# Syncs always run in the background: the page answers at once and shows the progress. Tests set this to wait
# for the result instead (so they can check it on the next page).
WAIT_SECONDS = 0.0
HISTORY = "/admin/import/history"


def _changes(r: dict, count: str) -> str:
    text = f"{count}, {len(r['created'])} new, {len(r['updated'])} updated"
    return text + (f", {len(r['removed'])} removed (no longer on MT5)" if r.get("removed") else "")


def _setup(r: dict) -> str:
    parts, errors = [], []
    for key, label in (("symbols", "symbols"), ("liquidity", None), ("routes", "routing rules")):
        res = r[key]
        if "error" in res:
            errors.append(res["error"])
        elif key == "liquidity":
            parts.append(f"{res['gateways']} gateways, {res['feeds']} data feeds")
        else:
            parts.append(f"{res[key]} {label}")
    return ", ".join(parts) + ("." if parts else "") + (" Not synced: " + " ".join(errors) if errors else "")


def _one(kind: str, r: dict) -> str:
    if kind in ("trades", "catch_up"):
        text = (f"{r['new_deals']} new deals, {r['open_positions']} open positions, {r['open_orders']} open orders, "
                f"{r['accounts']} accounts updated")
        return text + (f"; errors: {'; '.join(r['errors'][:3])}" if r.get("errors") else "")
    if kind == "positions":
        return f"{r['open_positions']} open positions"
    if kind == "orders":
        return f"{r['open_orders']} open orders, {r['new_deals']} new deals"
    if kind == "deals":
        return f"{r['new_deals']} new deals"
    if kind == "trading_setup":
        return _setup(r)
    if kind == "symbols":
        return _changes(r, f"{r.get('symbols', 0)} symbols on MT5")
    if kind == "liquidity":
        return _changes(r, f"{r['gateways']} gateways and {r['feeds']} data feeds on MT5")
    if kind == "routing":
        return _changes(r, f"{r['routes']} routing rules on MT5")
    if kind == "demo_cleanup":
        text = f"{'archived' if r['action'] == 'archive' else 'deleted'} {r['done']} demo account(s) on MT5."
        if r["active_again"]:
            text += f" {r['active_again']} kept: they connected since the CRM last looked."
        if r["failed"]:
            text += f" Not done for {r['failed']}: " + "; ".join(r["errors"][:5])
        if r["remaining"]:
            text += f" {r['remaining']} more qualify: run it again (at most {demo_cleanup.MAX_PER_RUN} per run)."
        return text
    if kind == "trade_cleanup":
        text = f"deleted {r['done']} {r['what']} on MT5 and in the CRM."
        if r["kept"]:
            text += f" {r['kept']} kept: their position is still open, or they are balance or credit deals."
        if r["failed"]:
            text += f" Not done for {r['failed']}: " + "; ".join(r["errors"][:5])
        if r["remaining"]:
            text += f" {r['remaining']} more match: run it again."
        return text
    if kind == "compact":
        text = (f"compacted {r['accounts']} account(s): {r['deals']} deals into {r['lines']} summaries, "
                f"{r['orders']} orders removed.")
        if r["skipped"] or r["failed"]:
            text += f" Needs a look ({r['skipped'] + r['failed']}): " + "; ".join(r["errors"][:5])
        return text
    if kind == "pricing":
        s = r["symbols"]
        return (f"symbols synced from MT5: {s['symbols']}, {len(s['created'])} new, {len(s['updated'])} updated. "
                + pricing.describe(r["pricing"]))
    return ""


def describe(run: SyncRun) -> tuple[str, str]:
    """What the run did, and a flash level (success | warning | danger | info)."""
    if run.status == "queued":
        return "Waiting to start.", "info"
    if run.status == "running":
        return "Running…", "info"
    if run.status == "failed" and run.message:
        return run.message, "danger"
    summary = run.summary or {}
    if run.kind in ("account_groups", "trading_accounts"):
        text, level = sync_message(summary)
        if run.kind == "account_groups" and any(isinstance(r, dict) and r.get("created") for r in summary.values()):
            text += " New groups are disabled until you review and enable them."
        return text, level
    parts, level = [], "success"
    for name, r in summary.items():
        if isinstance(r, str):
            parts.append(f"{name}: {r}")
            level = "danger"
        elif run.kind == "trading_setup":
            parts.append(f"{name} synced from MT5: {_one(run.kind, r)}")
        else:
            parts.append(f"{name}: {_one(run.kind, r)}")
    return (" · ".join(parts) or "Nothing to sync."), ("danger" if run.status == "failed" else level)


def history(db: Session, query, page: int, per_page: int = 50) -> dict:
    """What servers/sync_runs.html shows: a page of these runs."""
    return dict(page=Page(db, query, page, per_page), kinds=sync_runs.KINDS, describe=describe,
                staff_names=dict(db.execute(select(Staff.id, Staff.full_name)).all()),
                server_names=dict(db.execute(select(MT5Server.id, MT5Server.name)).all()))


def history_page(request, db: Session, title: str = "Synchronization history", only: tuple | None = None, kind: str = "",
                 trigger: str = "", status: str = "", partial: int = 0, page: int = 1):
    """A history page with its filters: Imports > History, or a menu's History tab (`only` its kinds)."""
    choices = {k: name for k, name in sync_runs.KINDS.items() if only is None or k in only}
    q = select(SyncRun).where(SyncRun.kind.in_(choices))
    server = request.query_params.get("server", "")
    if server.isdigit():  # a sync over every server belongs to each of them
        q = q.where(or_(SyncRun.server_id == int(server), SyncRun.server_id.is_(None)))
    if kind in choices:
        q = q.where(SyncRun.kind == kind)
    if trigger in ("manual", "schedule"):
        q = q.where(SyncRun.trigger == trigger)
    if status in ("queued", "running", "done", "failed"):
        q = q.where(SyncRun.status.in_(("queued", "running")) if status == "running" else SyncRun.status == status)
    ctx = dict(title=title, everything=only is None, choices=choices, kind=kind, trigger=trigger, status=status, server=server,
               **history(db, q.order_by(SyncRun.id.desc()), page))
    return render(request, "admin/servers/sync_runs.html" if partial else "admin/servers/sync_history.html", **ctx)


def server_id(db: Session, chosen: str | int | None = "") -> int | None:
    """The MT5 server a page's sync button works on: the one chosen on the page, else the default one. None before
    one is set up: the sync then says so."""
    from app.services.servers import pick

    server = pick(db, chosen)
    return server.id if server else None


def start(request, db: Session, me: Staff, kind: str, server_id: int | None, back: str,
          params: dict | None = None) -> RedirectResponse:
    """Start a sync from a page's button and go back to the page, which shows its progress."""
    label = sync_runs.KINDS[kind]
    try:
        run, future = sync_runs.start(db, me, kind, server_id, params)
    except ServiceError as exc:
        flash(request, exc.message, "danger")
        return redirect(back)
    if future is None:
        flash(request, f"{label}: already running. Its progress is shown below.", "info")
        return redirect(back)
    if WAIT_SECONDS:
        try:
            future.result(timeout=WAIT_SECONDS)
        except FutureTimeout:
            pass
        db.refresh(run)
    if run.active:
        flash(request, f"{label}: started in the background. Its progress is shown below, also after a refresh "
                       "or when you come back later.", "info")
    else:
        flash(request, *describe(run))
    return redirect(back)


def panel(kinds: list[str]) -> list[dict]:
    """For sync_panel (macros.html): the runs of these kinds waiting or running, else the last one if recent."""
    with SessionLocal() as db:
        runs = sync_runs.latest(db, kinds)
        names = dict(db.execute(select(Staff.id, Staff.full_name).where(
            Staff.id.in_([r.created_by_id for r in runs if r.created_by_id]))).all()) if runs else {}
        servers = dict(db.execute(select(MT5Server.id, MT5Server.name)).all()) if runs else {}
        out = []
        for r in runs:
            text, level = describe(r)
            out.append({"run": r, "label": sync_runs.KINDS[r.kind], "text": text, "level": level,
                        "by": "the schedule" if r.trigger == "schedule" else names.get(r.created_by_id, "staff"),
                        "server": servers.get(r.server_id, "all servers")})
            db.expunge(r)
        return out


templates.env.globals["sync_panel_runs"] = panel
