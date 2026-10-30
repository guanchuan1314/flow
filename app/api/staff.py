from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import require
from app.core.errors import get_or_404
from app.core.permissions import PERMISSIONS
from app.db import get_db
from app.models import Department, Role, Staff
from app.schemas import (
    DepartmentIn, DepartmentOut, RoleIn, RoleOut, RoleUpdate, StaffIn, StaffOut, StaffUpdate,
)
from app.services import staff as svc

router = APIRouter(tags=["staff"])


@router.get("/permissions")
def list_permissions(_: Staff = Depends(require("staff.view"))):
    return PERMISSIONS


@router.get("/roles", response_model=list[RoleOut])
def list_roles(db: Session = Depends(get_db), _: Staff = Depends(require("staff.view"))):
    return db.scalars(select(Role).order_by(Role.name)).all()


@router.post("/roles", response_model=RoleOut, status_code=201)
def create_role(body: RoleIn, db: Session = Depends(get_db), me: Staff = Depends(require("staff.manage"))):
    return svc.create_role(db, me, body.name, body.permissions, body.description)


@router.patch("/roles/{role_id}", response_model=RoleOut)
def update_role(role_id: int, body: RoleUpdate, db: Session = Depends(get_db),
                me: Staff = Depends(require("staff.manage"))):
    return svc.update_role(db, me, get_or_404(db, Role, role_id), body.permissions, body.description)


@router.get("/departments", response_model=list[DepartmentOut])
def list_departments(db: Session = Depends(get_db), _: Staff = Depends(require("staff.view"))):
    return db.scalars(select(Department).order_by(Department.name)).all()


@router.post("/departments", response_model=DepartmentOut, status_code=201)
def create_department(body: DepartmentIn, db: Session = Depends(get_db), me: Staff = Depends(require("staff.manage"))):
    return svc.create_department(db, me, body.name)


@router.get("/staff", response_model=list[StaffOut])
def list_staff(active: bool | None = None, db: Session = Depends(get_db), _: Staff = Depends(require("staff.view"))):
    q = select(Staff).order_by(Staff.id)
    if active is not None:
        q = q.where(Staff.is_active.is_(active))
    return db.scalars(q).all()


@router.post("/staff", response_model=StaffOut, status_code=201)
def create_staff(body: StaffIn, db: Session = Depends(get_db), me: Staff = Depends(require("staff.manage"))):
    return svc.create_staff(db, me, **body.model_dump())


@router.get("/staff/{staff_id}", response_model=StaffOut)
def get_staff(staff_id: int, db: Session = Depends(get_db), _: Staff = Depends(require("staff.view"))):
    return get_or_404(db, Staff, staff_id)


@router.patch("/staff/{staff_id}", response_model=StaffOut)
def update_staff(staff_id: int, body: StaffUpdate, db: Session = Depends(get_db),
                 me: Staff = Depends(require("staff.manage"))):
    return svc.update_staff(db, me, get_or_404(db, Staff, staff_id), **body.model_dump(exclude_unset=True))


@router.post("/staff/{staff_id}/reset-2fa", status_code=204)
def reset_2fa(staff_id: int, db: Session = Depends(get_db), me: Staff = Depends(require("staff.manage"))):
    svc.reset_staff_2fa(db, me, get_or_404(db, Staff, staff_id))
