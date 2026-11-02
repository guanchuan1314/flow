"""KYC methods: several can be configured, exactly one is active and used for every client.

* manual: the client uploads the required documents and staff review them (KYC queue).
* custom: an external verification provider. The client is sent to the provider's unique
  verification URL; the provider's callback approves or rejects the client (see services.integrations).
With no active method, the manual flow is used with any document type.
A custom method approves clients by itself: setting its secret or activating it needs kyc.review
as well as kyc.methods. Only callbacks for sessions still waiting are processed.
"""

import secrets

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import Conflict, ServiceError
from app.services.clients import MAX_DOC_BYTES
from app.core.security import encrypt_secret
from app.models import (
    CUSTOM, MANUAL, Client, KycDocument, KycMethod, KycSession, KycStatus,
)
from app.services import integrations

SCOPE = "kyc"
# Document categories a manual method can require, and the document types that satisfy each.
CATEGORIES = {
    "identity": ("Photo ID (passport, ID card or driving licence)", {"passport", "id_card", "driving_license"}),
    "proof_of_address": ("Proof of address (utility bill or bank statement)", {"proof_of_address"}),
    "selfie": ("Selfie holding the ID", {"selfie"}),
}
MAX_PENDING_DOCUMENTS = 10  # per client, awaiting review
MAX_DOCUMENTS = 50  # per client, in total
CHUNK = 64 * 1024


def active_method(db: Session) -> KycMethod | None:
    return db.scalar(select(KycMethod).where(KycMethod.active.is_(True)))


def _apply(m: KycMethod, fields: dict) -> None:
    secret, api_key = fields.pop("secret", None), fields.pop("api_key", None)
    for k, v in fields.items():
        setattr(m, k, v)
    if secret:
        m.secret_enc = encrypt_secret(secret)
    if api_key:
        m.api_key_enc = encrypt_secret(api_key)
    if m.kind not in (MANUAL, CUSTOM):
        raise ServiceError("Type must be manual or custom")
    m.callback_ips = integrations.clean_ips(m.callback_ips if m.callback_ips is not None else [integrations.ANY_IP])
    unknown = set(m.required_documents or []) - set(CATEGORIES)
    if unknown:
        raise ServiceError(f"Unknown document categories: {', '.join(sorted(unknown))}")
    if m.is_custom:
        if not (m.api_url or m.redirect_template):
            raise ServiceError("A custom method needs the provider's API URL or a redirect URL template")
        if not m.secret_enc:
            raise ServiceError("A custom method needs the shared secret used to sign callbacks")
    elif not m.required_documents:
        raise ServiceError("Choose at least one required document")
    if m.api_url:
        integrations.check_url(m.api_url, resolve=False)


def _sensitive(db: Session, actor, m: KycMethod, secret_set: bool, was_auto: bool) -> None:
    """A new secret, or the active method becoming a custom one, lets callbacks approve clients:
    it needs kyc.review too, and is audited on its own."""
    auto_on = m.active and m.is_custom and not was_auto
    if secret_set:
        integrations.require_perm(db, actor, "kyc.review", "Changing the callback secret")
    if auto_on:
        integrations.require_perm(db, actor, "kyc.review", "Letting a provider approve clients")
    if secret_set:
        audit(db, actor, "kyc_method.secret_change", "kyc_method", m.id, {"name": m.name})
    if auto_on:
        audit(db, actor, "kyc_method.auto_approve_on", "kyc_method", m.id, {"name": m.name})


def create_method(db: Session, actor, **fields) -> KycMethod:
    if db.scalar(select(KycMethod.id).where(KycMethod.name == fields["name"])):
        raise Conflict("A KYC method with this name already exists")
    m = KycMethod(callback_token=integrations.new_token(), **integrations.PROVIDER_DEFAULTS, active=False,
                  required_documents=["identity", "proof_of_address"])
    secret_set = bool(fields.get("secret"))
    _apply(m, fields)
    db.add(m)
    db.flush()
    _sensitive(db, actor, m, secret_set, was_auto=False)
    audit(db, actor, "kyc_method.create", "kyc_method", m.id, {"name": m.name, "kind": m.kind})
    db.commit()
    return m


def update_method(db: Session, actor, m: KycMethod, fields: dict) -> KycMethod:
    if db.scalar(select(KycMethod.id).where(KycMethod.name == fields.get("name", m.name), KycMethod.id != m.id)):
        raise Conflict("A KYC method with this name already exists")
    was_auto, secret_set = m.active and m.is_custom, bool(fields.get("secret"))
    _apply(m, fields)
    _sensitive(db, actor, m, secret_set, was_auto)
    audit(db, actor, "kyc_method.update", "kyc_method", m.id,
          {k: v for k, v in fields.items() if k not in ("secret", "api_key")})
    db.commit()
    return m


def activate(db: Session, actor, m: KycMethod) -> KycMethod:
    """Make `m` the one active method (all others are switched off)."""
    if m.is_custom and not m.active:
        integrations.require_perm(db, actor, "kyc.review", "Letting a provider approve clients")
        audit(db, actor, "kyc_method.auto_approve_on", "kyc_method", m.id, {"name": m.name})
    db.scalars(select(KycMethod).with_for_update()).all()  # serialise concurrent activations
    for other in db.scalars(select(KycMethod).where(KycMethod.active.is_(True), KycMethod.id != m.id)):
        other.active = False
    m.active = True
    audit(db, actor, "kyc_method.activate", "kyc_method", m.id, {"name": m.name})
    db.commit()
    return m


def new_callback_url(db: Session, actor, m: KycMethod) -> KycMethod:
    m.callback_token = integrations.new_token()
    audit(db, actor, "kyc_method.new_callback_url", "kyc_method", m.id, {})
    db.commit()
    return m


def delete_method(db: Session, actor, m: KycMethod) -> None:
    if m.active:
        raise ServiceError("This is the active KYC method; activate another one first")
    used = db.scalar(select(func.count()).select_from(KycSession).where(KycSession.method_id == m.id))
    if used:
        raise ServiceError(f"{used} verification(s) used this method; keep it for their history.")
    audit(db, actor, "kyc_method.delete", "kyc_method", m.id, {"name": m.name})
    db.delete(m)
    db.commit()


# ---------------------------------------------------------------- manual: uploads
def read_upload(fileobj, limit: int = MAX_DOC_BYTES) -> bytes:
    """Read an uploaded file in chunks, refusing it as soon as it passes `limit` bytes."""
    out = bytearray()
    while chunk := fileobj.read(CHUNK):
        out += chunk
        if len(out) > limit:
            raise ServiceError(f"File too large (max {limit // (1024 * 1024)} MB)")
    return bytes(out)


def check_upload_allowed(db: Session, client: Client) -> None:
    """Cap the documents a client can upload (each one is stored and lands in the review queue)."""
    rows = dict(db.execute(select(KycDocument.status, func.count()).where(KycDocument.client_id == client.id)
                           .group_by(KycDocument.status)).all())
    if rows.get(KycStatus.pending, 0) >= MAX_PENDING_DOCUMENTS:
        raise ServiceError(f"You already have {MAX_PENDING_DOCUMENTS} documents waiting for review; "
                           "please wait for them to be reviewed")
    if sum(rows.values()) >= MAX_DOCUMENTS:
        raise ServiceError(f"Upload limit reached ({MAX_DOCUMENTS} documents); please contact support")


# ---------------------------------------------------------------- manual: required documents
def checklist(db: Session, client: Client, m: KycMethod | None) -> list[dict]:
    """Required categories with the client's progress (empty when nothing specific is required)."""
    if m is None or m.is_custom:
        return []
    docs = db.scalars(select(KycDocument).where(KycDocument.client_id == client.id,
                                                KycDocument.status != KycStatus.rejected)).all()
    out = []
    for key in m.required_documents or []:
        label, types = CATEGORIES[key]
        mine = [d for d in docs if d.doc_type in types]
        out.append({"key": key, "label": label, "types": sorted(types), "done": bool(mine),
                    "approved": any(d.status == KycStatus.approved for d in mine)})
    return out


def submission_complete(db: Session, client: Client) -> bool:
    """True when the client uploaded everything the active method requires."""
    return all(item["done"] for item in checklist(db, client, active_method(db)))


# ---------------------------------------------------------------- custom: provider sessions
def start_session(db: Session, client: Client, return_url: str) -> str:
    m = active_method(db)
    if m is None or not m.is_custom:
        raise ServiceError("Online verification is not available; please upload your documents")
    if client.kyc_status == KycStatus.approved:
        raise ServiceError("Your identity is already verified")
    session = KycSession(client_id=client.id, method_id=m.id, reference=f"K{client.id}-{secrets.token_hex(8)}")
    db.add(session)
    db.flush()
    payload = {"reference": session.reference, "client_id": client.id, "email": client.email,
               "first_name": client.first_name, "last_name": client.last_name, "country": client.country or "",
               "date_of_birth": client.date_of_birth.isoformat() if client.date_of_birth else "",
               "callback_url": integrations.callback_url(SCOPE, m.callback_token), "return_url": return_url}
    try:
        session.url = integrations.start(m, payload)[:1024]
    except ServiceError as exc:
        session.status, session.reason = "failed", exc.message[:255]
        audit(db, None, "kyc.session_failed", "client", client.id, {"error": exc.message})
        db.commit()
        raise ServiceError("Online verification is not available right now. Please try again later.", 502) from exc
    if client.kyc_status in (KycStatus.not_submitted, KycStatus.rejected):
        client.kyc_status = KycStatus.pending
    audit(db, client, "kyc.session_start", "client", client.id, {"method": m.name, "reference": session.reference})
    db.commit()
    return session.url


def handle_callback(db: Session, token: str, raw: bytes, headers, content_type: str, ip: str | None) -> tuple[int, str]:
    from app.services.clients import set_kyc_status

    m = db.scalar(select(KycMethod).where(KycMethod.callback_token == token))
    if m is None or not m.is_custom:
        return 404, "unknown callback URL"
    data = integrations.parse_body(raw, content_type)
    ref = integrations.field(data, m.ref_field)
    ok = integrations.signature_ok(m, raw, headers)

    def done(code: int, result: str) -> tuple[int, str]:
        integrations.log_callback(db, SCOPE, m.id, ref, ip, ok, result, raw, data)
        db.commit()
        return code, result

    if not integrations.callback_ip_allowed(m, ip):
        return done(403, f"IP {ip} is not on the callback whitelist")
    if not ok:
        return done(401, "invalid signature")
    session = db.scalar(select(KycSession).where(KycSession.reference == str(ref), KycSession.method_id == m.id)
                        .with_for_update())
    if session is None:
        return done(404, f"unknown reference {ref}")
    if session.status != "started":  # replays, and late callbacks once the session is decided
        return done(200, f"ignored: session already {session.status}")
    state, reason = integrations.outcome(m, data)
    if state == "pending":
        return done(200, "noted, still pending")
    client = db.get(Client, session.client_id)
    if client.kyc_status != KycStatus.pending:  # staff decided meanwhile (or the client is already verified)
        session.status, session.reason = "superseded", f"client already {client.kyc_status.value}"
        return done(200, f"ignored: client already {client.kyc_status.value}")
    session.status, session.reason = ("approved", None) if state == "success" else ("rejected", reason)
    set_kyc_status(db, None, client, KycStatus.approved if state == "success" else KycStatus.rejected)
    return done(200, f"client {state == 'success' and 'approved' or 'rejected'}")
