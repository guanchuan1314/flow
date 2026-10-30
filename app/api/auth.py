from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.ratelimit import check_auth_rate
from app.core.deps import client_ip, current_client, current_staff, current_staff_no_mfa, token_info
from app.db import get_db
from app.models import Client, Staff
from app.schemas import (
    ClientOut, ClientRegisterIn, ForgotPasswordIn, LoginIn, ResetPasswordIn, PasswordChangeIn, StaffOut, TokenOut, TotpCodeIn, TotpEnrollOut,
)
from app.services import auth, clients

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------- staff
@router.post("/staff/login", response_model=TokenOut)
def staff_login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    check_auth_rate("staff-login", client_ip(request))
    return auth.staff_login(db, body.email, body.password, body.totp_code, client_ip(request))


@router.post("/staff/logout", status_code=204)
def staff_logout(request: Request, staff: Staff = Depends(current_staff_no_mfa), db: Session = Depends(get_db)):
    auth.revoke(db, *token_info(request))


@router.get("/staff/me", response_model=StaffOut)
def staff_me(staff: Staff = Depends(current_staff_no_mfa)):
    return staff


@router.post("/staff/2fa/enroll", response_model=TotpEnrollOut)
def staff_2fa_enroll(staff: Staff = Depends(current_staff_no_mfa), db: Session = Depends(get_db)):
    return auth.begin_totp_enrollment(db, staff)


@router.post("/staff/2fa/confirm", status_code=204)
def staff_2fa_confirm(body: TotpCodeIn, staff: Staff = Depends(current_staff_no_mfa), db: Session = Depends(get_db)):
    auth.confirm_totp_enrollment(db, staff, body.code)


@router.post("/staff/2fa/disable", status_code=204)
def staff_2fa_disable(body: TotpCodeIn, staff: Staff = Depends(current_staff), db: Session = Depends(get_db)):
    auth.disable_totp(db, staff, body.code)


@router.post("/staff/password", status_code=204)
def staff_password(body: PasswordChangeIn, staff: Staff = Depends(current_staff_no_mfa), db: Session = Depends(get_db)):
    auth.change_password(db, staff, body.current_password, body.new_password)


# ---------------------------------------------------------------- clients
@router.post("/client/register", response_model=ClientOut, status_code=201)
def client_register(body: ClientRegisterIn, request: Request, db: Session = Depends(get_db)):
    check_auth_rate("register", client_ip(request))
    return clients.register_client(db, None, ip=client_ip(request), **body.model_dump())


@router.post("/client/login", response_model=TokenOut)
def client_login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    check_auth_rate("client-login", client_ip(request))
    return auth.client_login(db, body.email, body.password, body.totp_code, client_ip(request))


@router.post("/client/logout", status_code=204)
def client_logout(request: Request, client: Client = Depends(current_client), db: Session = Depends(get_db)):
    auth.revoke(db, *token_info(request))


@router.post("/client/2fa/enroll", response_model=TotpEnrollOut)
def client_2fa_enroll(client: Client = Depends(current_client), db: Session = Depends(get_db)):
    return auth.begin_totp_enrollment(db, client)


@router.post("/client/2fa/confirm", status_code=204)
def client_2fa_confirm(body: TotpCodeIn, client: Client = Depends(current_client), db: Session = Depends(get_db)):
    auth.confirm_totp_enrollment(db, client, body.code)


@router.post("/client/2fa/disable", status_code=204)
def client_2fa_disable(body: TotpCodeIn, client: Client = Depends(current_client), db: Session = Depends(get_db)):
    auth.disable_totp(db, client, body.code)


@router.post("/client/password", status_code=204)
def client_password(body: PasswordChangeIn, client: Client = Depends(current_client), db: Session = Depends(get_db)):
    auth.change_password(db, client, body.current_password, body.new_password)


@router.post("/client/forgot-password", status_code=202)
def client_forgot_password(body: ForgotPasswordIn, request: Request, db: Session = Depends(get_db)):
    """Always returns 202 so the endpoint can't be used to discover registered emails."""
    check_auth_rate("forgot", client_ip(request))
    auth.request_password_reset(db, body.email, client_ip(request))


@router.post("/client/reset-password", status_code=204)
def client_reset_password(body: ResetPasswordIn, request: Request, db: Session = Depends(get_db)):
    check_auth_rate("reset", client_ip(request))
    auth.reset_password(db, body.token, body.new_password, client_ip(request))
