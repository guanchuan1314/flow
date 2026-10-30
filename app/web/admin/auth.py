from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from app.core.errors import ServiceError
from app.core.security import totp_uri
from app.db import get_db
from app.models import Staff
from app.services import auth, branding
from app.web import login_flow
from app.web.common import (
    STAFF_KEY, attempt, flash, logout_session, qr_data_uri, redirect, refresh_fingerprint, render, safe_next,
    web_staff,
)

router = APIRouter()


@router.get("/login")
def login_page(request: Request, next: str | None = None):
    if request.session.get(f"{STAFF_KEY}_id"):
        return redirect(safe_next(next, "/admin"))
    return render(request, "admin/login.html", next=next)


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str | None = Form(None),
          db: Session = Depends(get_db)):
    try:
        _, needs_totp = login_flow.password_step(request, db, STAFF_KEY, auth.authenticate_staff, email, password)
    except ServiceError as exc:
        return render(request, "admin/login.html", status_code=_status(exc), error=exc.message, email=email, next=next)
    if needs_totp:
        return render(request, "admin/login.html", need_totp=True, next=next)
    return redirect(safe_next(next, "/admin"))


@router.post("/login/2fa")
def login_2fa(request: Request, totp_code: str = Form(...), next: str | None = Form(None), db: Session = Depends(get_db)):
    try:
        login_flow.totp_step(request, db, STAFF_KEY, Staff, totp_code)
    except ServiceError as exc:
        if login_flow.has_pending(request, STAFF_KEY):
            return render(request, "admin/login.html", status_code=_status(exc), need_totp=True, error=exc.message, next=next)
        return render(request, "admin/login.html", status_code=_status(exc), error=exc.message, next=next)
    return redirect(safe_next(next, "/admin"))


def _status(exc: ServiceError) -> int:
    return 403 if exc.status_code in (403, 423) else 429 if exc.status_code == 429 else 401


@router.post("/logout")
def logout(request: Request):
    logout_session(request, STAFF_KEY)
    flash(request, "You have been signed out.", "info")
    return redirect("/admin/login")


@router.get("/me/security")
def security_page(request: Request, staff: Staff = Depends(web_staff)):
    return render(request, "admin/security.html")


@router.post("/me/password")
def change_password(request: Request, current_password: str = Form(...), new_password: str = Form(...),
                    staff: Staff = Depends(web_staff), db: Session = Depends(get_db)):
    def run():
        auth.change_password(db, staff, current_password, new_password)
        refresh_fingerprint(request, STAFF_KEY, staff)
    return attempt(request, run, "Password updated.", "/admin/me/security")


@router.post("/me/2fa/start")
def start_2fa(request: Request, staff: Staff = Depends(web_staff), db: Session = Depends(get_db)):
    try:
        data = auth.begin_totp_enrollment(db, staff)
    except ServiceError as exc:
        flash(request, exc.message, "danger")
        return redirect("/admin/me/security")
    uri = totp_uri(data["secret"], staff.email, issuer=f"{branding.current().app_name} Admin")
    return render(request, "admin/security.html", enroll={"secret": data["secret"], "qr": qr_data_uri(uri)})


@router.post("/me/2fa/confirm")
def confirm_2fa(request: Request, code: str = Form(...), staff: Staff = Depends(web_staff), db: Session = Depends(get_db)):
    return attempt(request, lambda: auth.confirm_totp_enrollment(db, staff, code),
                   "Two-factor authentication is now on.", "/admin/me/security")


@router.post("/me/2fa/disable")
def disable_2fa(request: Request, code: str = Form(...), staff: Staff = Depends(web_staff), db: Session = Depends(get_db)):
    def run():
        auth.disable_totp(db, staff, code)
        refresh_fingerprint(request, STAFF_KEY, staff)  # other sessions end; this one stays
    return attempt(request, run, "Two-factor authentication disabled.", "/admin/me/security")
