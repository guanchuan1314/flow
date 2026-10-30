"""Custom provider integrations, shared by payment methods and KYC methods.

Start (CRM -> provider), one of:
  * API: POST JSON to `api_url`. Headers: <signature_header>: hex HMAC-SHA256(body, secret) and, if an
    API key is set, "Authorization: Bearer <key>". The provider answers JSON containing the unique URL
    for this client at `url_field` (a dot path, e.g. "data.checkout_url").
  * Template: `redirect_template` with placeholders such as {reference}, {amount}, {currency}, {email},
    {callback_url}, {return_url} and {signature} (HMAC-SHA256 of the reference).

Result (provider -> CRM): POST to the method's callback URL, JSON or form-encoded, with the
signature header computed over the raw body. Fields are read by the configured names.

Outbound calls only go to https URLs (http is allowed outside production, for the local sandbox).
In production the host must resolve to public addresses only, and redirects are never followed.
The provider's answer is logged (truncated), never shown to users.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import string
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, quote, urlsplit

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import Forbidden, ServiceError
from app.core.security import decrypt_secret
from app.db import utcnow
from app.models import ProviderCallback
from app.models.integrations import ProviderSettings

log = logging.getLogger(__name__)
TIMEOUT_SECONDS = 15
# Starting values of a new method (column defaults only apply on INSERT, validation runs before).
PROVIDER_DEFAULTS = dict(
    kind="manual", description="", instructions="", api_url="", redirect_template="", url_field="url",
    api_key_enc="", secret_enc="", signature_header="X-Signature", ref_field="reference", status_field="status",
    success_values="success,paid,completed,approved", failure_values="failed,cancelled,declined,expired,rejected",
    reason_field="reason")


def new_token() -> str:
    return secrets.token_urlsafe(24)


def callback_url(scope: str, token: str) -> str:
    return f"{get_settings().public_url.rstrip('/')}/callbacks/{scope}/{token}"


def sign(secret: str, data: bytes) -> str:
    return hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()


def _secret(method: ProviderSettings) -> str:
    return decrypt_secret(method.secret_enc) if method.secret_enc else ""


def field(data: dict, path: str):
    """Value at a dot path ("data.status"), or None."""
    value = data
    for part in (path or "").split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def require_perm(db: Session, actor, perm: str, what: str) -> None:
    """Staff making a security-sensitive change to a method need `perm` as well (None = system)."""
    from app.models import Staff

    if isinstance(actor, Staff) and not actor.has_perm(perm):
        db.rollback()
        raise Forbidden(f"{what} also needs the {perm} permission")


def check_url(url: str, resolve: bool | None = None) -> None:
    """Refuse provider URLs the CRM must not call: other schemes, and (in production) hosts that
    resolve to private, loopback, link-local, multicast or reserved addresses."""
    prod = get_settings().is_prod
    parts = urlsplit(url)
    allowed = ("https",) if prod else ("https", "http")
    if parts.scheme not in allowed or not parts.hostname:
        raise ServiceError("The provider API URL must be an https:// URL")
    if not (prod if resolve is None else resolve):
        return
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        raise ServiceError(f"The provider host {parts.hostname} could not be resolved", 502) from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise ServiceError(f"The provider host {parts.hostname} is not a public address")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a 3xx is returned as is (and refused by the caller)


_opener = urllib.request.build_opener(_NoRedirect)


def http_post(url: str, body: bytes, headers: dict) -> tuple[int, str]:
    """POST and return (status, text). Replaced in tests."""
    check_url(url)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _opener.open(req, timeout=TIMEOUT_SECONDS) as resp:  # noqa: S310 - checked by check_url
            return resp.status, resp.read(1_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(1_000_000).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("provider %s unreachable: %s", url, exc)
        raise ServiceError("Could not reach the provider", 502) from exc


class _Blank(dict):
    def __missing__(self, key):
        return ""


def start(method: ProviderSettings, payload: dict) -> str:
    """The unique URL to send the client to. `payload` must contain "reference"."""
    secret = _secret(method)
    if method.api_url:
        body = json.dumps(payload, default=str, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if secret:
            headers[method.signature_header or "X-Signature"] = sign(secret, body)
        if method.api_key_enc:
            headers["Authorization"] = f"Bearer {decrypt_secret(method.api_key_enc)}"
        check_url(method.api_url)
        status, text = http_post(method.api_url, body, headers)
        if status >= 300:
            log.warning("provider '%s' refused the request (HTTP %s): %.500s", method.name, status, text)
            raise ServiceError(f"The provider refused the request (HTTP {status})", 502)
        try:
            url = field(json.loads(text), method.url_field or "url")
        except ValueError:
            url = None
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            log.warning("provider '%s' answer has no URL at '%s': %.500s", method.name, method.url_field, text)
            raise ServiceError(f"The provider's answer has no URL at '{method.url_field}'", 502)
        return url
    if method.redirect_template:
        values = _Blank({k: quote(str(v), safe="") for k, v in payload.items()})
        values["signature"] = sign(secret, str(payload["reference"]).encode()) if secret else ""
        return string.Formatter().vformat(method.redirect_template, (), values)
    raise ServiceError(f"'{method.name}' has neither a provider API URL nor a redirect URL template")


def parse_body(raw: bytes, content_type: str) -> dict:
    if "json" in (content_type or "") or raw.lstrip().startswith(b"{"):
        try:
            data = json.loads(raw or b"{}")
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}
    return dict(parse_qsl(raw.decode("utf-8", "replace")))


def signature_ok(method: ProviderSettings, raw: bytes, headers) -> bool:
    secret = _secret(method)
    sent = (headers.get(method.signature_header or "X-Signature") or "").strip()
    if not secret or not sent:
        return False
    sent = sent.split("=", 1)[1] if sent.lower().startswith("sha256=") else sent
    return hmac.compare_digest(sent.lower(), sign(secret, raw))


def _values(csv: str) -> set[str]:
    return {v.strip().lower() for v in (csv or "").split(",") if v.strip()}


def outcome(method: ProviderSettings, data: dict) -> tuple[str, str | None]:
    """("success" | "failure" | "pending", reason)."""
    status = str(field(data, method.status_field) or "").strip().lower()
    reason = field(data, method.reason_field)
    if status in _values(method.success_values):
        return "success", None
    if status in _values(method.failure_values):
        return "failure", str(reason or status)[:255]
    return "pending", None


def log_callback(db: Session, scope: str, method_id: int | None, reference: str | None, ip: str | None,
                 ok: bool, result: str, raw: bytes, data: dict) -> None:
    db.add(ProviderCallback(created_at=utcnow(), scope=scope, method_id=method_id,
                            reference=(reference or None) and str(reference)[:64], ip=ip, signature_ok=ok,
                            outcome=result[:255], body=raw.decode("utf-8", "replace")[:8000],
                            data=data if len(json.dumps(data, default=str)) < 8000 else {}))
