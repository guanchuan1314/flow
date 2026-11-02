from datetime import date

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import ServiceError, get_or_404
from app.db import get_db
from app.models import (
    AccountGroup, AuditLog, Client, ClientNote, ClientStatus, IBProfile, KycDocument, KycStatus, PaymentRequest, ReferralLink, Staff,
    TradingAccount, WalletTransaction,
)
from app.services import clients as svc
from app.services import trader_styles, wallet
from app.web.common import Page, attempt, opt_enum, opt_int, redirect, render, staff_perm

router = APIRouter()

TABS = ("overview", "accounts", "kyc", "finance", "notes", "activity")


def _staff_options(db: Session):
    return [(s.id, s.full_name) for s in db.scalars(select(Staff).where(Staff.is_active.is_(True)).order_by(Staff.full_name))]


@router.get("/clients")
def list_clients(request: Request, q: str = "", status: str = "", kyc: str = "", link: int = 0, page: int = 1,
                 me: Staff = Depends(staff_perm("clients.view")), db: Session = Depends(get_db)):
    query = svc.visible_clients(me)
    ref_link = db.get(ReferralLink, link) if link else None
    if link:
        query = query.where(Client.referral_link_id == link)
    if q.strip():
        like = f"%{q.strip()}%"
        conds = [Client.email.ilike(like), Client.first_name.ilike(like), Client.last_name.ilike(like),
                 Client.phone.ilike(like)]
        if q.strip().isdigit():
            conds.append(Client.id == int(q.strip()))
            conds.append(Client.id.in_(select(TradingAccount.client_id).where(TradingAccount.login == int(q.strip()))))
        query = query.where(or_(*conds))
    if status:
        query = query.where(Client.status == status)
    if kyc:
        query = query.where(Client.kyc_status == kyc)
    pg = Page(db, query.order_by(Client.id.desc()), page)
    staff_names = dict(db.execute(select(Staff.id, Staff.full_name)).all())
    return render(request, "admin/clients/list.html", page=pg, q=q, status=status, kyc=kyc, staff_names=staff_names,
                  ref_link=ref_link,
                  statuses=[s.value for s in ClientStatus], kyc_statuses=[s.value for s in KycStatus])


@router.get("/referral-links")
def referral_links(request: Request, me: Staff = Depends(staff_perm("clients.referrals")), db: Session = Depends(get_db)):
    links = db.scalars(select(ReferralLink).where(ReferralLink.staff_id == me.id).order_by(ReferralLink.tag)).all()
    stats = {lid: (n, last) for lid, n, last in db.execute(
        select(Client.referral_link_id, func.count(Client.id), func.max(Client.created_at))
        .where(Client.referral_link_id.in_([x.id for x in links])).group_by(Client.referral_link_id))}
    return render(request, "admin/clients/referral_links.html", links=links, stats=stats,
                  untagged=svc.referral_link(svc.staff_referral_code(db, me)), url=svc.referral_link)


@router.post("/referral-links/new")
def referral_link_create(request: Request, tag: str = Form(""), me: Staff = Depends(staff_perm("clients.referrals")),
                         db: Session = Depends(get_db)):
    return attempt(request, lambda: svc.create_referral_link(db, me, tag), "Referral link created.", "/admin/referral-links")


@router.post("/referral-links/{link_id}/delete")
def referral_link_delete(request: Request, link_id: int, me: Staff = Depends(staff_perm("clients.referrals")),
                         db: Session = Depends(get_db)):
    return attempt(request, lambda: svc.delete_referral_link(db, me, link_id), "Referral link deleted.",
                   "/admin/referral-links")


@router.get("/clients/new")
def new_client_page(request: Request, me: Staff = Depends(staff_perm("clients.manage")), db: Session = Depends(get_db)):
    return render(request, "admin/clients/new.html", staff_options=_staff_options(db))


@router.post("/clients/new")
def create_client(request: Request, email: str = Form(...), first_name: str = Form(...), last_name: str = Form(...),
                  phone: str = Form(""), country: str = Form(""), password: str = Form(""),
                  referral_code: str = Form(""), lead_source: str = Form(""), sales_staff_id: str = Form(""),
                  me: Staff = Depends(staff_perm("clients.manage")), db: Session = Depends(get_db)):
    created = {}

    def run():
        sales = opt_int(sales_staff_id) or (None if me.has_perm("clients.view_all") else me.id)
        created["c"] = svc.register_client(
            db, me, email=email, first_name=first_name.strip(), last_name=last_name.strip(), phone=phone or None,
            country=country or None, password=password or None, referral_code=referral_code or None,
            lead_source=lead_source or "staff", sales_staff_id=sales)
    resp = attempt(request, run, "Client created.", "/admin/clients")
    return redirect(f"/admin/clients/{created['c'].id}") if "c" in created else resp


@router.get("/clients/{client_id}")
def client_detail(request: Request, client_id: int, tab: str = "overview",
                  me: Staff = Depends(staff_perm("clients.view")), db: Session = Depends(get_db)):
    client = svc.ensure_visible(db, me, client_id)
    tab = tab if tab in TABS else "overview"
    ctx = {"c": client, "tab": tab}
    staff_names = dict(db.execute(select(Staff.id, Staff.full_name)).all())
    ctx["staff_names"] = staff_names
    ctx["accounts"] = db.scalars(select(TradingAccount).where(TradingAccount.client_id == client.id)
                                 .order_by(TradingAccount.id)).all()
    if tab == "overview":
        ctx["staff_options"] = _staff_options(db)
        ctx["referrer"] = db.get(Client, client.referred_by_id) if client.referred_by_id else None
        ctx["ref_link"] = db.get(ReferralLink, client.referral_link_id) if client.referral_link_id else None
        ctx["referred_count"] = len(db.scalars(select(Client.id).where(Client.referred_by_id == client.id)).all())
        ctx["ib"] = db.scalar(select(IBProfile).where(IBProfile.client_id == client.id))
        ctx["statuses"] = [s.value for s in ClientStatus]
    elif tab == "accounts":
        ctx["account_groups"] = db.scalars(select(AccountGroup).where(AccountGroup.enabled.is_(True))).all()
        ctx["styles"] = trader_styles.badges(db, ctx["accounts"])
    elif tab == "kyc":
        ctx["docs"] = db.scalars(select(KycDocument).where(KycDocument.client_id == client.id)
                                 .order_by(KycDocument.id.desc())).all()
        ctx["kyc_statuses"] = [s.value for s in KycStatus]
        ctx["doc_types"] = sorted(svc.ALLOWED_DOC_TYPES)
    elif tab == "finance":
        w = wallet.get_wallet(db, client.id)
        db.commit()
        ctx["wallet"] = w
        ctx["txs"] = db.scalars(select(WalletTransaction).where(WalletTransaction.wallet_id == w.id)
                                .order_by(WalletTransaction.id.desc()).limit(100)).all()
        ctx["payments"] = db.scalars(select(PaymentRequest).where(PaymentRequest.client_id == client.id)
                                     .order_by(PaymentRequest.id.desc()).limit(50)).all()
    elif tab == "notes":
        ctx["notes"] = db.scalars(select(ClientNote).where(ClientNote.client_id == client.id)
                                  .order_by(ClientNote.id.desc())).all()
    elif tab == "activity":
        logins = [a.login for a in ctx["accounts"]]
        ctx["events"] = db.scalars(select(AuditLog).where(or_(
            (AuditLog.entity == "client") & (AuditLog.entity_id == str(client.id)),
            (AuditLog.actor_type == "client") & (AuditLog.actor_id == client.id),
            (AuditLog.entity == "trading_account") & AuditLog.entity_id.in_([str(x) for x in logins]),
        )).order_by(AuditLog.id.desc()).limit(200)).all()
    return render(request, "admin/clients/detail.html", **ctx)


@router.post("/clients/{client_id}/edit")
def edit_client(request: Request, client_id: int, first_name: str = Form(...), last_name: str = Form(...), email: str = Form(""),
                phone: str = Form(""), country: str = Form(""), city: str = Form(""), address: str = Form(""),
                date_of_birth: str = Form(""), status: str = Form(...),
                me: Staff = Depends(staff_perm("clients.manage")), db: Session = Depends(get_db)):
    client = svc.ensure_visible(db, me, client_id)

    def run():
        try:
            dob = date.fromisoformat(date_of_birth) if date_of_birth else None
            new_status = ClientStatus(status)
        except ValueError:
            raise ServiceError("Invalid date of birth or status")
        if len(country.strip()) not in (0, 2):
            raise ServiceError("Country must be a 2-letter ISO code")
        changes = {"email": email} if email.strip() else {}
        svc.update_client(db, me, client, changes | {
            "first_name": first_name.strip(), "last_name": last_name.strip(), "phone": phone or None,
            "country": country.strip().upper() or None, "city": city or None, "address": address or None,
            "date_of_birth": dob, "status": new_status})
    return attempt(request, run, "Client updated.", f"/admin/clients/{client_id}")


@router.post("/clients/{client_id}/push-mt5")
def push_mt5(request: Request, client_id: int, me: Staff = Depends(staff_perm("clients.manage")),
             db: Session = Depends(get_db)):
    from app.services import client_sync

    client = svc.ensure_visible(db, me, client_id)

    def run():
        errors = client_sync.push_client(db, client)
        if errors:
            raise ServiceError(f"MT5 still refuses: {'; '.join(errors)[:300]}")
    return attempt(request, run, "Client details written to MT5.", f"/admin/clients/{client_id}")


@router.post("/clients/{client_id}/assign")
def assign(request: Request, client_id: int, sales_staff_id: str = Form(""), account_manager_id: str = Form(""),
           me: Staff = Depends(staff_perm("clients.manage")), db: Session = Depends(get_db)):
    client = svc.ensure_visible(db, me, client_id)
    return attempt(request, lambda: svc.assign_client(db, me, client, opt_int(sales_staff_id), opt_int(account_manager_id)),
                   "Assignment saved.", f"/admin/clients/{client_id}")


@router.post("/clients/{client_id}/notes")
def add_note(request: Request, client_id: int, body: str = Form(...),
             me: Staff = Depends(staff_perm("clients.manage")), db: Session = Depends(get_db)):
    client = svc.ensure_visible(db, me, client_id)

    def run():
        if not body.strip():
            raise ServiceError("Note cannot be empty")
        svc.add_note(db, me, client, body.strip())
    return attempt(request, run, "Note added.", f"/admin/clients/{client_id}?tab=notes")


@router.post("/clients/{client_id}/kyc-status")
def kyc_status(request: Request, client_id: int, status: str = Form(...),
               me: Staff = Depends(staff_perm("kyc.review")), db: Session = Depends(get_db)):
    client = svc.ensure_visible(db, me, client_id)

    def run():
        svc.set_kyc_status(db, me, client, opt_enum(KycStatus, status, "KYC status"))
    return attempt(request, run, "KYC status updated.", f"/admin/clients/{client_id}?tab=kyc")


@router.post("/clients/{client_id}/kyc/upload")
async def kyc_upload(request: Request, client_id: int, doc_type: str = Form(...), file: UploadFile = File(...),
                     me: Staff = Depends(staff_perm("clients.manage")), db: Session = Depends(get_db)):
    client = svc.ensure_visible(db, me, client_id)
    content = bytearray()
    while chunk := await file.read(1024 * 1024):  # stop reading once over the limit
        content += chunk
        if len(content) > svc.MAX_DOC_BYTES:
            break
    content = bytes(content)
    return attempt(request, lambda: svc.upload_kyc_document(db, me, client, doc_type, file.filename or "upload", content),
                   "Document uploaded.", f"/admin/clients/{client_id}?tab=kyc")


@router.get("/kyc")
def kyc_queue(request: Request, page: int = 1, me: Staff = Depends(staff_perm("kyc.review")), db: Session = Depends(get_db)):
    visible = svc.visible_client_ids(me)
    query = select(KycDocument).where(KycDocument.status == KycStatus.pending, KycDocument.client_id.in_(visible)) \
        .order_by(KycDocument.id)
    pg = Page(db, query, page)
    clients = {c.id: c for c in db.scalars(select(Client).where(Client.id.in_({d.client_id for d in pg.items})))}
    return render(request, "admin/kyc_queue.html", page=pg, clients=clients)


@router.get("/kyc/{doc_id}/file")
def kyc_file(doc_id: int, me: Staff = Depends(staff_perm("kyc.review")), db: Session = Depends(get_db)):
    doc = get_or_404(db, KycDocument, doc_id)
    svc.ensure_visible(db, me, doc.client_id)
    # Served as an attachment so a crafted file can never execute in the CRM's origin.
    return FileResponse(doc.file_path, filename=doc.original_name, content_disposition_type="attachment")


@router.post("/kyc/{doc_id}/review")
def kyc_review(request: Request, doc_id: int, decision: str = Form(...), reason: str = Form(""),
               me: Staff = Depends(staff_perm("kyc.review")), db: Session = Depends(get_db)):
    doc = get_or_404(db, KycDocument, doc_id)
    svc.ensure_visible(db, me, doc.client_id)
    approve = decision == "approve"
    return attempt(request, lambda: svc.review_kyc_document(db, me, doc, approve, reason.strip() or None),
                   "Document approved." if approve else "Document rejected.", f"/admin/clients/{doc.client_id}?tab=kyc")
