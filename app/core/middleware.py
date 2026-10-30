import logging
import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from app.config import get_settings
from app.core.deps import client_ip

access_log = logging.getLogger("crm.access")
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "font-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
)
DOCS_CSP = (  # Swagger UI loads its assets from jsDelivr
    "default-src 'self'; img-src 'self' data: https://fastapi.tiangolo.com; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "frame-ancestors 'none'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        path = request.url.path
        h.setdefault("Content-Security-Policy", DOCS_CSP if path in ("/docs", "/redoc") else CSP)
        if not path.startswith("/static"):  # pages carry per-session CSRF tokens
            h.setdefault("Cache-Control", "no-store")
        if get_settings().is_prod:
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        sent = request.headers.get("X-Request-ID") or ""
        # a caller's id is kept only if it is safe to log and echo back
        rid = sent if REQUEST_ID_RE.fullmatch(sent) else uuid.uuid4().hex[:16]
        request.state.request_id = rid
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            access_log.exception("%s %s failed rid=%s", request.method, request.url.path, rid)
            raise
        ms = (time.perf_counter() - start) * 1000
        if not request.url.path.startswith("/static"):
            access_log.info("%s %s %s %.0fms ip=%s rid=%s", request.method, request.url.path, response.status_code,
                            ms, client_ip(request) or "-", rid)
        response.headers["X-Request-ID"] = rid
        return response


class IpFilterMiddleware(BaseHTTPMiddleware):
    """IP blacklist / whitelist (Administration > IP access rules)."""

    async def dispatch(self, request: Request, call_next):
        from starlette.concurrency import run_in_threadpool

        from app.services import ip_rules

        ip = client_ip(request)
        reason = await run_in_threadpool(ip_rules.check, ip, request.url.path)
        if reason is None:
            return await call_next(request)
        access_log.warning("refused %s %s from %s: %s", request.method, request.url.path, ip, reason)
        if request.url.path.startswith(("/api", "/callbacks")):
            return JSONResponse({"detail": "Access from your IP address is not allowed"}, status_code=403)
        return HTMLResponse("<!doctype html><title>Access denied</title><h1>Access denied</h1>"
                            "<p>Access from your IP address is not allowed. If you think this is a mistake, "
                            "please contact support.</p>", status_code=403)
