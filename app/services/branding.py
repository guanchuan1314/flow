"""Branding: the broker's name, colour and contact details shown on the website, client area, back-office
and in emails. Defaults come from .env (APP_NAME, COMPANY_LEGAL_NAME, ...); Administration > Branding
overrides them in the database, so a change shows without a restart (each process re-reads within CACHE_SECONDS)."""

from __future__ import annotations

import re
from dataclasses import dataclass, fields, replace

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.audit import audit
from app.core.errors import ServiceError
from app.services import app_settings

KEY = "branding"
CACHE_SECONDS = app_settings.CACHE_SECONDS
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


def css(brand: Brand) -> str:
    """The stylesheet served at /brand.css: the accent colour as CSS variables used by every page."""
    return (":root{"
            f"--brand:{brand.accent_color};--brand-rgb:{brand.accent_rgb};"
            f"--bs-primary:{brand.accent_color};--bs-primary-rgb:{brand.accent_rgb};"
            f"--bs-link-color:{brand.accent_color};--bs-link-color-rgb:{brand.accent_rgb};"
            "}\n")
