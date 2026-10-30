"""Two-step (password, then 2FA code) login shared by the admin and the portal."""

from fastapi import Request
from sqlalchemy.orm import Session

from app.core.deps import client_ip
from app.core.errors import ServiceError
from app.core.ratelimit import check_auth_rate
from app.db import utcnow
from app.services import auth
from app.web.common import login_session, logout_session

PENDING_SECONDS = 300


def _pending_key(key: str) -> str:
    return f"pending2fa_{key}"


def password_step(request: Request, db: Session, key: str, authenticate, email: str, password: str):
    """Returns (principal, needs_totp). Raises ServiceError on failure."""
    ip = client_ip(request)
    check_auth_rate(f"{key}-login", ip)
    request.session.pop(_pending_key(key), None)
    try:
        principal = authenticate(db, email, password, None, ip)
    except auth.TotpRequired:
        from sqlalchemy import select

        model = auth.Staff if key == "staff" else auth.Client
        principal = db.scalar(select(model).where(model.email == email.lower()))
        request.session[_pending_key(key)] = {"id": principal.id, "at": utcnow().timestamp()}
        return principal, True
    login_session(request, key, principal)
    return principal, False


def has_pending(request: Request, key: str) -> bool:
    pending = request.session.get(_pending_key(key))
    return bool(pending) and utcnow().timestamp() - pending["at"] < PENDING_SECONDS


def totp_step(request: Request, db: Session, key: str, model, code: str):
    ip = client_ip(request)
    check_auth_rate(f"{key}-login", ip)
    pending = request.session.get(_pending_key(key))
    if not pending or utcnow().timestamp() - pending["at"] >= PENDING_SECONDS:
        request.session.pop(_pending_key(key), None)
        raise ServiceError("Your sign-in attempt expired. Please enter your password again.", 401)
    principal = db.get(model, pending["id"])
    if principal is None:
        raise ServiceError("Sign-in failed", 401)
    auth.complete_second_factor(db, principal, code, ip)
    request.session.pop(_pending_key(key), None)
    logout_session(request, key)
    login_session(request, key, principal)
    return principal
