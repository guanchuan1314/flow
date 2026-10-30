"""MT5 > Symbols and MT5 > Liquidity providers: both kept in step with the MT5 server."""

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.errors import ServiceError, get_or_404
from app.db import get_db
from app.models import LiquidityConnection, Staff, Symbol
from app.services import liquidity, symbols as svc
from app.services.servers import require_server, the_server
from app.web.common import Page, attempt, opt_dec, opt_int, render, staff_perm

router = APIRouter()


def _sync_text(name: str, r: dict, what: str) -> str:
    text = f"{name}: {r.get(what, 0)} {what} on MT5, {len(r['created'])} new, {len(r['updated'])} updated"
    return text + (f", {len(r['removed'])} removed (no longer on MT5)" if r["removed"] else "")


# ---------------------------------------------------------------- symbols
class SymbolForm:
    """The symbol form's settings. Blank = unchanged (edit) or the copied symbol's value (new)."""

    INTS = ("digits", "calc_mode", "trade_mode", "exec_mode", "spread", "stops_level", "freeze_level", "swap_mode",
            "swap_3day")
    DECIMALS = ("contract_size", "volume_min", "volume_max", "volume_step", "swap_long", "swap_short",
                "margin_initial", "margin_maintenance", "margin_hedged")
    TEXTS = ("path", "description", "source", "currency_base", "currency_profit", "currency_margin")

    def __init__(self, path: str = Form(""), description: str = Form(""), source: str = Form(""),
                 currency_base: str = Form(""), currency_profit: str = Form(""), currency_margin: str = Form(""),
                 digits: str = Form(""), contract_size: str = Form(""), calc_mode: str = Form(""),
                 trade_mode: str = Form(""), exec_mode: str = Form(""), volume_min: str = Form(""),
                 volume_max: str = Form(""), volume_step: str = Form(""), spread: str = Form(""),
                 stops_level: str = Form(""), freeze_level: str = Form(""), swap_mode: str = Form(""),
                 swap_long: str = Form(""), swap_short: str = Form(""), swap_3day: str = Form(""),
                 margin_initial: str = Form(""), margin_maintenance: str = Form(""), margin_hedged: str = Form(""),
                 quote: list[str] = Form([]), trade: list[str] = Form([])):
        self.raw = {k: v for k, v in locals().items() if k not in ("self", "quote", "trade")}
        self.week = {"quote": quote, "trade": trade}

    def changes(self, blank_text_clears: bool = False) -> dict:
        out = {}
        if all(len(days) == 7 for days in self.week.values()):  # the edit form's sessions table
            out["sessions"] = {kind: svc.parse_sessions(days) for kind, days in self.week.items()}
        for k, v in self.raw.items():
            v = v.strip()
            if k in self.TEXTS:
                out[k] = v.upper() if k.startswith("currency") and v else (v if v or blank_text_clears else None)
            elif k in self.INTS:
                out[k] = opt_int(v)
            else:
                out[k] = opt_dec(v)
        return out


def _choices() -> dict:
    return dict(trade_modes=svc.TRADE_MODES, exec_modes=svc.EXEC_MODES, calc_modes=svc.CALC_MODES,
                swap_modes=svc.SWAP_MODES, weekdays=svc.WEEKDAYS, session_kinds=svc.SESSION_KINDS,
                format_sessions=svc.format_sessions)


@router.get("/symbols")
def symbols_page(request: Request, q: str = "", page: int = 1,
                 me: Staff = Depends(staff_perm("symbols.manage")), db: Session = Depends(get_db)):
    srv = the_server(db)
    query = select(Symbol).where(Symbol.server_id == (srv.id if srv else 0))
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(Symbol.name.ilike(like), Symbol.path.ilike(like), Symbol.description.ilike(like)))
    return render(request, "admin/symbols/list.html", page=Page(db, query.order_by(Symbol.path, Symbol.name), page, 100),
                  server=srv, q=q, **_choices())


@router.post("/symbols/sync")
def symbols_sync(request: Request, server_id: str = Form(""), me: Staff = Depends(staff_perm("symbols.manage")),
                 db: Session = Depends(get_db)):
    def run():
        srv = require_server(db)
        return _sync_text(srv.name, svc.sync_symbols(db, srv, me), "symbols")
    return attempt(request, run, "{result}", f"/admin/symbols?server={server_id}")


@router.get("/symbols/new")
def symbol_new(request: Request, me: Staff = Depends(staff_perm("symbols.manage")), db: Session = Depends(get_db)):
    srv = the_server(db)
    templates = db.scalars(select(Symbol).where(Symbol.server_id == (srv.id if srv else 0))
                           .order_by(Symbol.path, Symbol.name)).all()
    return render(request, "admin/symbols/form.html", s=None, server=srv, templates=templates,
                  **_choices())


@router.post("/symbols/new")
def symbol_create(request: Request, server_id: str = Form(...), name: str = Form(...), template: str = Form(""),
                  form: SymbolForm = Depends(), me: Staff = Depends(staff_perm("symbols.manage")),
                  db: Session = Depends(get_db)):
    def run():
        if not template:
            raise ServiceError("Choose the existing symbol to copy settings from")
        sym = svc.create_symbol(db, me, server_id=int(server_id), name=name, template=template, changes=form.changes())
        return sym.name
    return attempt(request, run, "Symbol {result} created on MT5.", f"/admin/symbols?server={server_id}")


@router.post("/symbols/bulk")
def symbols_bulk(request: Request, server_id: str = Form(""), ids: list[str] = Form([]), form: SymbolForm = Depends(),
                 me: Staff = Depends(staff_perm("symbols.manage")), db: Session = Depends(get_db)):
    def run():
        chosen = [s for s in (db.get(Symbol, int(i)) for i in ids if i.isdigit())
                  if s is not None and str(s.server_id) == server_id]
        done, failed = svc.bulk_update(db, me, chosen, form.changes())
        if failed and not done:
            raise ServiceError("Nothing saved. " + " ".join(f"{k}: {v}" for k, v in failed.items()))
        text = f"Saved on MT5: {len(done)} symbols ({', '.join(done[:8])}{'…' if len(done) > 8 else ''})."
        return text + (" Not saved: " + "; ".join(f"{k}: {v}" for k, v in failed.items()) if failed else "")
    return attempt(request, run, "{result}", f"/admin/symbols?server={server_id}")


@router.get("/symbols/{symbol_id}")
def symbol_edit(request: Request, symbol_id: int, me: Staff = Depends(staff_perm("symbols.manage")),
                db: Session = Depends(get_db)):
    sym = get_or_404(db, Symbol, symbol_id)
    warning = svc.refresh_symbol(db, sym)  # show MT5's current settings
    return render(request, "admin/symbols/form.html", s=sym, server=sym.server, mt5_warning=warning,
                  usage=svc.usage(db, sym), **_choices())


@router.post("/symbols/{symbol_id}")
def symbol_update(request: Request, symbol_id: int, form: SymbolForm = Depends(),
                  me: Staff = Depends(staff_perm("symbols.manage")), db: Session = Depends(get_db)):
    sym = get_or_404(db, Symbol, symbol_id)
    return attempt(request, lambda: svc.update_symbol(db, me, sym, form.changes(blank_text_clears=True)),
                   f"Symbol {sym.name} saved on MT5.", f"/admin/symbols/{symbol_id}")


@router.post("/symbols/{symbol_id}/delete")
def symbol_delete(request: Request, symbol_id: int, me: Staff = Depends(staff_perm("symbols.manage")),
                  db: Session = Depends(get_db)):
    sym = get_or_404(db, Symbol, symbol_id)
    name, server_id = sym.name, sym.server_id
    return attempt(request, lambda: svc.delete_symbol(db, me, sym), f"Symbol {name} deleted on MT5 and in the CRM.",
                   f"/admin/symbols?server={server_id}")


# ---------------------------------------------------------------- liquidity providers
def _lines(text: str) -> list[str]:
    return [x.strip() for x in text.replace(",", "\n").splitlines() if x.strip()]


def _params(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if line.strip():
            name, sep, value = line.partition("=")
            if not sep or not name.strip():
                raise ServiceError(f"Parameters: write one per line as Name = Value (not \"{line.strip()[:40]}\")")
            out[name.strip()] = value.strip()
    return out


def _translations(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if not 2 <= len(parts) <= 5:
            raise ServiceError("Symbol translations: one per line as LP symbol, MT5 symbol[, digits, bid markup, "
                               "ask markup]")
        try:
            nums = [int(p) if p else 0 for p in parts[2:]] + [0] * (5 - len(parts))
        except ValueError:
            raise ServiceError(f"Symbol translations: digits and markups are whole numbers (\"{line.strip()[:40]}\")")
        out.append({"source": parts[0], "symbol": parts[1], "digits": nums[0], "bid_markup": nums[1],
                    "ask_markup": nums[2]})
    return out


class LinkForm:
    """The gateway / data feed form."""

    def __init__(self, enabled: str = Form(""), lp_server: str = Form(""), lp_login: str = Form(""),
                 password: str = Form(""), gateway_server: str = Form(""), gateway_login: str = Form(""),
                 flag: list[str] = Form([]), other_flags: str = Form("0"), keywords: str = Form(""),
                 categories: str = Form(""), timeout_reconnect: str = Form(""), timeout_sleep: str = Form(""),
                 timeout_attempts: str = Form(""), symbols: str = Form(""), groups: str = Form(""),
                 params: str = Form(""), translations: str = Form("")):
        self.raw = {k: v for k, v in locals().items() if k != "self"}

    @property
    def password(self) -> str | None:
        return self.raw["password"] or None

    def fields(self) -> dict:
        f = self.raw
        flags = (opt_int(f["other_flags"]) or 0) | sum(int(x) for x in f["flag"] if x.isdigit())
        return dict(enabled=f["enabled"] == "on", lp_server=f["lp_server"].strip(), lp_login=f["lp_login"].strip(),
                    gateway_server=f["gateway_server"].strip(), gateway_login=opt_int(f["gateway_login"]) or 0,
                    flags=flags, keywords=f["keywords"].strip(), categories=f["categories"].strip(),
                    timeout_reconnect=opt_int(f["timeout_reconnect"]), timeout_sleep=opt_int(f["timeout_sleep"]),
                    timeout_attempts=opt_int(f["timeout_attempts"]), symbols=_lines(f["symbols"]),
                    groups=_lines(f["groups"]), params=_params(f["params"]),
                    translations=_translations(f["translations"]))


def _kind(kind: str) -> str:
    return kind if kind in liquidity.KINDS else "gateway"


def _flags(kind: str, value: int) -> dict:
    """The flag checkboxes the form offers; the other bits ride along in a hidden field, unchanged."""
    offered = liquidity.FLAGS[kind]
    return dict(flags=offered, flags_on=[bit for bit in offered if value & bit], other_flags=value & ~sum(offered))


@router.get("/liquidity")
def liquidity_page(request: Request, kind: str = "gateway", me: Staff = Depends(staff_perm("liquidity.manage")),
                   db: Session = Depends(get_db)):
    srv, kind = the_server(db), _kind(kind)
    rows = db.scalars(select(LiquidityConnection).where(LiquidityConnection.server_id == (srv.id if srv else 0),
                                                        LiquidityConnection.kind == kind)
                      .order_by(LiquidityConnection.name)).all()
    counts = {k: len(db.scalars(select(LiquidityConnection.id).where(
        LiquidityConnection.server_id == (srv.id if srv else 0), LiquidityConnection.kind == k)).all())
        for k in liquidity.KINDS}
    return render(request, "admin/liquidity/list.html", rows=rows, server=srv, kind=kind,
                  kinds=liquidity.KINDS, counts=counts, routes={c.id: liquidity.routes_to(db, c) for c in rows})


@router.post("/liquidity/sync")
def liquidity_sync(request: Request, server_id: str = Form(""), kind: str = Form("gateway"),
                   me: Staff = Depends(staff_perm("liquidity.manage")), db: Session = Depends(get_db)):
    def run():
        srv = require_server(db)
        r = liquidity.sync_liquidity(db, srv, me)
        text = f"{srv.name}: {r['gateways']} gateways and {r['feeds']} data feeds on MT5, {len(r['created'])} new, " \
               f"{len(r['updated'])} updated"
        return text + (f", {len(r['removed'])} removed (no longer on MT5)" if r["removed"] else "")
    return attempt(request, run, "{result}", f"/admin/liquidity?server={server_id}&kind={_kind(kind)}")


@router.post("/liquidity/restart")
def liquidity_restart(request: Request, server_id: str = Form(""), kind: str = Form("gateway"),
                      me: Staff = Depends(staff_perm("liquidity.manage")), db: Session = Depends(get_db)):
    kind = _kind(kind)

    def run():
        srv = require_server(db)
        liquidity.restart_all(db, me, srv, kind)
        return (f"{srv.name}: every {liquidity.KINDS[kind].lower()} is restarting. They reconnect to their LPs "
                "within a minute; Sync from MT5 shows their status.")
    return attempt(request, run, "{result}", f"/admin/liquidity?server={server_id}&kind={kind}")


@router.get("/liquidity/new")
def link_new(request: Request, kind: str = "gateway", module: str = "",
             me: Staff = Depends(staff_perm("liquidity.manage")), db: Session = Depends(get_db)):
    srv, kind = the_server(db), _kind(kind)
    mods, error = [], None
    if srv is not None:
        try:
            mods = liquidity.modules(srv.id, kind)
        except ServiceError as exc:
            error = exc.message
    chosen = next((m for m in mods if m.module == module), None)
    return render(request, "admin/liquidity/form.html", c=None, server=srv, kind=kind,
                  kinds=liquidity.KINDS, modules=mods, grouped=liquidity.grouped_modules(mods), module=chosen,
                  mt5_warning=error, secrets=liquidity.secret_params(None, chosen),
                  **_flags(kind, 1 if kind == "feed" else 0))


@router.post("/liquidity/new")
def link_create(request: Request, server_id: str = Form(...), kind: str = Form(...), name: str = Form(...),
                module: str = Form(...), map_symbols: str = Form(""), route_groups: str = Form(""),
                route_symbols: str = Form(""), form: LinkForm = Depends(),
                me: Staff = Depends(staff_perm("liquidity.manage")), db: Session = Depends(get_db)):
    kind = _kind(kind)

    def run():
        if route_groups.strip() and not me.has_perm("routing.manage"):
            raise ServiceError("Adding a routing rule needs the routing permission; leave the groups blank.")
        return liquidity.create_connection(db, me, server_id=int(server_id), kind=kind, name=name, module=module,
                                           fields=form.fields(), password=form.password,
                                           map_symbols=map_symbols == "on", route_groups=_lines(route_groups),
                                           route_symbols=_lines(route_symbols)).name
    return attempt(request, run, f"{liquidity.KINDS[kind]} {{result}} created on MT5.",
                   f"/admin/liquidity?server={server_id}&kind={kind}")


@router.get("/liquidity/{link_id}")
def link_edit(request: Request, link_id: int, me: Staff = Depends(staff_perm("liquidity.manage")),
              db: Session = Depends(get_db)):
    c = get_or_404(db, LiquidityConnection, link_id)
    warning = liquidity.refresh_connection(db, c)  # show MT5's current settings and state
    return render(request, "admin/liquidity/form.html", c=c, server=c.server, kind=c.kind, kinds=liquidity.KINDS,
                  mt5_warning=warning, secrets=liquidity.secret_params(c), routes=liquidity.routes_to(db, c),
                  **_flags(c.kind, c.flags))


@router.post("/liquidity/{link_id}")
def link_update(request: Request, link_id: int, form: LinkForm = Depends(),
                me: Staff = Depends(staff_perm("liquidity.manage")), db: Session = Depends(get_db)):
    c = get_or_404(db, LiquidityConnection, link_id)
    return attempt(request, lambda: liquidity.update_connection(db, me, c, form.fields(), password=form.password),
                   f"{liquidity.KINDS[c.kind]} {c.name} saved on MT5.", f"/admin/liquidity/{link_id}")


@router.post("/liquidity/{link_id}/delete")
def link_delete(request: Request, link_id: int, me: Staff = Depends(staff_perm("liquidity.manage")),
                db: Session = Depends(get_db)):
    c = get_or_404(db, LiquidityConnection, link_id)
    label, server_id, kind = f"{liquidity.KINDS[c.kind]} {c.name}", c.server_id, c.kind
    return attempt(request, lambda: liquidity.delete_connection(db, me, c), f"{label} deleted on MT5 and in the CRM.",
                   f"/admin/liquidity?server={server_id}&kind={kind}")

