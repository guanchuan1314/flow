from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import require
from app.db import get_db
from app.models import AuditLog, LoginAttempt, Staff
from app.schemas import AuditOut, LoginAttemptOut

router = APIRouter(prefix="/security", tags=["security"])


@router.get("/audit", response_model=list[AuditOut])
def audit_log(action: str | None = None, actor_type: str | None = None, actor_id: int | None = None,
              entity: str | None = None, entity_id: str | None = None, date_from: datetime | None = None,
              limit: int = Query(200, le=5000), offset: int = 0,
              db: Session = Depends(get_db), _: Staff = Depends(require("security.view"))):
    q = select(AuditLog)
    for col, val in ((AuditLog.actor_type, actor_type), (AuditLog.actor_id, actor_id),
                     (AuditLog.entity, entity), (AuditLog.entity_id, entity_id)):
        if val is not None:
            q = q.where(col == val)
    if action:
        q = q.where(AuditLog.action.startswith(action))
    if date_from:
        q = q.where(AuditLog.created_at >= date_from)
    return db.scalars(q.order_by(AuditLog.id.desc()).limit(limit).offset(offset)).all()


@router.get("/login-attempts", response_model=list[LoginAttemptOut])
def login_attempts(email: str | None = None, failed_only: bool = False, limit: int = Query(200, le=5000),
                   db: Session = Depends(get_db), _: Staff = Depends(require("security.view"))):
    q = select(LoginAttempt)
    if email:
        q = q.where(LoginAttempt.email == email.lower())
    if failed_only:
        q = q.where(LoginAttempt.success.is_(False))
    return db.scalars(q.order_by(LoginAttempt.id.desc()).limit(limit)).all()
