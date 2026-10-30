"""Login, lockout, 2FA and token revocation for staff and clients."""

import secrets
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.audit import audit
from app.core.errors import ServiceError
from app.core.security import (
    accepted_totp_step, create_token, hash_password, ip_allowed, new_totp_secret, open_totp_secret,
    seal_totp_secret, totp_uri, validate_password_policy, verify_password,
)
from app.db import utcnow
from app.models import Client, ClientStatus, LoginAttempt, RevokedToken, Staff

STAFF_AUD, CLIENT_AUD = "staff", "client"
LOCKED_MESSAGE = "Too many failed logins: please wait a few minutes and try again"


class AuthError(ServiceError):
    status_code = 401


def _record(db: Session, kind: str, email: str, ip: str | None, success: bool, reason: str | None = None):
    db.add(LoginAttempt(principal_type=kind, email=email.lower(), ip=ip, success=success, reason=reason))


_dummy_hash: str | None = None


def _burn_password_check(password: str) -> None:
    """Spend as long as a real password check, so the response time doesn't reveal whether an email exists."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(16))
    verify_password(password, _dummy_hash)


def bump_token_version(principal) -> None:
    """Invalidate every API token and web session issued to this staff member or client so far.
    Done in SQL (version + 1), so two bumps at the same time both count."""
    principal.token_version = type(principal).token_version + 1


def _locked(principal, now: datetime) -> bool:
    return bool(principal.locked_until and principal.locked_until > now)


def _count_failure(principal, now: datetime) -> None:
    """A wrong password or 2FA code. From MAX_FAILED_LOGINS failures on, each one makes the account wait
    1, 2, 4, ... minutes (at most LOCKOUT_MINUTES) before the next try: guessing gets slow quickly, while
    someone else's wrong guesses can only delay the real owner briefly. A day after the last wait the count restarts."""
    s = get_settings()
    if principal.locked_until and now - principal.locked_until > timedelta(days=1):
        principal.failed_logins = 0
    principal.failed_logins = (principal.failed_logins or 0) + 1
    over = principal.failed_logins - s.max_failed_logins
    if over >= 0:
        principal.locked_until = now + timedelta(minutes=min(2 ** min(over, 20), s.lockout_minutes))


def _totp_ok(principal, code: str | None) -> bool:
    """Check a 2FA code; each code is accepted only once. Encrypts a secret stored before encryption existed."""
    secret = open_totp_secret(principal.totp_secret)
    step = accepted_totp_step(secret, code, principal.totp_last_step)
    if step is None:
        return False
    principal.totp_last_step = step
    if not principal.totp_secret.startswith("gAAAA"):
        principal.totp_secret = seal_totp_secret(secret)
    return True


def _check_credentials(db: Session, kind: str, principal, email: str, password: str,
                       totp_code: str | None, ip: str | None):
    now = utcnow()
    if principal is None:
        _burn_password_check(password)
        _record(db, kind, email, ip, False, "unknown_email")
        db.commit()
        raise AuthError("Invalid email or password")
    if _locked(principal, now):
        _record(db, kind, email, ip, False, "locked")
        db.commit()
        raise AuthError(LOCKED_MESSAGE, 423)
    if not verify_password(password, principal.password_hash):
        _count_failure(principal, now)
        _record(db, kind, email, ip, False, "bad_password")
        db.commit()
        raise AuthError("Invalid email or password")
    if principal.totp_enabled:
        if not totp_code:
            raise TotpRequired("2FA code required", 401)
        if not _totp_ok(principal, totp_code):
            _count_failure(principal, now)
            _record(db, kind, email, ip, False, "bad_totp")
            db.commit()
            raise AuthError("Invalid 2FA code")
    principal.failed_logins = 0
    principal.locked_until = None
    principal.last_login_at = now
    _record(db, kind, email, ip, True)


class TotpRequired(AuthError):
    """Password was correct; a 2FA code is needed to finish logging in."""


def authenticate_staff(db: Session, email: str, password: str, totp_code: str | None, ip: str | None) -> Staff:
    staff = db.scalar(select(Staff).where(Staff.email == email.lower()))
    if staff is not None and not staff.is_active:
        _burn_password_check(password)
        _record(db, STAFF_AUD, email, ip, False, "inactive")
        db.commit()
        raise AuthError("Invalid email or password")
    _check_credentials(db, STAFF_AUD, staff, email, password, totp_code, ip)
    if not ip_allowed(ip, staff.allowed_ips):
        _record(db, STAFF_AUD, email, ip, False, "ip_not_allowed")
        db.commit()
        raise AuthError("Login not allowed from this IP address", 403)
    audit(db, staff, "auth.login", "staff", staff.id, ip=ip)
    db.commit()
    return staff


def authenticate_client(db: Session, email: str, password: str, totp_code: str | None, ip: str | None) -> Client:
    client = db.scalar(select(Client).where(Client.email == email.lower()))
    if client is not None and client.status in (ClientStatus.suspended, ClientStatus.closed):
        _record(db, CLIENT_AUD, email, ip, False, "suspended")
        db.commit()
        raise AuthError("Account is not active", 403)
    _check_credentials(db, CLIENT_AUD, client, email, password, totp_code, ip)
    audit(db, client, "auth.login", "client", client.id, ip=ip)
    db.commit()
    return client


def complete_second_factor(db: Session, principal, code: str, ip: str | None):
    """Second step of a two-step login whose password step already succeeded."""
    kind = STAFF_AUD if isinstance(principal, Staff) else CLIENT_AUD
    now = utcnow()
    if _locked(principal, now):
        raise AuthError(LOCKED_MESSAGE, 423)
    if isinstance(principal, Staff) and (not principal.is_active or not ip_allowed(ip, principal.allowed_ips)):
        _record(db, kind, principal.email, ip, False, "ip_not_allowed")
        db.commit()
        raise AuthError("Login not allowed from this IP address", 403)
    if not _totp_ok(principal, code):
        _count_failure(principal, now)
        _record(db, kind, principal.email, ip, False, "bad_totp")
        db.commit()
        raise AuthError("Invalid 2FA code")
    principal.failed_logins, principal.locked_until, principal.last_login_at = 0, None, now
    _record(db, kind, principal.email, ip, True)
    audit(db, principal, "auth.login", kind, principal.id, {"mfa": True}, ip=ip)
    db.commit()
    return principal


def staff_login(db: Session, email: str, password: str, totp_code: str | None, ip: str | None) -> dict:
    staff = authenticate_staff(db, email, password, totp_code, ip)
    token, _, exp = create_token(staff.id, STAFF_AUD, get_settings().access_token_minutes,
                                 {"mfa": bool(staff.totp_enabled), "tv": staff.token_version or 0})
    return {"access_token": token, "token_type": "bearer", "expires_at": exp,
            "mfa_enrollment_required": get_settings().staff_2fa_required and not staff.totp_enabled}


def client_login(db: Session, email: str, password: str, totp_code: str | None, ip: str | None) -> dict:
    client = authenticate_client(db, email, password, totp_code, ip)
    token, _, exp = create_token(client.id, CLIENT_AUD, get_settings().client_token_minutes,
                                 {"tv": client.token_version or 0})
    return {"access_token": token, "token_type": "bearer", "expires_at": exp}


# ---------------------------------------------------------------- password reset

RESET_AUD = "client-password-reset"


def _reset_fingerprint(client: Client) -> str:
    # Changes whenever the password changes, so a reset link works only once.
    from app.core.security import password_fingerprint

    return password_fingerprint(client.password_hash, "password-reset")


def request_password_reset(db: Session, email: str, ip: str | None) -> None:
    """Email a reset link. Always succeeds silently so emails can't be enumerated."""
    from app.services import notifications

    client = db.scalar(select(Client).where(Client.email == email.lower()))
    if client is None or client.status in (ClientStatus.suspended, ClientStatus.closed):
        return
    s = get_settings()
    token, _, _ = create_token(client.id, RESET_AUD, s.password_reset_minutes, {"fp": _reset_fingerprint(client)})
    link = f"{s.public_url.rstrip('/')}/portal/reset-password?token={token}"
    notifications.send_email(client.email, "Reset your password",
                             f"Hello {client.first_name},\n\nUse this link within {s.password_reset_minutes} minutes "
                             f"to set a new password:\n\n{link}\n\nIf you did not request this, ignore this email.")
    audit(db, client, "auth.password_reset_requested", "client", client.id, ip=ip)
    db.commit()


def reset_password(db: Session, token: str, new_password: str, ip: str | None) -> Client:
    """Signs the client out everywhere (token_version)."""
    from app.core.security import decode_token

    try:
        payload = decode_token(token, RESET_AUD)
    except Exception:
        raise ServiceError("This reset link is invalid or has expired")
    client = db.get(Client, int(payload["sub"]))
    if client is None or payload.get("fp") != _reset_fingerprint(client):
        raise ServiceError("This reset link is invalid or has already been used")
    validate_password_policy(new_password)
    client.password_hash = hash_password(new_password)
    client.failed_logins, client.locked_until = 0, None
    bump_token_version(client)
    audit(db, client, "auth.password_reset", "client", client.id, ip=ip)
    db.commit()
    return client


def revoke(db: Session, jti: str, expires_at: datetime) -> None:
    if not db.get(RevokedToken, jti):
        db.add(RevokedToken(jti=jti, expires_at=expires_at))
    db.execute(delete(RevokedToken).where(RevokedToken.expires_at < utcnow()))
    db.commit()


def is_revoked(db: Session, jti: str) -> bool:
    return db.get(RevokedToken, jti) is not None


# ---------------------------------------------------------------- 2FA


def begin_totp_enrollment(db: Session, principal) -> dict:
    """A new secret, stored encrypted; returned in clear once so it can be put in an authenticator app."""
    if principal.totp_enabled:
        raise ServiceError("2FA is already enabled")
    secret = new_totp_secret()
    principal.totp_secret, principal.totp_last_step = seal_totp_secret(secret), None
    db.commit()
    return {"secret": secret, "otpauth_uri": totp_uri(secret, principal.email)}


def confirm_totp_enrollment(db: Session, principal, code: str) -> None:
    if principal.totp_enabled:
        raise ServiceError("2FA is already enabled")
    if not _totp_ok(principal, code):
        raise ServiceError("Invalid 2FA code")
    principal.totp_enabled = True
    audit(db, principal, "auth.2fa_enabled", type(principal).__name__.lower(), principal.id)
    db.commit()


def disable_totp(db: Session, principal, code: str) -> None:
    """Signs the principal out everywhere (token_version)."""
    if not principal.totp_enabled or not _totp_ok(principal, code):
        raise ServiceError("Invalid 2FA code")
    principal.totp_enabled = False
    principal.totp_secret = principal.totp_last_step = None
    bump_token_version(principal)
    audit(db, principal, "auth.2fa_disabled", type(principal).__name__.lower(), principal.id)
    db.commit()


def change_password(db: Session, principal, current: str, new: str) -> None:
    """Signs the principal out everywhere (token_version); the web keeps the current session (refresh_fingerprint)."""
    if not verify_password(current, principal.password_hash):
        raise ServiceError("Current password is incorrect")
    validate_password_policy(new)
    principal.password_hash = hash_password(new)
    bump_token_version(principal)
    if isinstance(principal, Staff):
        principal.password_changed_at = utcnow()
    audit(db, principal, "auth.password_changed", type(principal).__name__.lower(), principal.id)
    db.commit()
