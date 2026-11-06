"""Branding: the broker's name, colour and contact details shown on the website, client area, back-office
and in emails. Defaults come from .env (APP_NAME, COMPANY_LEGAL_NAME, ...); Administration > Branding
overrides them in the database, so a change shows without a restart (each process re-reads within CACHE_SECONDS)."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, fields, replace

from markupsafe import Markup, escape
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.audit import audit
from app.core.errors import ServiceError
from app.services import app_settings

KEY = "branding"
CACHE_SECONDS = app_settings.CACHE_SECONDS
LOGO_KEY = "logo"
LOGO_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_ACCENT = "#2563eb"
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


@dataclass(frozen=True)
class Brand:
    app_name: str
    company_legal_name: str
    brand_tagline: str
    support_email: str
    support_phone: str
    regulatory_text: str
    mt5_server_name: str
    accent_color: str = DEFAULT_ACCENT

    @property
    def accent_rgb(self) -> str:
        """"r, g, b" of the accent colour, for rgba() in CSS."""
        h = self.accent_color.lstrip("#")
        return ", ".join(str(int(h[i:i + 2], 16)) for i in (0, 2, 4))

    @property
    def initials(self) -> str:
        """Up to two letters for the logo mark."""
        words = [w for w in re.split(r"\s+", self.app_name.strip()) if w]
        return ("".join(w[0] for w in words[:2]) or "?").upper()


# Field -> (label, help, max length); the order is the form's order
FIELDS = {
    "app_name": ("Broker name", "Shown in the header of every page, in page titles, emails and the 2FA app.", 64),
    "company_legal_name": ("Legal company name", "Used in the footer, terms and copyright lines.", 128),
    "brand_tagline": ("Tagline", "The website's headline and page-title suffix.", 128),
    "accent_color": ("Brand colour", "Buttons, links and highlights on every page (#RRGGBB).", 7),
    "support_email": ("Support email", "Shown to clients on the website and in the client area.", 128),
    "support_phone": ("Support phone", "Optional.", 64),
    "mt5_server_name": ("MT5 server name", "What clients pick in the MetaTrader 5 terminal.", 64),
    "regulatory_text": ("Regulatory text", "Footer wording: licence number and regulator.", 1000),
}

def defaults() -> Brand:
    s = get_settings()
    return Brand(**{f.name: getattr(s, f.name, DEFAULT_ACCENT) for f in fields(Brand)})


def current() -> Brand:
    """The brand to show now: .env defaults overridden by what the back-office saved (cached per process)."""
    return replace(defaults(), **{k: v for k, v in app_settings.load(KEY).items() if k in FIELDS and v})


def clear_cache() -> None:
    app_settings.clear(KEY)


def clean(values: dict) -> dict:
    """Validate the form's values; an empty field means "use the .env default"."""
    out = {}
    for name, (label, _help, limit) in FIELDS.items():
        v = str(values.get(name) or "").strip()
        if len(v) > limit:
            raise ServiceError(f"{label} is too long (at most {limit} characters)")
        if v and name == "accent_color" and not HEX.match(v):
            raise ServiceError("Brand colour must look like #2563eb")
        if v and name == "support_email" and "@" not in v:
            raise ServiceError("Support email is not an email address")
        out[name] = v.lower() if name == "accent_color" else v
    if not (out["app_name"] or get_settings().app_name):
        raise ServiceError("Broker name cannot be empty")
    return out


def save(db: Session, staff, values: dict, ip: str | None = None) -> Brand:
    value = {k: v for k, v in clean(values).items() if v}
    before = app_settings.save(db, KEY, value)
    audit(db, staff, "branding.update", "app_settings", KEY,
          {k: v for k, v in value.items() if before.get(k) != v}, ip=ip)
    db.commit()
    clear_cache()
    return current()


def stored(db: Session) -> dict:
    """What the back-office saved (empty = .env default), for the form."""
    return app_settings.stored(db, KEY)


def _logo_type(content: bytes) -> str | None:
    """The image type from the file's own first bytes: what a file is called or claims to be proves nothing."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    head = content[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return "image/svg+xml"
    return None


def logo() -> dict:
    """The saved logo: {type, data (base64), version}, or {} when there is none. In the database, like the rest of the
    branding, so every process and every server of the deployment has it."""
    return app_settings.load(LOGO_KEY)


def logo_file() -> tuple[bytes, str] | None:
    saved = logo()
    return (base64.b64decode(saved["data"]), saved["type"]) if saved else None


def save_logo(db: Session, staff, content: bytes, ip: str | None = None) -> None:
    if not content:
        raise ServiceError("Choose a logo file first")
    if len(content) > LOGO_MAX_BYTES:
        raise ServiceError("The logo is too big (at most 2 MB)")
    kind = _logo_type(content)
    if kind is None:
        raise ServiceError("The logo must be a PNG, JPEG, WebP or SVG image")
    app_settings.save(db, LOGO_KEY, {"type": kind, "data": base64.b64encode(content).decode(),
                                     "version": hashlib.sha256(content).hexdigest()[:12]})
    audit(db, staff, "branding.logo", "app_settings", LOGO_KEY, {"type": kind, "bytes": len(content)}, ip=ip)
    db.commit()
    app_settings.clear(LOGO_KEY)


def remove_logo(db: Session, staff, ip: str | None = None) -> None:
    app_settings.save(db, LOGO_KEY, {})
    audit(db, staff, "branding.logo", "app_settings", LOGO_KEY, {"removed": True}, ip=ip)
    db.commit()
    app_settings.clear(LOGO_KEY)


def mark(classes: str = "") -> Markup:
    """The broker's logo, or the mark with its initials while it has none. `classes` set the size."""
    brand, saved = current(), logo()
    if saved:
        return Markup('<img class="brand-logo {}" src="/brand/logo?v={}" alt="{}">').format(
            classes, saved["version"], brand.app_name)
    return Markup('<span class="brand-mark {}">{}</span>').format(classes, escape(brand.initials))


def css(brand: Brand) -> str:
    """The stylesheet served at /brand.css: the accent colour as CSS variables used by every page."""
    return (":root{"
            f"--brand:{brand.accent_color};--brand-rgb:{brand.accent_rgb};"
            f"--bs-primary:{brand.accent_color};--bs-primary-rgb:{brand.accent_rgb};"
            f"--bs-link-color:{brand.accent_color};--bs-link-color-rgb:{brand.accent_rgb};"
            "}\n")
