from fastapi import APIRouter, Depends

from app.web.admin import (
    accounts, auth, automation, branding, clients, copy, dashboard, finance, ip_rules, marketing, methods, operations, servers,
    staff, symbols, trading, trading_setup,
)
from app.web.common import csrf_protect

router = APIRouter(prefix="/admin", include_in_schema=False, dependencies=[Depends(csrf_protect)])
router.add_api_route("", dashboard.dashboard, methods=["GET"])
for module in (auth, clients, accounts, servers, symbols, trading_setup, operations, finance, methods, trading, copy,
               marketing, staff, ip_rules, automation, branding):
    router.include_router(module.router)
