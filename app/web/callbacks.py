"""Provider callbacks (payment and KYC). Public, no session or CSRF: each request is authenticated by
the unguessable per-method URL token and the HMAC signature of its body."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.deps import client_ip
from app.db import get_db
from app.services import kyc_methods, payment_methods

router = APIRouter(prefix="/callbacks", include_in_schema=False)
MAX_BODY = 64 * 1024


async def _handle(handler, token: str, request: Request, db: Session) -> JSONResponse:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return JSONResponse({"result": "body too large"}, status_code=413)
    ip = client_ip(request)
    code, message = await run_in_threadpool(handler, db, token, raw, request.headers,
                                            request.headers.get("content-type", ""), ip)
    return JSONResponse({"result": message}, status_code=code)


@router.post("/payment/{token}")
async def payment_callback(token: str, request: Request, db: Session = Depends(get_db)):
    return await _handle(payment_methods.handle_callback, token, request, db)


@router.post("/kyc/{token}")
async def kyc_callback(token: str, request: Request, db: Session = Depends(get_db)):
    return await _handle(kyc_methods.handle_callback, token, request, db)
