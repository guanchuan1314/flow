"""Payment methods and KYC methods: built-in manual flows, or a custom external provider.

A "custom" method talks to the provider the same way for payments and KYC:
  * Start: the CRM asks the provider for a unique URL to send the client to, either by calling
    `api_url` (signed JSON POST) or by filling in `redirect_template`.
  * Result: the provider POSTs to the method's callback URL (/callbacks/<scope>/<callback_token>),
    signed with the shared secret. Field names and status values are configurable.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, TimestampMixin

MANUAL, CUSTOM = "manual", "custom"


class ProviderSettings:
    """Columns shared by payment and KYC methods."""

    name: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(8), default=MANUAL)  # manual | custom
    description: Mapped[str] = mapped_column(String(255), default="")  # shown to clients
    instructions: Mapped[str] = mapped_column(Text, default="")  # shown to clients (manual methods)
    # custom: how to get the unique URL for the client
    api_url: Mapped[str] = mapped_column(String(512), default="")
    redirect_template: Mapped[str] = mapped_column(String(1024), default="")
    url_field: Mapped[str] = mapped_column(String(64), default="url")  # where the URL is in the API response
    api_key_enc: Mapped[str] = mapped_column(String(512), default="")  # sent as "Authorization: Bearer"
    # custom: the provider's result callback
    secret_enc: Mapped[str] = mapped_column(String(512), default="")  # HMAC-SHA256 key, both directions
    signature_header: Mapped[str] = mapped_column(String(64), default="X-Signature")
    callback_token: Mapped[str] = mapped_column(String(64), unique=True)
    ref_field: Mapped[str] = mapped_column(String(64), default="reference")
    status_field: Mapped[str] = mapped_column(String(64), default="status")
    success_values: Mapped[str] = mapped_column(String(255), default="success,paid,completed,approved")
    failure_values: Mapped[str] = mapped_column(String(255), default="failed,cancelled,declined,expired,rejected")
    reason_field: Mapped[str] = mapped_column(String(64), default="reason")
    # Addresses allowed to send callbacks: IPs or CIDR ranges, "*" = any (the default)
    callback_ips: Mapped[list] = mapped_column(JSON, default=lambda: ["*"])

    @property
    def is_custom(self) -> bool:
        return self.kind == CUSTOM


class PaymentMethod(ProviderSettings, Base, TimestampMixin):
    __tablename__ = "payment_methods"

    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_deposit: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_withdrawal: Mapped[bool] = mapped_column(Boolean, default=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    min_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    max_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    requires_proof: Mapped[bool] = mapped_column(Boolean, default=False)  # manual: client uploads a receipt
    auto_credit: Mapped[bool] = mapped_column(Boolean, default=True)  # custom: credit on a "paid" callback
    amount_field: Mapped[str] = mapped_column(String(64), default="amount")  # custom: checked against the request
    icon: Mapped[str] = mapped_column(String(32), default="bank")  # Bootstrap icon name
    sort_order: Mapped[int] = mapped_column(Integer, default=100)


class KycMethod(ProviderSettings, Base, TimestampMixin):
    __tablename__ = "kyc_methods"

    id: Mapped[int] = mapped_column(primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False)  # exactly one method is used at a time
    # manual: document categories the client must upload (see services.kyc_methods.CATEGORIES)
    required_documents: Mapped[list] = mapped_column(JSON, default=lambda: ["identity", "proof_of_address"])


class KycSession(Base, TimestampMixin):
    """One verification attempt with a custom KYC provider."""

    __tablename__ = "kyc_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    method_id: Mapped[int] = mapped_column(ForeignKey("kyc_methods.id"))
    reference: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="started")  # started | approved | rejected | failed
    url: Mapped[str] = mapped_column(String(1024), default="")
    reason: Mapped[str | None] = mapped_column(String(255))


class ProviderCallback(Base):
    """Every callback received from a provider, kept for troubleshooting integrations."""

    __tablename__ = "provider_callbacks"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    scope: Mapped[str] = mapped_column(String(8))  # payment | kyc
    method_id: Mapped[int | None] = mapped_column(Integer, index=True)
    reference: Mapped[str | None] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(String(64))
    signature_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome: Mapped[str] = mapped_column(String(255), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
