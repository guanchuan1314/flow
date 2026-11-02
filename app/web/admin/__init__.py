import re

from fastapi import APIRouter, Depends, HTTPException, Request

from app.web.admin import (
    accounts, auth, automation, branding, clients, copy, dashboard, finance, ip_rules, marketing, methods, operations, servers,
    staff, symbols, trading, trading_setup,
)
from app.web.common import csrf_protect, redirect

router = APIRouter(prefix="/admin", include_in_schema=False, dependencies=[Depends(csrf_protect)])
router.add_api_route("", dashboard.dashboard, methods=["GET"])
for module in (auth, clients, accounts, servers, symbols, trading_setup, operations, finance, methods, trading, copy,
               marketing, staff, ip_rules, automation, branding):
    router.include_router(module.router)

# Pages that moved under their menu (/admin/<menu>/<page>): old links and bookmarks still work.
MOVED = {
    "accounts": "accounts/trading-accounts", "account-groups": "accounts/account-groups",
    "trader-styles": "accounts/trader-styles", "inactive-accounts": "accounts/inactive-accounts",
    "demo-cleanup": "accounts/inactive-accounts",
    "trading-setup": "trading-setup/overview", "symbols": "trading-setup/symbols",
    "liquidity": "trading-setup/liquidity", "routing": "trading-setup/routing",
    "sync": "import/trades",
    "server-health": "server-tools/health", "pricing": "server-tools/pricing", "restart": "server-tools/restart",
    "staff": "staff/members", "roles": "staff/roles", "departments": "staff/departments",
    "audit": "security/audit", "login-attempts": "security/login-attempts", "ip-rules": "security/ip-rules",
    "mt5-servers": "configuration/servers",
}
_SERVER_PAGES = re.compile(r"mt5-servers/\d+/(inactive|pricing|restart)")
_SERVER_PAGE_MOVED = {"inactive": "accounts/inactive-accounts", "pricing": "server-tools/pricing",
                      "restart": "server-tools/restart"}


def moved_to(path: str) -> str | None:
    """The new address of an old back-office page path ("symbols/12" -> "trading-setup/symbols/12"), or None."""
    if any(path == new or path.startswith(new + "/") for new in MOVED.values()):
        return None  # already a current address
    m = _SERVER_PAGES.fullmatch(path)
    if m:
        return _SERVER_PAGE_MOVED[m.group(1)]
    first, _, rest = path.partition("/")
    if first in MOVED:
        return MOVED[first] + ("/" + rest if rest else "")
    return None


@router.get("/{path:path}")
def old_address(request: Request, path: str):
    """Registered last: only addresses no page answers reach it."""
    new = moved_to(path)
    if new is None:
        raise HTTPException(404)
    query = request.url.query
    return redirect(f"/admin/{new}" + (f"?{query}" if query else ""))
