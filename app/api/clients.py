from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.deps import require
from app.core.errors import get_or_404
from app.db import get_db
from app.models import ClientNote, ClientStatus, KycDocument, KycStatus, Staff, TradingAccount, WalletTransaction
from app.schemas import (
    AssignIn, ClientCreateIn, ClientOut, ClientUpdate, KycDocOut, KycReviewIn, KycStatusIn, NoteIn, NoteOut,
    TradingAccountOut, WalletOut, WalletTxOut,
)
from app.services import clients as svc
from app.services import wallet

router = APIRouter(prefix="/clients", tags=["clients"])


@router.get("", response_model=list[ClientOut])
def list_clients(q: str | None = None, status: ClientStatus | None = None, kyc_status: KycStatus | None = None,
                 limit: int = Query(50, le=500), offset: int = 0,
                 db: Session = Depends(get_db), me: Staff = Depends(require("clients.view"))):
    from app.models import Client

    query = svc.visible_clients(me)
    if q:
        like = f"%{q.lower()}%"
        query = query.where(or_(Client.email.ilike(like), Client.first_name.ilike(like),
                                Client.last_name.ilike(like), Client.phone.ilike(like)))
    if status:
        query = query.where(Client.status == status)
    if kyc_status:
        query = query.where(Client.kyc_status == kyc_status)
    return db.scalars(query.order_by(Client.id.desc()).limit(limit).offset(offset)).all()


@router.post("", response_model=ClientOut, status_code=201)
def create_client(body: ClientCreateIn, db: Session = Depends(get_db), me: Staff = Depends(require("clients.manage"))):
    data = body.model_dump()
    data["sales_staff_id"] = data["sales_staff_id"] or (None if me.has_perm("clients.view_all") else me.id)
    return svc.register_client(db, me, **data)


@router.get("/{client_id}", response_model=ClientOut)
def get_client(client_id: int, db: Session = Depends(get_db), me: Staff = Depends(require("clients.view"))):
    return svc.ensure_visible(db, me, client_id)


@router.patch("/{client_id}", response_model=ClientOut)
def update_client(client_id: int, body: ClientUpdate, db: Session = Depends(get_db),
                  me: Staff = Depends(require("clients.manage"))):
    client = svc.ensure_visible(db, me, client_id)
    return svc.update_client(db, me, client, body.model_dump(exclude_unset=True))


@router.put("/{client_id}/assignment", response_model=ClientOut)
def assign(client_id: int, body: AssignIn, db: Session = Depends(get_db), me: Staff = Depends(require("clients.manage"))):
    return svc.assign_client(db, me, svc.ensure_visible(db, me, client_id), body.sales_staff_id, body.account_manager_id)


@router.get("/{client_id}/notes", response_model=list[NoteOut])
def list_notes(client_id: int, db: Session = Depends(get_db), me: Staff = Depends(require("clients.view"))):
    svc.ensure_visible(db, me, client_id)
    return db.scalars(select(ClientNote).where(ClientNote.client_id == client_id).order_by(ClientNote.id.desc())).all()


@router.post("/{client_id}/notes", response_model=NoteOut, status_code=201)
def add_note(client_id: int, body: NoteIn, db: Session = Depends(get_db), me: Staff = Depends(require("clients.manage"))):
    return svc.add_note(db, me, svc.ensure_visible(db, me, client_id), body.body)


@router.get("/{client_id}/accounts", response_model=list[TradingAccountOut])
def client_accounts(client_id: int, db: Session = Depends(get_db), me: Staff = Depends(require("clients.view"))):
    svc.ensure_visible(db, me, client_id)
    return db.scalars(select(TradingAccount).where(TradingAccount.client_id == client_id)).all()


@router.get("/{client_id}/wallet", response_model=WalletOut)
def client_wallet(client_id: int, db: Session = Depends(get_db), me: Staff = Depends(require("finance.view"))):
    svc.ensure_visible(db, me, client_id)
    w = wallet.get_wallet(db, client_id)
    db.commit()
    return w


@router.get("/{client_id}/wallet/transactions", response_model=list[WalletTxOut])
def client_wallet_txs(client_id: int, limit: int = Query(100, le=1000), db: Session = Depends(get_db),
                      me: Staff = Depends(require("finance.view"))):
    svc.ensure_visible(db, me, client_id)
    w = wallet.get_wallet(db, client_id)
    return db.scalars(select(WalletTransaction).where(WalletTransaction.wallet_id == w.id)
                      .order_by(WalletTransaction.id.desc()).limit(limit)).all()


# ---------------------------------------------------------------- KYC
@router.get("/{client_id}/kyc", response_model=list[KycDocOut])
def list_kyc(client_id: int, db: Session = Depends(get_db), me: Staff = Depends(require("clients.view"))):
    svc.ensure_visible(db, me, client_id)
    return db.scalars(select(KycDocument).where(KycDocument.client_id == client_id)).all()


@router.post("/{client_id}/kyc", response_model=KycDocOut, status_code=201)
async def upload_kyc(client_id: int, doc_type: str = Form(...), file: UploadFile = File(...),
                     db: Session = Depends(get_db), me: Staff = Depends(require("clients.manage"))):
    client = svc.ensure_visible(db, me, client_id)
    return svc.upload_kyc_document(db, me, client, doc_type, file.filename or "upload", await file.read())


@router.get("/kyc/{doc_id}/file")
def download_kyc(doc_id: int, db: Session = Depends(get_db), me: Staff = Depends(require("kyc.review"))):
    doc = get_or_404(db, KycDocument, doc_id)
    svc.ensure_visible(db, me, doc.client_id)
    return FileResponse(doc.file_path, filename=doc.original_name)


@router.post("/kyc/{doc_id}/review", response_model=KycDocOut)
def review_kyc(doc_id: int, body: KycReviewIn, db: Session = Depends(get_db), me: Staff = Depends(require("kyc.review"))):
    doc = get_or_404(db, KycDocument, doc_id)
    svc.ensure_visible(db, me, doc.client_id)
    return svc.review_kyc_document(db, me, doc, body.approve, body.reason)


@router.put("/{client_id}/kyc-status", response_model=ClientOut)
def kyc_status(client_id: int, body: KycStatusIn, db: Session = Depends(get_db), me: Staff = Depends(require("kyc.review"))):
    return svc.set_kyc_status(db, me, svc.ensure_visible(db, me, client_id), body.status)
