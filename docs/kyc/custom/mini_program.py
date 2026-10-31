"""Sample mini program for a custom KYC method in Flow.

It sits between Flow and your identity verification provider:

  1. Flow   -> POST /start           (signed)   we answer {"url": <unique verification link>}
  2. Client -> that link                         provider.py sends them to your provider
  3. Provider tells us the result (webhook / return page), we POST it to Flow's callback_url (signed)
     and send the client back to Flow's return_url.

Only the standard library is used:  python mini_program.py
Settings (environment variables):
  FLOW_SECRET   the "Shared secret" of the KYC method in Flow (required)
  FLOW_API_KEY  the "API key" of the method in Flow, if you set one (optional)
  FLOW_URL      your Flow address, e.g. https://portal.yourbroker.com; callbacks only go there
  PUBLIC_URL    the address of this mini program as clients reach it, e.g. https://kyc.yourbroker.com
  PORT          default 9002
"""

import hashlib
import hmac
import json
import os
import secrets
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import provider

FLOW_SECRET = os.environ.get("FLOW_SECRET", "")
FLOW_API_KEY = os.environ.get("FLOW_API_KEY", "")
FLOW_URL = os.environ.get("FLOW_URL", "http://127.0.0.1:8000").rstrip("/")
PORT = int(os.environ.get("PORT", "9002"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", f"http://127.0.0.1:{PORT}").rstrip("/")
SIGNATURE_HEADER = "X-Signature"  # the "Signature header" of the method in Flow
SESSIONS_FILE = Path(__file__).with_name("sessions.json")
REQUIRED = ("reference", "client_id", "callback_url", "return_url")

_lock = threading.Lock()


# ---------------------------------------------------------------- signatures (both directions)
def sign(body: bytes) -> str:
    return hmac.new(FLOW_SECRET.encode(), body, hashlib.sha256).hexdigest()


def signature_ok(body: bytes, sent: str | None) -> bool:
    return bool(FLOW_SECRET and sent) and hmac.compare_digest(sent.strip().lower(), sign(body))


# ---------------------------------------------------------------- sessions (one per verification)
def load_sessions() -> dict:
    return json.loads(SESSIONS_FILE.read_text("utf-8")) if SESSIONS_FILE.exists() else {}


def save_session(session: dict) -> None:
    with _lock:
        sessions = load_sessions()
        sessions[session["id"]] = session
        SESSIONS_FILE.write_text(json.dumps(sessions, indent=1), "utf-8")


def get_session(sid: str) -> dict | None:
    return load_sessions().get(sid)


# ---------------------------------------------------------------- result -> Flow
def report_to_flow(sid: str, status: str, reason: str = "") -> bool:
    """Send the verification result to Flow. status: "approved", "rejected" or "pending".
    "approved" verifies the client in Flow; "rejected" asks them to try again (with `reason`).
    Safe to call more than once: Flow ignores results once the verification is decided."""
    session = get_session(sid)
    if session is None:
        return False
    body = {"reference": session["reference"], "status": status, "reason": reason}
    raw = json.dumps(body, separators=(",", ":")).encode()
    req = urllib.request.Request(session["callback_url"], data=raw, method="POST", headers={
        "Content-Type": "application/json", SIGNATURE_HEADER: sign(raw)})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - checked in /start
            answer = resp.read().decode("utf-8", "replace")
            ok = True
    except urllib.error.HTTPError as exc:
        answer, ok = exc.read().decode("utf-8", "replace"), False
    except OSError as exc:
        answer, ok = str(exc), False
    print(f"[flow] {session['reference']} {status} -> {answer}")
    session.setdefault("results", []).append({"status": status, "flow_answer": answer[:300], "delivered": ok})
    if ok and status != "pending":
        session["state"] = status
    save_session(session)
    return ok


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body, content_type="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, url: str):
        self.send_response(303)
        self.send_header("Location", url)
        self.end_headers()

    def _body(self) -> bytes:
        return self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 64 * 1024))

    def do_POST(self):  # noqa: N802
        path = urlsplit(self.path).path
        raw = self._body()
        if path == "/start":
            return self.start(raw)
        if path.startswith("/verify/"):  # the demo provider's buttons
            sid = path.rsplit("/", 1)[-1]
            return self.after_provider(sid, *provider.demo_decision(dict(parse_qsl(raw.decode()))))
        if path == "/provider/webhook":  # your real provider's server-to-server notification
            result = provider.parse_webhook(self.headers, raw)
            if result is None:
                return self._send(400, {"error": "rejected"})
            sid, status, reason = result
            report_to_flow(sid, status, reason)
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def do_GET(self):  # noqa: N802
        path = urlsplit(self.path).path
        if path.startswith("/verify/"):
            session = get_session(path.rsplit("/", 1)[-1])
            if session is None:
                return self._send(404, "Unknown or expired verification", "text/plain")
            return self._send(200, provider.demo_page(session), "text/html; charset=utf-8")
        if path.startswith("/return/"):  # where your provider sends the client back
            session = get_session(path.rsplit("/", 1)[-1])
            return self._redirect(session["return_url"] if session else FLOW_URL)
        if path == "/health":
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def start(self, raw: bytes):
        """Step 1: Flow asks for a unique verification link."""
        if not signature_ok(raw, self.headers.get(SIGNATURE_HEADER)):
            return self._send(401, {"error": "invalid signature"})
        if FLOW_API_KEY and self.headers.get("Authorization") != f"Bearer {FLOW_API_KEY}":
            return self._send(401, {"error": "invalid API key"})
        try:
            payload = json.loads(raw)
        except ValueError:
            return self._send(400, {"error": "invalid JSON"})
        missing = [k for k in REQUIRED if not payload.get(k)]
        if missing:
            return self._send(400, {"error": f"missing {', '.join(missing)}"})
        if not payload["callback_url"].startswith(FLOW_URL + "/callbacks/"):
            return self._send(400, {"error": "callback_url is not on FLOW_URL"})
        session = {"id": secrets.token_urlsafe(16), "state": "started", **payload}
        save_session(session)
        url = provider.create_verification(session, PUBLIC_URL)  # step 2: the link the client is sent to
        print(f"[start] {payload['reference']} client {payload['client_id']} -> {url}")
        self._send(200, {"url": url})

    def after_provider(self, sid: str, status: str, reason: str):
        """Step 3 (demo provider): report the result to Flow, then send the client back to Flow."""
        session = get_session(sid)
        if session is None:
            return self._send(404, "Unknown or expired verification", "text/plain")
        if session["state"] == "started":
            report_to_flow(sid, status, reason)
        self._redirect(session["return_url"])

    def log_message(self, fmt, *args):
        print("[http]", fmt % args)


if __name__ == "__main__":
    if not FLOW_SECRET:
        raise SystemExit("Set FLOW_SECRET to the method's shared secret in Flow")
    print(f"KYC mini program on {PUBLIC_URL} (Flow: {FLOW_URL})")
    print(f"In Flow, set the method's Provider API URL to {PUBLIC_URL}/start")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
