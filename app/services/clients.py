import uuid
from pathlib import Path

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.audit import audit
from app.core.errors import Conflict, Forbidden, NotFound, ServiceError
from app.core.security import hash_password, new_referral_code, validate_password_policy
from app.db import utcnow
from app.services import notifications
from app.models import Client, ClientNote, ClientStatus, KycDocument, KycStatus, Staff, TradingAccount

ALLOWED_DOC_TYPES = {"passport", "id_card", "driving_license", "proof_of_address", "selfie", "other"}
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
MAX_DOC_BYTES = 10 * 1024 * 1024
FILE_SIGNATURES = {
    ".pdf": (b"%PDF-",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
}


def visible_clients(staff: Staff) -> Select:
    """Clients a staff member may see: all, or those assigned to them or their direct reports."""
    q = select(Client)
    if staff.has_perm("clients.view_all"):
        return q
    team = select(Staff.id).where(or_(Staff.id == staff.id, Staff.manager_id == staff.id))
    return q.where(or_(Client.sales_staff_id.in_(team), Client.account_manager_id.in_(team)))


def visible_client_ids(staff: Staff) -> Select:
    """The ids of visible_clients(staff), for `.in_()`."""
    return visible_clients(staff).with_only_columns(Client.id)


def visible_logins(staff: Staff) -> Select:
    """The trading account logins of the clients a staff member may see, for `.in_()`."""
    return select(TradingAccount.login).where(TradingAccount.client_id.in_(visible_client_ids(staff)))


def ensure_visible(db: Session, staff: Staff, client_id: int) -> Client:
    client = db.scalar(visible_clients(staff).where(Client.id == client_id))
    if client is None:
        if db.get(Client, client_id):
            raise Forbidden("Client is not assigned to you")
        raise NotFound(f"Client {client_id} not found")
    return client


def visible_account(db: Session, staff: Staff, login: int) -> TradingAccount:
    """The trading account, if its client is visible to the staff member (404 / 403 otherwise)."""
    from app.services.accounts import get_account

    acc = get_account(db, login)
    ensure_visible(db, staff, acc.client_id)
    return acc


def _unique_referral_code(db: Session) -> str:
    while True:
        code = new_referral_code()
        if not db.scalar(select(Client.id).where(Client.referral_code == code)):
            return code


WEBSITE_LEAD_SOURCE = "website-contact"


def _claimable_lead(db: Session, email: str) -> Client | None:
    """A lead the contact form created (no password, no accounts yet) that sign-up may take over.

    Trade-off: anyone who signs up with that address gets the lead record (the name, phone and country the
    enquiry gave, plus the sales assignment), without proving they own the address. Refusing instead would
    lock the real person out of signing up after using the contact form, and the record holds nothing that
    the sign-up form doesn't ask for anyway. Every other existing email keeps the "already registered" answer.
    """
    client = db.scalar(select(Client).where(Client.email == email))
    if (client is None or client.password_hash or client.status != ClientStatus.lead
            or client.lead_source != WEBSITE_LEAD_SOURCE
            or db.scalar(select(TradingAccount.id).where(TradingAccount.client_id == client.id).limit(1))):
        return None
    return client


def register_client(db: Session, actor, *, email: str, first_name: str, last_name: str,
                    password: str | None = None, phone: str | None = None, country: str | None = None,
                    referral_code: str | None = None, lead_source: str | None = None,
                    utm_source: str | None = None, utm_medium: str | None = None,
                    utm_campaign: str | None = None, sales_staff_id: int | None = None,
                    ip: str | None = None) -> Client:
    email = email.lower()
    if password:
        validate_password_policy(password)
    if db.scalar(select(Client.id).where(Client.email == email)):
        lead = _claimable_lead(db, email) if password and actor is None else None
        if lead is None:
            raise Conflict("Email already registered")
        return _claim_lead(db, lead, first_name=first_name, last_name=last_name, password=password, phone=phone,
                           country=country, ip=ip)
    referrer = None
    if referral_code:
        referrer = db.scalar(select(Client).where(Client.referral_code == referral_code.upper()))
        if referrer is None:
            raise ServiceError("Invalid referral code")
    client = Client(
        email=email, first_name=first_name, last_name=last_name,
        password_hash=hash_password(password) if password else None, phone=phone,
        country=country.upper() if country else None, referral_code=_unique_referral_code(db),
        referred_by_id=referrer.id if referrer else None,
        lead_source=lead_source or ("referral" if referrer else None),
        utm_source=utm_source, utm_medium=utm_medium, utm_campaign=utm_campaign,
        sales_staff_id=sales_staff_id or (referrer.sales_staff_id if referrer else None),
        status=ClientStatus.active if password else ClientStatus.lead,
    )
    db.add(client)
    db.flush()
    audit(db, actor, "client.register", "client", client.id,
          {"referrer": referrer.id if referrer else None, "utm_campaign": utm_campaign}, ip=ip)
    db.commit()
    return client


def _claim_lead(db: Session, client: Client, *, first_name: str, last_name: str, password: str,
                phone: str | None, country: str | None, ip: str | None) -> Client:
    """Self sign-up with the email of a website lead: the lead becomes the client (see _claimable_lead)."""
    client.first_name, client.last_name = first_name, last_name
    client.password_hash = hash_password(password)
    client.phone = phone or client.phone
    client.country = country.upper() if country else client.country
    client.status = ClientStatus.active
    audit(db, None, "client.register", "client", client.id, {"claimed_lead": True}, ip=ip)
    db.commit()
    return client


def update_client(db: Session, actor, client: Client, changes: dict) -> Client:
    """Save the changes, then write them to the client's MT5 accounts (see services.client_sync).
    Staff need clients.change_email to change the email (the client's login); the old address is told."""
    from app.services import client_sync

    old_email = client.email
    if "email" in changes:
        changes["email"] = changes["email"].strip().lower()
        if changes["email"] == old_email:
            del changes["email"]
    if "email" in changes:
        if isinstance(actor, Staff) and not actor.has_perm("clients.change_email"):
            raise Forbidden("Changing a client's email needs the permission clients.change_email")
        if "@" not in changes["email"]:
            raise ServiceError("Enter a valid email address")
        if db.scalar(select(Client.id).where(Client.email == changes["email"], Client.id != client.id)):
            raise Conflict("Another client already uses this email")
    before = {k: getattr(client, k) for k in changes}
    for key, value in changes.items():
        setattr(client, key, value)
    audit(db, actor, "client.update", "client", client.id, {"before": before, "after": changes})
    db.commit()
    if "email" in changes:
        notifications.send_email(old_email, "Your email address was changed",
                                 f"Hello {client.first_name},\n\nThe email address of your account was changed to "
                                 f"{changes['email']}. You now sign in with the new address.\n\n"
                                 "If you did not ask for this, contact support straight away.")
    if any(before[k] != changes[k] for k in client_sync.MT5_FIELDS & set(changes)):
        client_sync.push_client(db, client)
    return client


def assign_client(db: Session, actor: Staff, client: Client, sales_staff_id: int | None,
                  account_manager_id: int | None) -> Client:
    for sid in (sales_staff_id, account_manager_id):
        if sid is not None and not db.get(Staff, sid):
            raise ServiceError(f"Staff {sid} not found", 404)
    before = {"sales": client.sales_staff_id, "am": client.account_manager_id}
    client.sales_staff_id = sales_staff_id
    client.account_manager_id = account_manager_id
    audit(db, actor, "client.assign", "client", client.id,
          {"before": before, "after": {"sales": sales_staff_id, "am": account_manager_id}})
    db.commit()
    return client


def add_note(db: Session, actor: Staff, client: Client, body: str) -> ClientNote:
    note = ClientNote(client_id=client.id, staff_id=actor.id, body=body)
    db.add(note)
    db.commit()
    return note


# ---------------------------------------------------------------- KYC


def store_upload(area: str, client_id: int, filename: str, content: bytes) -> str:
    """Validate an uploaded PDF/JPG/PNG and store it under a random name. Returns the path."""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ServiceError(f"File type must be one of {sorted(ALLOWED_EXTENSIONS)}")
    if len(content) > MAX_DOC_BYTES:
        raise ServiceError("File too large (max 10 MB)")
    if not any(content.startswith(sig) for sig in FILE_SIGNATURES[ext]):
        raise ServiceError("File content does not match its type")
    folder = Path(get_settings().storage_dir) / area / str(client_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}{ext}"  # never trust the client-supplied name on disk
    path.write_bytes(content)
    return str(path)


def upload_kyc_document(db: Session, actor, client: Client, doc_type: str, filename: str, content: bytes) -> KycDocument:
    if doc_type not in ALLOWED_DOC_TYPES:
        raise ServiceError(f"doc_type must be one of {sorted(ALLOWED_DOC_TYPES)}")
    from app.services import kyc_methods

    kyc_methods.check_upload_allowed(db, client)  # every path (portal, API, admin) is capped
    path = store_upload("kyc", client.id, filename, content)
    doc = KycDocument(client_id=client.id, doc_type=doc_type, file_path=path, original_name=filename[:255])

    db.add(doc)
    db.flush()
    # under review once everything the active KYC method requires has been uploaded
    if client.kyc_status in (KycStatus.not_submitted, KycStatus.rejected) and kyc_methods.submission_complete(db, client):
        client.kyc_status = KycStatus.pending
    audit(db, actor, "kyc.upload", "kyc_document", doc.id, {"client_id": client.id, "doc_type": doc_type})
    db.commit()
    return doc


def review_kyc_document(db: Session, actor: Staff, doc: KycDocument, approve: bool, reason: str | None) -> KycDocument:
    if doc.status != KycStatus.pending:
        raise ServiceError("Document already reviewed")
    if not approve and not reason:
        raise ServiceError("A rejection reason is required")
    doc.status = KycStatus.approved if approve else KycStatus.rejected
    doc.reviewed_by_id = actor.id
    doc.reviewed_at = utcnow()
    doc.reject_reason = None if approve else reason
    audit(db, actor, "kyc.review", "kyc_document", doc.id, {"approved": approve, "reason": reason})
    db.commit()
    if not approve:
        notifications.notify_client(db.get(Client, doc.client_id), "Document rejected",
                                    f"Your {doc.doc_type.replace('_', ' ')} was rejected: {reason}\n"
                                    "Please upload a new document in the client portal.")
    return doc


def set_kyc_status(db: Session, actor: Staff, client: Client, status: KycStatus) -> Client:
    before = client.kyc_status
    client.kyc_status = status
    if status == KycStatus.approved and client.status == ClientStatus.lead:
        client.status = ClientStatus.active
    audit(db, actor, "kyc.status", "client", client.id, {"before": before, "after": status})
    db.commit()
    if status in (KycStatus.approved, KycStatus.rejected) and status != before:
        notifications.notify_client(client, f"Verification {status.value}",
                                    "Your identity verification has been approved. You can now open live accounts."
                                    if status == KycStatus.approved else
                                    "Your identity verification was not approved. Please contact support.")
    return client


def capture_website_lead(db: Session, *, name: str, email: str, phone: str | None, country: str | None,
                         topic: str, message: str, ip: str | None = None) -> Client:
    """Contact-form enquiry: create a lead (or attach to the existing client) with a note for sales."""
    email = email.strip().lower()
    if "@" not in email or len(email) > 255:
        raise ServiceError("Please enter a valid email address")
    if not message.strip():
        raise ServiceError("Please write a message")
    client = db.scalar(select(Client).where(Client.email == email))
    if client is None:
        first, _, last = name.strip().partition(" ")
        client = register_client(db, None, email=email, first_name=(first or "Website")[:64],
                                 last_name=(last or "Lead")[:64], phone=(phone or None),
                                 country=(country or "").strip().upper()[:2] or None, lead_source=WEBSITE_LEAD_SOURCE, ip=ip)
    db.add(ClientNote(client_id=client.id, staff_id=None,
                      body=f"Website enquiry ({topic}) from {name.strip() or email}:\n{message.strip()[:4000]}"))
    audit(db, None, "lead.website_enquiry", "client", client.id, {"topic": topic}, ip=ip)
    db.commit()
    return client
