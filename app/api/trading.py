from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import gateway, require
from app.db import get_db
from app.models import Deal, Position, Staff, SyncState
from app.mt5 import MT5Gateway
from app.schemas import DealOut, PositionOut, SyncIn, SyncStateOut
from app.services import sync
from app.services.clients import visible_rows

router = APIRouter(tags=["trading"])


@router.get("/deals", response_model=list[DealOut])
def list_deals(login: int | None = None, server_id: int | None = None, symbol: str | None = None, date_from: datetime | None = None,
               date_to: datetime | None = None, trades_only: bool = False,
               limit: int = Query(200, le=5000), offset: int = 0,
               db: Session = Depends(get_db), me: Staff = Depends(require("trading.view"))):
    q = select(Deal).where(visible_rows(me, Deal))
    if login:
        q = q.where(Deal.login == login)
    if server_id:
        q = q.where(Deal.server_id == server_id)
    if symbol:
        q = q.where(Deal.symbol == symbol)
    if date_from:
        q = q.where(Deal.time >= date_from)
    if date_to:
        q = q.where(Deal.time <= date_to)
    if trades_only:
        q = q.where(Deal.action.in_((0, 1)))
    return db.scalars(q.order_by(Deal.time.desc(), Deal.ticket.desc()).limit(limit).offset(offset)).all()


@router.get("/positions", response_model=list[PositionOut])
def list_positions(login: int | None = None, server_id: int | None = None, open_only: bool = True, limit: int = Query(500, le=5000),
                   db: Session = Depends(get_db), me: Staff = Depends(require("trading.view"))):
    q = select(Position).where(visible_rows(me, Position))
    if login:
        q = q.where(Position.login == login)
    if server_id:
        q = q.where(Position.server_id == server_id)
    if open_only:
        q = q.where(Position.is_open.is_(True))
    return db.scalars(q.order_by(Position.time_create.desc()).limit(limit)).all()


@router.post("/sync")
def trigger_sync(body: SyncIn, db: Session = Depends(get_db), gw: MT5Gateway = Depends(gateway),
                 _: Staff = Depends(require("trading.sync"))):
    return sync.full_sync(db, None, body.logins)


@router.get("/sync/state", response_model=list[SyncStateOut])
def sync_state(errors_only: bool = False, db: Session = Depends(get_db), _: Staff = Depends(require("trading.view"))):
    q = select(SyncState)
    if errors_only:
        q = q.where(SyncState.last_error.is_not(None))
    return db.scalars(q).all()
