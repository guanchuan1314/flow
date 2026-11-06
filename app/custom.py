"""Broker-specific modules: custom/<name>/ next to app/, which updates of Flow never touch (git ignores it).

A module is a folder with an __init__.py holding setup(crm); `crm` (Crm below) is how it plugs in: its own pages,
menu entries, permissions, reactions to new deals and scheduled jobs. Optional folders: templates/ (a file with the
path of one of ours replaces it), static/ (served at /static/custom/<name>/) and migrations/ (*.sql, each run once).
docs/custom/README.md is the guide, docs/custom/big_deals/ a working sample.

Modules load one by one: one that fails is logged and left out, shown to staff, and the CRM carries on without it.
"""

import importlib.util
import logging
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlalchemy import text

from app.config import get_settings
from app.core.permissions import PERMISSIONS

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
NAME = re.compile(r"[a-z][a-z0-9_]*")


@dataclass
class Job:
    label: str
    seconds: int
    fn: Callable
    last: float = float("-inf")


class Crm:
    """What a module's setup(crm) can do. Nothing takes effect unless setup finishes."""

    def __init__(self, name: str, folder: Path):
        self.name, self.folder = name, folder
        self.admin_routers: list[APIRouter] = []
        self.portal_routers: list[APIRouter] = []
        self.public_routers: list[APIRouter] = []
        self.menu_items: list[dict] = []
        self.permissions: dict[str, str] = {}
        self.deal_handlers: list[Callable] = []
        self.jobs: list[Job] = []

    def add_admin_routes(self, router: APIRouter) -> None:
        """Back-office pages at /admin/custom/<name>/...: staff only, forms need their CSRF token."""
        self.admin_routers.append(router)

    def add_portal_routes(self, router: APIRouter) -> None:
        """Client area pages at /portal/custom/<name>/...: signed-in clients only, forms need their CSRF token."""
        self.portal_routers.append(router)

    def add_public_routes(self, router: APIRouter) -> None:
        """Open addresses at /custom/<name>/..., e.g. a provider's callback: the module checks who is calling."""
        self.public_routers.append(router)

    def add_menu(self, text: str, href: str, icon: str = "puzzle", perm: str | None = None,
                 section: str = "Custom") -> None:
        """A back-office menu entry; icon is a Bootstrap icon name, perm hides it from staff without that permission."""
        self.menu_items.append(dict(text=text, href=href, icon=icon, perm=perm, section=section))

    def add_permission(self, code: str, description: str) -> None:
        """A permission staff roles can be given (Staff > Roles), e.g. "big_deals.view"."""
        self.permissions[code] = description

    def on_deals(self, handler: Callable) -> None:
        """handler(db, deals) after new deals are saved. It commits its own work and must cope with seeing a deal twice."""
        self.deal_handlers.append(handler)

    def every(self, seconds: int, fn: Callable, label: str | None = None) -> None:
        """fn(db) in the worker every N seconds, at most once a minute; it commits its own work."""
        self.jobs.append(Job(f"custom {self.name}: {label or fn.__name__}", seconds, fn))


modules: list[Crm] = []
failed: dict[str, str] = {}  # module -> what went wrong, shown to staff
_loaded = False


def _folders() -> list[Path]:
    s = get_settings()
    root = ROOT / s.custom_dir
    if not s.custom_enabled or not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if (p / "__init__.py").is_file())


def _import(folder: Path):
    """The module under a name of its own, so its relative imports work wherever custom/ is."""
    name = f"flow_custom_{folder.name}"
    spec = importlib.util.spec_from_file_location(name, folder / "__init__.py",
                                                  submodule_search_locations=[str(folder)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _setup(folder: Path) -> Crm:
    if not NAME.fullmatch(folder.name):
        raise ValueError("the folder name must be lower case letters, digits and _")
    crm = Crm(folder.name, folder)
    _import(folder).setup(crm)
    taken = sorted(set(crm.permissions) & set(PERMISSIONS))
    if taken:
        raise ValueError(f"permission already exists: {', '.join(taken)}")
    return crm


def _activate(crm: Crm) -> None:
    from app.services import events

    PERMISSIONS.update(crm.permissions)
    for i, handler in enumerate(crm.deal_handlers):
        events.register(f"custom.{crm.name}.{i}", handler)
    modules.append(crm)
    log.info("custom module %s loaded", crm.name)


def _use_templates() -> None:
    """Module templates come first, so one with the path of ours replaces it."""
    from app.web.common import APP_DIR, templates

    folders = [crm.folder / "templates" for crm in modules if (crm.folder / "templates").is_dir()]
    if not folders:
        return
    for folder in folders:
        for page in folder.rglob("*.html"):
            if (APP_DIR / "templates" / page.relative_to(folder)).is_file():
                log.warning("%s replaces a page of Flow: check it after updates", page)
    templates.env.loader = ChoiceLoader([FileSystemLoader([str(f) for f in folders]), templates.env.loader])


def load() -> None:
    """Load the modules, once per process."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    for folder in _folders():
        try:
            crm = _setup(folder)
        except Exception as exc:  # noqa: BLE001 - one broker's module must not take the CRM down
            log.critical("custom module %s not loaded", folder.name, exc_info=True)
            failed[folder.name] = str(exc) or exc.__class__.__name__
            continue
        _activate(crm)
    _use_templates()


def include_routes(app: FastAPI) -> None:
    """Before the back-office and client area routers: /admin ends in a catch-all for old addresses."""
    from app.web.common import csrf_protect, web_client, web_staff

    for crm in modules:
        for router in crm.admin_routers:
            app.include_router(router, prefix=f"/admin/custom/{crm.name}", include_in_schema=False,
                               dependencies=[Depends(csrf_protect), Depends(web_staff)])
        for router in crm.portal_routers:
            app.include_router(router, prefix=f"/portal/custom/{crm.name}", include_in_schema=False,
                               dependencies=[Depends(csrf_protect), Depends(web_client)])
        for router in crm.public_routers:
            app.include_router(router, prefix=f"/custom/{crm.name}", include_in_schema=False)
        if (crm.folder / "static").is_dir():
            app.mount(f"/static/custom/{crm.name}", StaticFiles(directory=crm.folder / "static"))


def menu(staff) -> list[tuple[str, list[dict]]]:
    """The menu entries this staff member may see: [(section, entries)]."""
    sections: dict[str, list[dict]] = {}
    for crm in modules:
        for item in crm.menu_items:
            if item["perm"] is None or staff.has_perm(item["perm"]):
                sections.setdefault(item["section"], []).append(item)
    return list(sections.items())


def problems() -> dict[str, str]:
    return failed


def steps() -> list[tuple[str, Callable]]:
    """The modules' jobs as worker scheduler steps, which run every minute."""
    def due(job: Job) -> Callable:
        def step(db) -> None:
            if time.monotonic() - job.last >= job.seconds:
                job.last = time.monotonic()
                job.fn(db)
        return step

    return [(job.label, due(job)) for crm in modules for job in crm.jobs]


def migrate() -> None:
    """Run each module's migrations/*.sql not run yet, in name order. A module's new files run in one transaction:
    if one fails none of them count, the module is reported, and the others carry on."""
    from app.db import MIGRATION_LOCK, engine

    for folder in _folders():
        files = sorted((folder / "migrations").glob("*.sql"))
        if not files:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK})
                conn.execute(text("CREATE TABLE IF NOT EXISTS custom_migrations (module text NOT NULL, file text NOT NULL,"
                                  " applied_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (module, file))"))
                done = set(conn.scalars(text("SELECT file FROM custom_migrations WHERE module = :m"), {"m": folder.name}))
                for file in files:
                    if file.name not in done:
                        conn.exec_driver_sql(file.read_text(encoding="utf-8"))
                        conn.execute(text("INSERT INTO custom_migrations (module, file) VALUES (:m, :f)"),
                                     {"m": folder.name, "f": file.name})
                        log.info("custom module %s: ran %s", folder.name, file.name)
        except Exception as exc:  # noqa: BLE001
            log.critical("custom module %s: migration failed", folder.name, exc_info=True)
            failed[folder.name] = f"migration failed: {exc.__class__.__name__}"
