import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import custom
from app import db as dbmod
from app.api import accounts, auth, clients, copy, finance, marketing, portal, security, staff, trading
from app.config import get_settings
from app.core.errors import ServiceError
from app.core.logging_setup import configure_logging
from app.core.middleware import IpFilterMiddleware, RequestLogMiddleware, SecurityHeadersMiddleware
from app.mt5 import MT5Error, get_gateway, peek
from app.services import branding, events
from app.web import admin as web_admin
from app.web import callbacks as web_callbacks
from app.web import portal as web_portal
from app.web import quotes_ws
from app.web import site as web_site
from app.web.common import WebForbidden, WebRedirect, back, flash, templates

log = logging.getLogger(__name__)
LOCAL_ADDRESSES = ("127.0.0.1", "::1")
NON_WEB_PREFIXES = ("/api", "/static", "/brand.css", "/ws/", "/quotes.json", "/docs", "/openapi.json", "/health", "/ready", "/callbacks",
                    "/sandbox/provider/start")


@asynccontextmanager
async def lifespan(_: FastAPI):
    s = get_settings()
    if s.auto_migrate:
        dbmod.migrate()
    elif not dbmod.schema_is_current():
        log.critical("Database schema is not up to date - run `python -m app.cli migrate`")
    # The API process also reacts to deals it ingests (e.g. a manual sync).
    events.register_default_handlers()
    _recover_sync_runs()
    servers = _enabled_servers()
    for server in servers:  # connect early so /ready reflects reality
        try:
            get_gateway(server.id)
        except MT5Error as exc:
            log.error("MT5 server '%s' not reachable at startup (%s); requests will retry", server.name, exc)
    # The in-process worker is the default only when every server is a simulator (local demos).
    simulators_only = bool(servers) and all(sv.kind.value == "simulator" for sv in servers)
    embedded = s.embedded_worker if s.embedded_worker is not None else simulators_only
    worker = None
    if embedded:
        from app.workers.runner import Worker

        worker = Worker().start()
    log.info("%s started (env=%s, gateway=%s, embedded worker=%s)", s.app_name, s.app_env, s.mt5_gateway, embedded)
    yield
    if worker:
        worker.shutdown()


def _recover_sync_runs() -> None:
    """Background syncs this process was running when it stopped: marked as interrupted."""
    from app.services import sync_runs

    try:
        with dbmod.SessionLocal() as db:
            if sync_runs.recover_interrupted(db, "manual"):
                log.warning("synchronizations interrupted by the restart were marked as failed")
    except Exception:  # noqa: BLE001 - e.g. the table is not there before the migration
        log.exception("could not check for interrupted synchronizations")


def _enabled_servers():
    from sqlalchemy import select

    from app.models import MT5Server

    with dbmod.SessionLocal() as db:
        servers = list(db.scalars(select(MT5Server).where(MT5Server.enabled.is_(True))))
        for server in servers:
            db.expunge(server)
        return servers


def _is_web(request: Request) -> bool:
    return not request.url.path.startswith(NON_WEB_PREFIXES)


def _home(request: Request) -> str:
    path = request.url.path
    return "/admin" if path.startswith("/admin") else "/portal" if path.startswith("/portal") else "/"


def _error_page(request: Request, status_code: int, title: str, message: str):
    from app.web.common import csrf_token

    home = _home(request)
    try:
        csrf = csrf_token(request)
    except AssertionError:  # rendered outside the session middleware (e.g. a crash very early)
        csrf = ""
    return templates.TemplateResponse(request, "errors/error.html", {
        "status_code": status_code, "title": title, "message": message, "home": home, "csrf": csrf,
        "request_id": getattr(request.state, "request_id", None)}, status_code=status_code)


def create_app() -> FastAPI:
    s = get_settings()
    configure_logging("api")
    show_docs = not s.is_prod
    app = FastAPI(title=s.app_name, version="1.0.0", lifespan=lifespan,
                  description="Broker back-office and client portal API for MetaTrader 5.",
                  docs_url="/docs" if show_docs else None, redoc_url=None,
                  openapi_url="/openapi.json" if show_docs else None)

    # Order matters: the last added middleware runs first.
    app.add_middleware(SessionMiddleware, secret_key=s.secret_key, session_cookie="crm_session",
                       max_age=s.session_hours * 3600, same_site="lax", https_only=s.is_prod)
    app.add_middleware(IpFilterMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestLogMiddleware)
    hosts = [h.strip() for h in s.allowed_hosts.split(",") if h.strip()]
    if hosts and hosts != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    # ------------------------------------------------------------ error handling
    @app.exception_handler(WebRedirect)
    async def web_redirect(_: Request, exc: WebRedirect):
        return RedirectResponse(exc.url, status_code=303)

    @app.exception_handler(WebForbidden)
    async def web_forbidden(request: Request, exc: WebForbidden):
        return _error_page(request, 403, "Access denied", exc.message)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        if _is_web(request):
            if request.method == "POST":
                flash(request, exc.message, "danger")
                return back(request, _home(request))
            return _error_page(request, exc.status_code, "Something went wrong", exc.message)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.exception_handler(MT5Error)
    async def mt5_error(request: Request, exc: MT5Error):
        rid = getattr(request.state, "request_id", None)
        log.error("MT5 error on %s %s rid=%s: %s", request.method, request.url.path, rid, exc)
        if _is_web(request):
            return _error_page(request, 502, "Trading server unavailable",
                               "We could not reach the trading server. Please try again in a moment.")
        # the detail (server address, MT5 codes) stays in the log; the request id finds it
        return JSONResponse(status_code=502, content={"detail": "MT5 server error", "request_id": rid})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if _is_web(request):
            if request.method == "POST":
                flash(request, "Some fields are missing or invalid. Please check the form.", "danger")
                return back(request, _home(request))
            return _error_page(request, 400, "Invalid request", "The link or parameters are not valid.")
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if _is_web(request):
            titles = {404: "Page not found", 405: "Not allowed"}
            return _error_page(request, exc.status_code, titles.get(exc.status_code, "Error"), str(exc.detail))
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        if _is_web(request):
            return _error_page(request, 500, "Unexpected error",
                               "Something went wrong on our side. The team has been notified.")
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    # ------------------------------------------------------------ routes
    custom.load()
    custom.include_routes(app)
    for module in (auth, staff, clients, accounts, finance, trading, copy, marketing, security, portal):
        app.include_router(module.router, prefix="/api")
    app.include_router(web_admin.router)
    app.include_router(web_callbacks.router)
    if not get_settings().is_prod:  # fake payment / KYC provider for demos and tests
        from app.web import sandbox

        app.include_router(sandbox.router)
    app.include_router(quotes_ws.router)
    app.include_router(web_portal.router)
    app.include_router(web_site.router)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    @app.get("/brand.css", include_in_schema=False)
    def brand_css():
        """The broker's colour (Administration > Branding) as CSS variables, loaded by every page."""
        return Response(branding.css(branding.current()), media_type="text/css",
                        headers={"Cache-Control": f"public, max-age={branding.CACHE_SECONDS}"})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return RedirectResponse("/static/favicon.svg", status_code=301)

    @app.get("/health", tags=["system"])
    def health():
        """Liveness: the process is up."""
        return {"status": "ok"}

    @app.get("/ready", tags=["system"])
    def ready(request: Request):
        """Readiness: 503 only when the database is down. MT5 servers are reported one by one
        (one offline server should not take the whole site out of a load balancer). Other hosts get only
        the status; the details (server names, errors) only go to requests from this machine."""
        local = request.client is not None and request.client.host in LOCAL_ADDRESSES
        try:
            with dbmod.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            database = "ok"
        except Exception as exc:  # noqa: BLE001
            details = {"database": f"error: {exc.__class__.__name__}"} if local else {}
            return JSONResponse(status_code=503, content={"status": "down", **details})
        servers = {}
        for server in _enabled_servers():
            gw = peek(server.id)
            servers[server.name] = "ok" if gw is not None and gw.connected else "disconnected"
        healthy = bool(servers) and all(v == "ok" for v in servers.values())
        status = "ok" if healthy else "degraded"
        if not local:
            return {"status": status}
        return {"status": status, "database": database, "mt5": servers or "no servers configured"}

    return app


app = create_app()
