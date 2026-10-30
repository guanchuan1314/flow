from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, TimestampMixin, utcnow


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_entity", "entity", "entity_id"),
                      Index("ix_audit_logs_actor", "actor_type", "actor_id"))

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor_type: Mapped[str] = mapped_column(String(16))  # staff | client | system
    actor_id: Mapped[int | None] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    principal_type: Mapped[str] = mapped_column(String(16))
    email: Mapped[str] = mapped_column(String(255), index=True)
    ip: Mapped[str | None] = mapped_column(String(64))
    success: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(String(64))


class IpRule(Base, TimestampMixin):
    """IP blacklist / whitelist entry for the CRM, optionally mirrored to the MT5 servers' firewall."""

    __tablename__ = "ip_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    network: Mapped[str] = mapped_column(String(64))  # one address or a CIDR range, IPv4 or IPv6
    action: Mapped[str] = mapped_column(String(8))  # block | allow
    scope: Mapped[str] = mapped_column(String(8), default="all")  # all | admin | portal | website (the CRM side)
    description: Mapped[str] = mapped_column(String(255), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    apply_mt5: Mapped[bool] = mapped_column(Boolean, default=False)
    mt5_server_ids: Mapped[list] = mapped_column(JSON, default=list)  # empty: every enabled MT5 server
    mt5_state: Mapped[dict] = mapped_column(JSON, default=dict)  # server id -> {"ok", "message", "at"}
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))

    @property
    def mt5_tag(self) -> str:
        """Comment prefix identifying this rule in the MT5 firewall."""
        return f"CRM#{self.id}:"


class RevokedToken(Base):
    __tablename__ = "revoked_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
