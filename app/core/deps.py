"""FastAPI dependencies: DB session, authenticated principals, RBAC, MT5 gateway."""

from datetime import datetime, timezone

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.security import decode_token, ip_allowed
from app.db import get_db
from app.models import Client, ClientStatus, Staff
from app.mt5 import MT5Gateway, routed
from app.services.auth import CLIENT_AUD, STAFF_AUD, is_revoked

bearer = HTTPBearer(auto_error=False)


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _payload(db: Session, creds: HTTPAuthorizationCredentials | None, audience: str) -> dict:
    if creds is None:
        raise HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = decode_token(creds.credentials, audience)
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    if is_revoked(db, payload["jti"]):
        raise HTTPException(401, "Token has been revoked")
    return payload


def _current_version(payload: dict, principal) -> None:
    """Tokens carry the principal's token_version; a password change, 2FA disable or deactivation bumps it."""
    if payload.get("tv") != (principal.token_version or 0):
        raise HTTPException(401, "Session ended: please log in again", headers={"WWW-Authenticate": "Bearer"})


def _staff(request: Request, creds, db: Session, enforce_mfa: bool) -> Staff:
    payload = _payload(db, creds, STAFF_AUD)
    staff = db.get(Staff, int(payload["sub"]))
    if staff is None or not staff.is_active:
        raise HTTPException(401, "Staff account disabled")
    _current_version(payload, staff)
    if not ip_allowed(client_ip(request), staff.allowed_ips):
        raise HTTPException(403, "Access not allowed from this IP address")
    if enforce_mfa and get_settings().staff_2fa_required and not payload.get("mfa"):
        raise HTTPException(403, "2FA enrollment required: enable 2FA and log in again")
    request.state.token = payload
    return staff


def current_staff(request: Request, creds=Depends(bearer), db: Session = Depends(get_db)) -> Staff:
    return _staff(request, creds, db, enforce_mfa=True)


def current_staff_no_mfa(request: Request, creds=Depends(bearer), db: Session = Depends(get_db)) -> Staff:
    """For the 2FA enrollment endpoints, reachable before 2FA is set up."""
    return _staff(request, creds, db, enforce_mfa=False)


def require(*perms: str):
    def checker(staff: Staff = Depends(current_staff)) -> Staff:
        missing = [p for p in perms if not staff.has_perm(p)]
        if missing:
            raise HTTPException(403, f"Missing permission: {', '.join(missing)}")
        return staff

    return checker


def current_client(request: Request, creds=Depends(bearer), db: Session = Depends(get_db)) -> Client:
    payload = _payload(db, creds, CLIENT_AUD)
    client = db.get(Client, int(payload["sub"]))
    if client is None or client.status in (ClientStatus.suspended, ClientStatus.closed):
        raise HTTPException(401, "Client account not active")
    _current_version(payload, client)
    request.state.token = payload
    return client


def token_info(request: Request) -> tuple[str, datetime]:
    payload = request.state.token
    return payload["jti"], datetime.fromtimestamp(payload["exp"], tz=timezone.utc).replace(tzinfo=None)


def gateway() -> MT5Gateway:
    """Routes each call to the MT5 server that owns the login involved."""
    return routed()
