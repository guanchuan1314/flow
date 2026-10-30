import hmac
import ipaddress
import re
import secrets
import string
import time
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
import pyotp

from app.config import get_settings
from app.core.errors import ServiceError

ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def validate_password_policy(password: str) -> None:
    min_len = get_settings().password_min_length
    problems = []
    if len(password) < min_len:
        problems.append(f"at least {min_len} characters")
    if not re.search(r"[A-Z]", password):
        problems.append("an uppercase letter")
    if not re.search(r"[a-z]", password):
        problems.append("a lowercase letter")
    if not re.search(r"\d", password):
        problems.append("a digit")
    if problems:
        raise ServiceError("Password must contain " + ", ".join(problems))


def generate_mt5_password(length: int = 12) -> str:
    """MT5 requires upper, lower, digit and special characters."""
    specials = "#@!$%"
    alphabet = string.ascii_letters + string.digits + specials
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pwd) and any(c.isupper() for c in pwd)
                and any(c.isdigit() for c in pwd) and any(c in specials for c in pwd)):
            return pwd


# ---------------------------------------------------------------- JWT


def create_token(subject: int, audience: str, minutes: int, extra: dict | None = None) -> tuple[str, str, datetime]:
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=minutes)
    jti = uuid.uuid4().hex
    payload = {"sub": str(subject), "aud": audience, "iat": now, "exp": exp, "jti": jti, **(extra or {})}
    token = jwt.encode(payload, get_settings().secret_key, algorithm=ALGORITHM)
    return token, jti, exp.replace(tzinfo=None)


def decode_token(token: str, audience: str) -> dict:
    return jwt.decode(token, get_settings().secret_key, algorithms=[ALGORITHM], audience=audience)


# ---------------------------------------------------------------- TOTP


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, email: str, issuer: str = "Flow") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def verify_totp(secret: str | None, code: str | None) -> bool:
    if not secret or not code:
        return False
    return pyotp.TOTP(secret).verify(code.strip(), valid_window=1)


def accepted_totp_step(secret: str | None, code: str | None, last_step: int | None) -> int | None:
    """The 30-second time step the code belongs to (one step of clock drift either way), or None.
    Steps at or before `last_step` are refused, so a code can't be replayed."""
    if not secret or not code:
        return None
    totp, code, now = pyotp.TOTP(secret), code.strip(), int(time.time())
    for offset in (-1, 0, 1):
        at = now + offset * totp.interval
        step = at // totp.interval
        if (last_step is None or step > last_step) and hmac.compare_digest(totp.at(at), code):
            return step
    return None


def seal_totp_secret(secret: str | None) -> str | None:
    """A TOTP secret as stored in the database (encrypted, like MT5 manager passwords)."""
    return encrypt_secret(secret) if secret else None


def open_totp_secret(stored: str | None) -> str | None:
    """The plain TOTP secret. Secrets saved before encryption (base32 text) are returned as they are."""
    if not stored:
        return None
    if not stored.startswith("gAAAA"):  # legacy plaintext; Fernet tokens always start like this
        return stored
    try:
        return decrypt_secret(stored)
    except ServiceError:
        raise ServiceError("Stored 2FA secret cannot be decrypted (CREDENTIALS_KEY/SECRET_KEY changed). "
                           "Ask an administrator to reset 2FA.")


# ---------------------------------------------------------------- IP allowlist


def ip_allowed(ip: str | None, allowlist: list[str] | None) -> bool:
    if not allowlist:
        return True
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in allowlist:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def password_fingerprint(password_hash: str | None, purpose: str) -> str:
    """Opaque value that changes whenever the password changes. Safe to put in cookies and
    tokens: it is an HMAC under the server secret, so it reveals nothing about the hash."""
    import hashlib

    key = get_settings().secret_key.encode()
    return hmac.new(key, f"{purpose}:{password_hash or ''}".encode(), hashlib.sha256).hexdigest()[:24]


# ---------------------------------------------------------------- secrets at rest


def _fernet():
    import base64
    import hashlib

    from cryptography.fernet import Fernet

    s = get_settings()
    material = s.credentials_key or s.secret_key
    key = base64.urlsafe_b64encode(hashlib.sha256(f"mt5-credentials:{material}".encode()).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt_secret(token: str) -> str:
    """Raises ServiceError if the key changed since the value was stored."""
    from cryptography.fernet import InvalidToken

    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise ServiceError("Stored MT5 password cannot be decrypted (CREDENTIALS_KEY/SECRET_KEY changed). "
                           "Re-enter the manager password.")


def new_referral_code() -> str:
    return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
