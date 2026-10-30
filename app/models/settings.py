"""Settings changed in the back-office (stored in the database, so no restart is needed)."""

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, TimestampMixin


class AppSetting(Base, TimestampMixin):
    """One group of settings (e.g. "branding") as a JSON object; missing keys fall back to .env."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
