"""Payment methods and KYC methods (manual or custom provider)."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ServiceError, get_or_404
from app.db import get_db
from app.models import CUSTOM, MANUAL, KycMethod, PaymentMethod, ProviderCallback, Staff
from app.services import integrations, kyc_methods, payment_methods
from app.web.common import attempt, opt_dec, opt_int, redirect, render, staff_perm

router = APIRouter()

# Form fields shared by both kinds of method.
PROVIDER_FIELDS = ("name", "kind", "description", "instructions", "api_url", "redirect_template", "url_field",
                   "signature_header", "ref_field", "status_field", "success_values", "failure_values", "reason_field")


async def _form(request: Request) -> dict:
    form = await request.form()
    return {k: (v.strip() if isinstance(v, str) else v) for k, v in form.multi_items() if k != "_csrf"} | {
        "_lists": {k: form.getlist(k) for k in ("required_documents",)}}


def _provider_fields(f: dict) -> dict:
    out = {k: f.get(k, "") for k in PROVIDER_FIELDS}
    if out["kind"] not in (MANUAL, CUSTOM):
        raise ServiceError("Choose manual or custom")
    if not out["name"]:
        raise ServiceError("Name is required")
    for k, default in (("url_field", "url"), ("signature_header", "X-Signature"), ("ref_field", "reference"),
                       ("status_field", "status"), ("reason_field", "reason")):
        out[k] = out[k] or default
    if f.get("secret"):
        out["secret"] = f["secret"]
    if f.get("api_key"):
        out["api_key"] = f["api_key"]
    return out


def _callbacks(db: Session, scope: str, method_id: int):
    return db.scalars(select(ProviderCallback).where(ProviderCallback.scope == scope,
                                                     ProviderCallback.method_id == method_id)
                      .order_by(ProviderCallback.id.desc()).limit(20)).all()


# ---------------------------------------------------------------- payment methods
@router.get("/payment-methods")
def pm_list(request: Request, me: Staff = Depends(staff_perm("finance.methods")), db: Session = Depends(get_db)):
    methods = db.scalars(select(PaymentMethod).order_by(PaymentMethod.sort_order, PaymentMethod.name)).all()
    return render(request, "admin/methods/payment_list.html", methods=methods)


@router.get("/payment-methods/new")
def pm_new(request: Request, me: Staff = Depends(staff_perm("finance.methods"))):
    return render(request, "admin/methods/form.html", m=None, scope="payment", base="/admin/payment-methods")


@router.get("/payment-methods/{method_id}")
def pm_edit(request: Request, method_id: int, me: Staff = Depends(staff_perm("finance.methods")),
            db: Session = Depends(get_db)):
    m = get_or_404(db, PaymentMethod, method_id)
    return render(request, "admin/methods/form.html", m=m, scope="payment", base="/admin/payment-methods",
                  callback_url=integrations.callback_url("payment", m.callback_token),
                  callbacks=_callbacks(db, "payment", m.id))


def _payment_fields(f: dict) -> dict:
    out = _provider_fields(f)
    out.update(enabled=f.get("enabled") == "on", allow_deposit=f.get("allow_deposit") == "on",
               allow_withdrawal=f.get("allow_withdrawal") == "on", currency=(f.get("currency") or "USD").upper()[:3],
               min_amount=opt_dec(f.get("min_amount")), max_amount=opt_dec(f.get("max_amount")),
               requires_proof=f.get("requires_proof") == "on", auto_credit=f.get("auto_credit") == "on",
               amount_field=f.get("amount_field", ""), icon=(f.get("icon") or "bank")[:32],
               sort_order=opt_int(f.get("sort_order")) or 100)
    return out


@router.post("/payment-methods/new")
async def pm_create(request: Request, me: Staff = Depends(staff_perm("finance.methods")), db: Session = Depends(get_db)):
    f = await _form(request)
    created = {}

    def run():
        created["m"] = payment_methods.create_method(db, me, **_payment_fields(f))
    resp = attempt(request, run, "Payment method created.", "/admin/payment-methods")
    return redirect(f"/admin/payment-methods/{created['m'].id}") if "m" in created else resp


@router.post("/payment-methods/{method_id}")
async def pm_update(request: Request, method_id: int, me: Staff = Depends(staff_perm("finance.methods")),
                    db: Session = Depends(get_db)):
    m = get_or_404(db, PaymentMethod, method_id)
    f = await _form(request)
    return attempt(request, lambda: payment_methods.update_method(db, me, m, _payment_fields(f)),
                   "Payment method saved.", f"/admin/payment-methods/{method_id}")


@router.post("/payment-methods/{method_id}/callback-url")
def pm_new_callback(request: Request, method_id: int, me: Staff = Depends(staff_perm("finance.methods")),
                    db: Session = Depends(get_db)):
    m = get_or_404(db, PaymentMethod, method_id)
    return attempt(request, lambda: payment_methods.new_callback_url(db, me, m),
                   "New callback URL generated. Give it to the provider; the old one no longer works.",
                   f"/admin/payment-methods/{method_id}")


@router.post("/payment-methods/{method_id}/delete")
def pm_delete(request: Request, method_id: int, me: Staff = Depends(staff_perm("finance.methods")),
              db: Session = Depends(get_db)):
    m = get_or_404(db, PaymentMethod, method_id)
    return attempt(request, lambda: payment_methods.delete_method(db, me, m), "Payment method deleted.",
                   "/admin/payment-methods")


# ---------------------------------------------------------------- KYC methods
@router.get("/kyc-methods")
def kyc_list(request: Request, me: Staff = Depends(staff_perm("kyc.methods")), db: Session = Depends(get_db)):
    methods = db.scalars(select(KycMethod).order_by(KycMethod.active.desc(), KycMethod.name)).all()
    return render(request, "admin/methods/kyc_list.html", methods=methods, categories=kyc_methods.CATEGORIES)


@router.get("/kyc-methods/new")
def kyc_new(request: Request, me: Staff = Depends(staff_perm("kyc.methods"))):
    return render(request, "admin/methods/form.html", m=None, scope="kyc", base="/admin/kyc-methods",
                  categories=kyc_methods.CATEGORIES)


@router.get("/kyc-methods/{method_id}")
def kyc_edit(request: Request, method_id: int, me: Staff = Depends(staff_perm("kyc.methods")),
             db: Session = Depends(get_db)):
    m = get_or_404(db, KycMethod, method_id)
    return render(request, "admin/methods/form.html", m=m, scope="kyc", base="/admin/kyc-methods",
                  categories=kyc_methods.CATEGORIES, callback_url=integrations.callback_url("kyc", m.callback_token),
                  callbacks=_callbacks(db, "kyc", m.id))


def _kyc_fields(f: dict) -> dict:
    return _provider_fields(f) | {"required_documents": f["_lists"]["required_documents"]}


@router.post("/kyc-methods/new")
async def kyc_create(request: Request, me: Staff = Depends(staff_perm("kyc.methods")), db: Session = Depends(get_db)):
    f = await _form(request)
    created = {}

    def run():
        created["m"] = kyc_methods.create_method(db, me, **_kyc_fields(f))
    resp = attempt(request, run, "KYC method created. Activate it to use it for clients.", "/admin/kyc-methods")
    return redirect(f"/admin/kyc-methods/{created['m'].id}") if "m" in created else resp


@router.post("/kyc-methods/{method_id}")
async def kyc_update(request: Request, method_id: int, me: Staff = Depends(staff_perm("kyc.methods")),
                     db: Session = Depends(get_db)):
    m = get_or_404(db, KycMethod, method_id)
    f = await _form(request)
    return attempt(request, lambda: kyc_methods.update_method(db, me, m, _kyc_fields(f)), "KYC method saved.",
                   f"/admin/kyc-methods/{method_id}")


@router.post("/kyc-methods/{method_id}/activate")
def kyc_activate(request: Request, method_id: int, me: Staff = Depends(staff_perm("kyc.methods")),
                 db: Session = Depends(get_db)):
    m = get_or_404(db, KycMethod, method_id)
    return attempt(request, lambda: kyc_methods.activate(db, me, m),
                   f"{m.name} is now the active KYC method; all others are off.", "/admin/kyc-methods")


@router.post("/kyc-methods/{method_id}/callback-url")
def kyc_new_callback(request: Request, method_id: int, me: Staff = Depends(staff_perm("kyc.methods")),
                     db: Session = Depends(get_db)):
    m = get_or_404(db, KycMethod, method_id)
    return attempt(request, lambda: kyc_methods.new_callback_url(db, me, m),
                   "New callback URL generated. Give it to the provider; the old one no longer works.",
                   f"/admin/kyc-methods/{method_id}")


@router.post("/kyc-methods/{method_id}/delete")
def kyc_delete(request: Request, method_id: int, me: Staff = Depends(staff_perm("kyc.methods")),
               db: Session = Depends(get_db)):
    m = get_or_404(db, KycMethod, method_id)
    return attempt(request, lambda: kyc_methods.delete_method(db, me, m), "KYC method deleted.", "/admin/kyc-methods")
