"""Shared plumbing for the server-rendered UI: templates, sessions, CSRF, flash, guards."""

import base64
import math
import secrets
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app import custom
from app.config import get_settings
from app.services import branding
from app.core.deps import client_ip
from app.core.errors import ServiceError
from app.core.security import ip_allowed, password_fingerprint
from app.db import get_db, utcnow
from app.models import Client, ClientStatus, Staff
from app.mt5 import MT5Error
from app.web import labels

APP_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

STAFF_KEY, CLIENT_KEY = "staff", "client"


# ---------------------------------------------------------------- template helpers
_label = labels.label


def _asset(name: str) -> str:
    """The address of our own static file, changing with the file: after an update a browser loads the new
    version instead of keeping its cached copy."""
    return f"/static/{name}?v={int((APP_DIR / 'static' / name).stat().st_mtime)}"


def _money(v, places: int = 2) -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"{Decimal(str(v)):,.{places}f}"
    except (InvalidOperation, ValueError):
        return str(v)


def _num(v) -> str:
    """A setting's number as typed: no exponent, no trailing zeros (100000, 0.01, -5.5)."""
    if v is None or v == "":
        return ""
    d = Decimal(str(v))
    text = f"{d:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _price(v, digits: int | None = None) -> str:
    """Prices at the instrument's precision; without it, trim trailing zeros (min 2 decimals)."""
    if v is None or v == "":
        return "—"
    d = Decimal(str(v))
    if digits is None:
        exponent = d.normalize().as_tuple().exponent
        digits = max(2, -exponent if isinstance(exponent, int) else 2)
    return f"{d:,.{digits}f}"


def _dt(v) -> str:
    return v.strftime("%Y-%m-%d %H:%M") if isinstance(v, datetime) else ("—" if v is None else str(v))


BADGES = {
    "active": "success", "approved": "success", "done": "success", "paid": "success",
    "pending": "warning", "lead": "info", "paused": "warning", "not_submitted": "secondary",
    "rejected": "danger", "failed": "danger", "suspended": "danger", "closed": "secondary",
    "disabled": "danger", "archived": "secondary", "stopped": "secondary", "skipped": "secondary",
    "void": "secondary", "ok": "success", "error": "danger", "unknown": "secondary",
}


def _badge(v) -> str:
    value = getattr(v, "value", v)
    return BADGES.get(str(value), "secondary")


def _side(action) -> str:
    return {0: "Buy", 1: "Sell", 2: "Balance", 3: "Credit"}.get(action, f"#{action}")


def _opts(values) -> list[tuple[str, str]]:
    """Enum members / strings -> [(value, human label)] for select boxes."""
    return [(str(getattr(v, "value", v)), _label(v)) for v in values]


templates.env.filters.update(money=_money, num=_num, price=_price, dt=_dt, badge=_badge, label=_label, side=_side,
                             action_label=labels.action_label, entity_label=labels.entity_label,
                             detail_rows=labels.detail_rows,
                             zip=lambda a, b: list(zip(a, b)))
def _health_alerts() -> list[dict]:
    """MT5 components that are disconnected now (Server health), for the banner on every admin page."""
    from app.services.server_health import banner_alerts

    return banner_alerts()


def _live_quotes() -> list[dict]:
    """Current quotes for the price widgets every page can show (the site ticker); updated live in the browser."""
    from app.services import market

    return market.quotes()


# Back-office pages grouped as one menu entry with tabs: key -> (menu text, icon, [(href, tab, permissions)]),
# where a tab shows to staff with any of its permissions.
ADMIN_HUBS = {
    "accounts": ("Trading accounts", "wallet2", [
        ("/admin/accounts/trading-accounts", "Data", ("accounts.view",)),
        ("/admin/accounts/trader-styles", "Trader styles", ("accounts.view",)),
        ("/admin/accounts/inactive-accounts", "Inactive accounts", ("accounts.cleanup",)),
        ("/admin/accounts/compact", "Compact", ("mt5_trades.compact",))]),
    "positions": ("Positions", "activity", [
        ("/admin/trades/positions/data", "Data", ("trading.view",)),
        ("/admin/trades/positions/sync", "Sync", ("trading.view",)),
        ("/admin/trades/positions/history", "History", ("trading.view",))]),
    "orders": ("Orders", "card-checklist", [
        ("/admin/trades/orders/data", "Data", ("trading.view",)),
        ("/admin/trades/orders/sync", "Sync", ("trading.view",)),
        ("/admin/trades/orders/history", "History", ("trading.view",)),
        ("/admin/trades/orders/controls", "Controls", ("mt5_trades.delete",))]),
    "deals": ("Deals", "list-columns", [
        ("/admin/trades/deals/data", "Data", ("trading.view",)),
        ("/admin/trades/deals/sync", "Sync", ("trading.view",)),
        ("/admin/trades/deals/history", "History", ("trading.view",)),
        ("/admin/trades/deals/controls", "Controls", ("mt5_trades.delete",))]),
    "symbols": ("Symbols", "currency-exchange", [
        ("/admin/symbols/data", "Data", ("symbols.manage",)),
        ("/admin/symbols/sync", "Sync", ("symbols.manage",)),
        ("/admin/symbols/history", "History", ("symbols.manage",))]),
    "setup": ("Trading setup", "diagram-3", [
        ("/admin/trading-setup/overview", "Overview", ("symbols.manage", "liquidity.manage", "routing.manage")),
        ("/admin/trading-setup/liquidity", "Liquidity providers", ("liquidity.manage",)),
        ("/admin/trading-setup/routing", "Routing", ("routing.manage",))]),
    "import": ("Imports", "cloud-download", [
        ("/admin/import/account-groups", "Account groups", ("mt5_servers.manage",)),
        ("/admin/import/trading-accounts", "Trading accounts", ("mt5_servers.manage",)),
        ("/admin/import/pricing", "Symbols & pricing", ("symbols.manage",)),
        ("/admin/import/positions", "Positions", ("trading.view",)),
        ("/admin/import/orders", "Orders", ("trading.view",)),
        ("/admin/import/deals", "Deals", ("trading.view",)),
        ("/admin/import/history", "History", ("mt5_servers.manage", "mt5_trades.delete", "account_groups.manage",
                                               "accounts.manage",
                                              "accounts.cleanup", "trading.sync", "symbols.manage", "liquidity.manage",
                                              "routing.manage"))]),
    "tools": ("Server tools", "tools", [
        ("/admin/server-tools/health", "Server health", ("mt5_health.view",)),
        ("/admin/server-tools/restart", "Restart components", ("mt5_servers.restart",))]),
    "staff": ("Staff", "person-badge", [
        ("/admin/staff/members", "Staff", ("staff.view",)),
        ("/admin/staff/roles", "Roles", ("staff.view",)),
        ("/admin/staff/departments", "Departments", ("staff.view",))]),
    "security": ("Security", "shield-lock", [
        ("/admin/security/audit", "Audit log", ("security.view",)),
        ("/admin/security/login-attempts", "Login attempts", ("security.view",)),
        ("/admin/security/ip-rules", "IP access rules", ("security.ip_rules",))]),
}


def _hub_items(key: str, staff) -> list[tuple[str, str]]:
    """The tabs of a hub this staff member may open: [(href, tab)]."""
    return [(href, tab) for href, tab, perms in ADMIN_HUBS[key][2] if any(staff.has_perm(p) for p in perms)]


def _hub_of(path: str) -> str | None:
    """The hub a back-office page belongs to (its tabs are shown on top of it), or None."""
    for key, (_, _, items) in ADMIN_HUBS.items():
        if any(path == href or path.startswith(href + "/") for href, _, _ in items):
            return key
    return None


templates.env.globals.update(asset=_asset, settings=get_settings, brand=branding.current, live_quotes=_live_quotes, opts=_opts,
                             health_alerts=_health_alerts, admin_hubs=ADMIN_HUBS, hub_items=_hub_items, hub_of=_hub_of,
                             custom_menu=custom.menu, custom_problems=custom.problems)


# ---------------------------------------------------------------- session primitives
def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = request.session["csrf"] = secrets.token_urlsafe(32)
    return token


def flash(request: Request, message: str, level: str = "success") -> None:
    request.session.setdefault("flash", []).append([level, message])


def pop_flashes(request: Request) -> list:
    return request.session.pop("flash", [])


def _fingerprint(password_hash: str | None) -> str:
    return password_fingerprint(password_hash, "web-session")


def _idle_limit_seconds(key: str) -> int:
    s = get_settings()
    return 60 * (s.staff_session_idle_minutes if key == STAFF_KEY else s.client_session_idle_minutes)


def login_session(request: Request, key: str, principal) -> None:
    """The signed cookie holds the principal's id, password fingerprint and token_version (checked on every
    request against the database, so a copied cookie dies with logout or a password change) and the time of
    the last request (idle timeout)."""
    _clear_session(request, key)
    now = utcnow().timestamp()
    request.session[f"{key}_id"] = principal.id
    request.session[f"{key}_fp"] = _fingerprint(principal.password_hash)
    request.session[f"{key}_v"] = principal.token_version or 0
    request.session[f"{key}_at"] = now
    request.session[f"{key}_seen"] = int(now)
    request.session["csrf"] = secrets.token_urlsafe(32)  # rotate on privilege change


def _clear_session(request: Request, key: str) -> None:
    for k in list(request.session.keys()):
        if k.startswith(key + "_"):
            del request.session[k]


def logout_session(request: Request, key: str) -> None:
    """Sign out. Also bumps the principal's token_version, which ends every other web session and API token
    of this user too (a stolen cookie can't outlive the logout; the price is signing out other devices)."""
    pid, version = request.session.get(f"{key}_id"), request.session.get(f"{key}_v")
    _clear_session(request, key)
    if not pid:
        return
    from app.db import SessionLocal
    from app.services.auth import bump_token_version

    with SessionLocal() as db:
        principal = db.get(Staff if key == STAFF_KEY else Client, pid)
        if principal is not None and version == (principal.token_version or 0):  # only a still-valid session
            bump_token_version(principal)
            db.commit()


def refresh_fingerprint(request: Request, key: str, principal) -> None:
    """Keep the current session valid after the user changes their own password or turns 2FA off."""
    request.session[f"{key}_fp"] = _fingerprint(principal.password_hash)
    request.session[f"{key}_v"] = principal.token_version or 0


# ---------------------------------------------------------------- guards
class WebRedirect(Exception):
    def __init__(self, url: str):
        self.url = url


class WebForbidden(Exception):
    def __init__(self, message: str = "You do not have permission to view this page."):
        self.message = message


async def csrf_protect(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    sent = request.headers.get("X-CSRF-Token")
    if not sent:
        form = await request.form()
        sent = form.get("_csrf")
    expected = request.session.get("csrf")
    if not expected or not sent or not secrets.compare_digest(str(sent), expected):
        raise WebForbidden("Your session expired or the form was tampered with. Please reload the page and try again.")


def _session_principal(request: Request, db: Session, key: str, model):
    pid = request.session.get(f"{key}_id")
    if not pid:
        return None
    now = utcnow().timestamp()
    age = now - request.session.get(f"{key}_at", 0)
    idle = now - request.session.get(f"{key}_seen", 0)
    principal = db.get(model, pid)
    if (principal is None or age > get_settings().session_hours * 3600 or idle > _idle_limit_seconds(key)
            or request.session.get(f"{key}_v") != (principal.token_version or 0)
            or request.session.get(f"{key}_fp") != _fingerprint(principal.password_hash)):
        _clear_session(request, key)
        return None
    request.session[f"{key}_seen"] = int(now)
    return principal


def _login_url(request: Request, base: str) -> str:
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return f"{base}?next={quote(target)}"


def web_staff(request: Request, db: Session = Depends(get_db)) -> Staff:
    staff = _session_principal(request, db, STAFF_KEY, Staff)
    if staff is None or not staff.is_active:
        _clear_session(request, STAFF_KEY)
        raise WebRedirect(_login_url(request, "/admin/login"))
    if not ip_allowed(client_ip(request), staff.allowed_ips):
        raise WebForbidden("Access is not allowed from your IP address.")
    if (get_settings().staff_2fa_required and not staff.totp_enabled
            and not request.url.path.startswith(("/admin/me/security", "/admin/me/2fa", "/admin/logout"))):
        flash(request, "Two-factor authentication is required. Please set it up to continue.", "warning")
        raise WebRedirect("/admin/me/security")
    request.state.staff = staff
    return staff


def staff_perm(*perms: str):
    def checker(staff: Staff = Depends(web_staff)) -> Staff:
        if not all(staff.has_perm(p) for p in perms):
            raise WebForbidden()
        return staff

    return checker


def web_client(request: Request, db: Session = Depends(get_db)) -> Client:
    client = _session_principal(request, db, CLIENT_KEY, Client)
    if client is None or client.status in (ClientStatus.suspended, ClientStatus.closed):
        _clear_session(request, CLIENT_KEY)
        raise WebRedirect(_login_url(request, "/portal/login"))
    request.state.client = client
    return client


# ---------------------------------------------------------------- responses
def render(request: Request, template: str, status_code: int = 200, **ctx):
    ctx.setdefault("staff", getattr(request.state, "staff", None))
    ctx.setdefault("client", getattr(request.state, "client", None))
    return templates.TemplateResponse(request, template, {
        "csrf": csrf_token(request), "flashes": pop_flashes(request), "path": request.url.path, **ctx,
    }, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def safe_next(url: str | None, default: str) -> str:
    """Only allow same-site relative redirects (prevents open redirects)."""
    if not url:
        return default
    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc or not url.startswith("/") or url.startswith("//"):
        return default
    return url


def sent_from(request: Request, default: str) -> str:
    """The page a form was sent from, for pages that open at two addresses."""
    return safe_next(urlparse(request.headers.get("referer", "")).path, default)


def back(request: Request, default: str) -> RedirectResponse:
    ref = request.headers.get("referer")
    if ref:
        parsed = urlparse(ref)
        if parsed.netloc == request.url.netloc:
            return redirect(parsed.path + (f"?{parsed.query}" if parsed.query else ""))
    return redirect(default)


def attempt(request: Request, fn, success: str | None, redirect_to: str):
    """Run a state-changing action; flash the outcome and redirect (POST-redirect-GET)."""
    try:
        result = fn()
    except (ServiceError, MT5Error) as exc:
        flash(request, getattr(exc, "message", None) or str(exc), "danger")
        return back(request, redirect_to)
    if success:
        flash(request, success.format(result=result) if "{result" in success else success)
    return redirect(redirect_to)


# ---------------------------------------------------------------- forms & paging
def opt_int(v) -> int | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        return int(str(v).strip())
    except ValueError:
        raise ServiceError(f"'{v}' is not a whole number")


def opt_dec(v) -> Decimal | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        d = Decimal(str(v).strip().replace(",", ""))
    except InvalidOperation:
        raise ServiceError(f"'{v}' is not a valid number")
    if not d.is_finite():
        raise ServiceError(f"'{v}' is not a valid number")
    return d


def req_dec(v, name: str = "Amount") -> Decimal:
    d = opt_dec(v)
    if d is None:
        raise ServiceError(f"{name} is required")
    return d


def opt_enum(enum_cls, value, what: str):
    """The form value as enum_cls, or ServiceError("Invalid <what>") (blank included)."""
    try:
        return enum_cls(value)
    except ValueError:
        raise ServiceError(f"Invalid {what}")


def form_rows(form, *names: str, strip: bool = True) -> list[tuple[str, ...]]:
    """A form's repeated fields as rows: form_rows(form, "a", "b") -> [(a1, b1), (a2, b2), ...]."""
    columns = ([str(v).strip() if strip else str(v) for v in form.getlist(n)] for n in names)
    return list(zip(*columns))


class AtLeast(int):
    """A count that was capped: behaves as the number, shows as "10,000+"."""

    def __str__(self) -> str:
        return f"{int(self):,}+"


COUNT_CAP = 10000  # lists count at most this many rows, so a huge table is not counted on every page


class Page:
    def __init__(self, db: Session, query: Select, page: int, per_page: int = 50):
        counted = db.scalar(select(func.count()).select_from(
            query.order_by(None).limit(COUNT_CAP + 1).subquery())) or 0
        self.capped = counted > COUNT_CAP
        self.total = AtLeast(COUNT_CAP) if self.capped else counted
        self.per_page = per_page
        pages = max(1, math.ceil(min(counted, COUNT_CAP) / per_page))
        self.page = max(1, page) if self.capped else min(max(1, page), pages)
        self.items = db.scalars(query.limit(per_page).offset((self.page - 1) * per_page)).all()
        if self.capped:  # pages beyond the cap stay reachable with Next while they are full
            self.pages = AtLeast(max(pages, self.page + 1 if len(self.items) == per_page else self.page))
        else:
            self.pages = pages


def sync_message(results: dict) -> tuple[str, str]:
    """Flash text + level for mt5_sync results ({server name: summary dict | 'failed: ...'})."""
    parts, level = [], "success"
    for name, r in results.items():
        if isinstance(r, str):
            parts.append(f"{name}: {r}")
            level = "danger"
            continue
        if "seen" in r:  # trading accounts
            t = r["account_groups"]
            text = (f"{name}: {r['seen']} MT5 accounts, {r['created']} imported, {r['updated']} updated, "
                    f"{r['clients_created']} new clients, {r['clients_linked']} linked to existing clients")
            if r["missing"]:
                text += f", {r['missing']} no longer on MT5"
            if t["created"]:
                text += f"; {len(t['created'])} new account groups"
            if r["conflicts"] or r["failed_groups"]:
                text += f"; {len(r['conflicts'])} conflicts, {len(r['failed_groups'])} groups failed"
                level = "warning"
        else:  # account groups
            text = f"{name}: {r['groups']} MT5 groups, {len(r['created'])} new account groups"
            if r["missing"]:
                text += f", {len(r['missing'])} groups no longer on MT5"
        parts.append(text)
    if not parts:
        return "No enabled MT5 server to sync.", "warning"
    return " · ".join(parts), level


def qr_data_uri(text: str) -> str:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    return "data:image/svg+xml;base64," + base64.b64encode(img.to_string()).decode()
