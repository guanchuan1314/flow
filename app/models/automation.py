"""Automation: rules the CRM's worker checks against its own data, then acts on (nothing synced to MT5)."""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, TimestampMixin, utcnow


class AutomationRule(Base, TimestampMixin):
    """When `trigger` matches an account or client (with `params`, narrowed by `filters`), do `action`.
    A target is acted on again only after `cooldown_hours` (0 = once per target)."""

    __tablename__ = "automation_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # None: every server. Deleting a server deletes its rules: they must not turn into rules for every server.
    server_id: Mapped[int | None] = mapped_column(ForeignKey("mt5_servers.id", ondelete="CASCADE"))
    server = relationship("MT5Server")
    trigger: Mapped[str] = mapped_column(String(32))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    filters: Mapped[dict] = mapped_column(JSON, default=dict)  # {groups: MT5 group masks, kind: live | demo | any}
    action: Mapped[str] = mapped_column(String(32))
    action_params: Mapped[dict] = mapped_column(JSON, default=dict)
    cooldown_hours: Mapped[int] = mapped_column(Integer, default=24)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_result: Mapped[str] = mapped_column(String(255), default="")


class AutomationRun(Base):
    """One action taken (or tried) by a rule on one target: the log, and the cooldown's memory."""

    __tablename__ = "automation_runs"
    __table_args__ = (Index("ix_automation_runs_rule_target", "rule_id", "target", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("automation_rules.id", ondelete="CASCADE"), index=True)
    target: Mapped[str] = mapped_column(String(32))  # "account:<server>:<login>" | "client:<id>"
    server_id: Mapped[int | None] = mapped_column(ForeignKey("mt5_servers.id", ondelete="SET NULL"))
    server = relationship("MT5Server")
    login: Mapped[int | None] = mapped_column(BigInteger)
    client_id: Mapped[int | None] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    outcome: Mapped[str] = mapped_column(String(8))  # done | failed
    message: Mapped[str] = mapped_column(String(255), default="")
