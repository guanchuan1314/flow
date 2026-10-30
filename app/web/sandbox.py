"""A fake payment / KYC provider for local demos and tests (never mounted in production).

It implements the provider side of services.integrations: POST /sandbox/provider/start returns a
unique page URL; on that page you choose "approve" or "decline" and the sandbox sends the signed
callback to the CRM, then returns you to the CRM, exactly like a real provider would.
"""

import json
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.security import decrypt_secret
from app.db import get_db
from app.models import KycMethod, PaymentMethod
from app.services import integrations
from app.web.common import render

router = APIRouter(prefix="/sandbox/provider", include_in_schema=False)
_sessions: dict[str, dict] = {}  # in memory: a demo provider


def _method(db: Session, callback_url: str):
    token = callback_url.rstrip("/").rsplit("/", 1)[-1]
    for model in (PaymentMethod, KycMethod):
        m = db.scalar(select(model).where(model.callback_token == token))
        if m is not None:
            return m
    return None


@router.post("/start")
async def start(request: Request, db: Session = Depends(get_db)):
    raw = await request.body()
    payload = json.loads(raw or b"{}")
    m = _method(db, payload.get("callback_url", ""))
    if m is None or not integrations.signature_ok(m, raw, request.headers):
        return JSONResponse({"error": "invalid signature"}, status_code=401)
    sid = secrets.token_urlsafe(12)
    _sessions[sid] = payload
    return {"url": f"{get_settings().public_url.rstrip('/')}/sandbox/provider/page/{sid}", "id": sid}


@router.get("/page/{sid}", response_class=HTMLResponse)
def page(request: Request, sid: str):
    payload = _sessions.get(sid)
    if payload is None:
        return HTMLResponse("Unknown or expired session", status_code=404)
    return render(request, "sandbox.html", sid=sid, p=payload, kyc="amount" not in payload)


@router.post("/page/{sid}")
def decide(sid: str, decision: str = Form(...), db: Session = Depends(get_db)):
    payload = _sessions.pop(sid, None)
    if payload is None:
        return HTMLResponse("Unknown or expired session", status_code=404)
    m = _method(db, payload["callback_url"])
    ok = decision == "approve"
    body = {"reference": payload["reference"], "status": "approved" if ok else "declined",
            "reason": None if ok else "Declined in the sandbox", "transaction_id": f"SBX-{sid[:8]}"}
    if "amount" in payload:
        body["status"] = "paid" if ok else "failed"
        body["amount"] = payload["amount"]
    raw = json.dumps(body).encode()
    integrations.http_post(payload["callback_url"], raw, {
        "Content-Type": "application/json",
        m.signature_header: integrations.sign(decrypt_secret(m.secret_enc), raw)})
    return RedirectResponse(payload.get("return_url") or "/", status_code=303)
