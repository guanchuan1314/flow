import enum
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Enum, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, TimestampMixin


class ClientStatus(str, enum.Enum):
    lead = "lead"
    active = "active"
    suspended = "suspended"
    closed = "closed"


class KycStatus(str, enum.Enum):
    not_submitted = "not_submitted"
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class Client(Base, TimestampMixin):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str] = mapped_column(String(64))
    last_name: Mapped[str] = mapped_column(String(64))
    phone: Mapped[str | None] = mapped_column(String(32))
    country: Mapped[str | None] = mapped_column(String(2))
    city: Mapped[str | None] = mapped_column(String(64))
    address: Mapped[str | None] = mapped_column(String(255))
    date_of_birth: Mapped[date | None] = mapped_column(Date)

    status: Mapped[ClientStatus] = mapped_column(Enum(ClientStatus), default=ClientStatus.lead)
    kyc_status: Mapped[KycStatus] = mapped_column(Enum(KycStatus), default=KycStatus.not_submitted)

    # Staff ownership
    sales_staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), index=True)
    account_manager_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), index=True)

    # Marketing
    referral_code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    referred_by_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), index=True)
    # The tagged business development link they signed up with (models.staff.ReferralLink).
    referral_link_id: Mapped[int | None] = mapped_column(ForeignKey("referral_links.id", ondelete="SET NULL"), index=True)
    lead_source: Mapped[str | None] = mapped_column(String(64))
    utm_source: Mapped[str | None] = mapped_column(String(64))
    utm_medium: Mapped[str | None] = mapped_column(String(64))
    utm_campaign: Mapped[str | None] = mapped_column(String(64))

    # Security
    totp_secret: Mapped[str | None] = mapped_column(String(255))  # encrypted (encrypt_secret)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger)  # last accepted 30 s step: a code works once
    # Bumped on password change/reset and 2FA disable: older API tokens and web sessions stop working.
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Profile changes are pushed to the client's MT5 accounts; a failed push is retried by the worker.
    mt5_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    mt5_sync_error: Mapped[str | None] = mapped_column(String(255))

    referred_by: Mapped["Client | None"] = relationship(remote_side="Client.id")

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


# Case-insensitive email lookups (login, registration, reset).
Index("ix_clients_email_lower", func.lower(Client.email))


class KycDocument(Base, TimestampMixin):
    __tablename__ = "kyc_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    doc_type: Mapped[str] = mapped_column(String(32))  # passport, id_card, proof_of_address, ...
    file_path: Mapped[str] = mapped_column(String(512))
    original_name: Mapped[str] = mapped_column(String(255))
    status: Mapped[KycStatus] = mapped_column(Enum(KycStatus), default=KycStatus.pending)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    reject_reason: Mapped[str | None] = mapped_column(String(255))


class ClientNote(Base, TimestampMixin):
    __tablename__ = "client_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))  # NULL = from the website/system
    body: Mapped[str] = mapped_column(Text)
