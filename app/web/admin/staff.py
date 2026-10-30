from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import get_or_404
from app.core.permissions import PERMISSIONS
from app.db import get_db
from app.models import AuditLog, Department, LoginAttempt, Role, Staff
from app.services import staff as svc
from app.web.common import Page, attempt, opt_int, redirect, render, staff_perm

router = APIRouter()


def _ips(text: str) -> list[str]:
    return [x.strip() for x in text.replace(";", ",").replace("\n", ",").split(",") if x.strip()]


def _form_ctx(db: Session) -> dict:
    return {"roles": db.scalars(select(Role).order_by(Role.name)).all(),
            "departments": db.scalars(select(Department).order_by(Department.name)).all(),
            "managers": db.scalars(select(Staff).where(Staff.is_active.is_(True)).order_by(Staff.full_name)).all()}


@router.get("/staff")
def staff_list(request: Request, me: Staff = Depends(staff_perm("staff.view")), db: Session = Depends(get_db)):
    rows = db.scalars(select(Staff).order_by(Staff.is_active.desc(), Staff.full_name)).all()
    deps = {d.id: d.name for d in db.scalars(select(Department))}
    return render(request, "admin/staff/list.html", rows=rows, deps=deps)


@router.get("/staff/new")
def staff_new(request: Request, me: Staff = Depends(staff_perm("staff.manage")), db: Session = Depends(get_db)):
    return render(request, "admin/staff/form.html", s=None, **_form_ctx(db))


@router.post("/staff/new")
async def staff_create(request: Request, me: Staff = Depends(staff_perm("staff.manage")), db: Session = Depends(get_db)):
    f = await request.form()
    return attempt(request, lambda: svc.create_staff(
        db, me, email=str(f.get("email", "")), full_name=str(f.get("full_name", "")).strip(),
        password=str(f.get("password", "")), role_ids=[int(x) for x in f.getlist("role_ids")],
        department_id=opt_int(f.get("department_id")), manager_id=opt_int(f.get("manager_id")),
        allowed_ips=_ips(str(f.get("allowed_ips", "")))), "Staff member created.", "/admin/staff")


@router.get("/staff/{staff_id}")
def staff_edit(request: Request, staff_id: int, me: Staff = Depends(staff_perm("staff.view")), db: Session = Depends(get_db)):
    return render(request, "admin/staff/form.html", s=get_or_404(db, Staff, staff_id, "Staff member"), **_form_ctx(db))


@router.post("/staff/{staff_id}")
async def staff_update(request: Request, staff_id: int, me: Staff = Depends(staff_perm("staff.manage")),
                       db: Session = Depends(get_db)):
    target = get_or_404(db, Staff, staff_id, "Staff member")
    f = await request.form()
    changes = dict(full_name=str(f.get("full_name", "")).strip() or None, department_id=opt_int(f.get("department_id")),
                   manager_id=opt_int(f.get("manager_id")), allowed_ips=_ips(str(f.get("allowed_ips", ""))),
                   is_active=f.get("is_active") == "on", password=str(f.get("password") or "") or None)
    if target.id != me.id:
        changes["role_ids"] = [int(x) for x in f.getlist("role_ids")]
    return attempt(request, lambda: svc.update_staff(db, me, target, **changes), "Staff member saved.",
                   f"/admin/staff/{staff_id}")


@router.post("/staff/{staff_id}/reset-2fa")
def staff_reset_2fa(request: Request, staff_id: int, me: Staff = Depends(staff_perm("staff.manage")),
                    db: Session = Depends(get_db)):
    target = get_or_404(db, Staff, staff_id, "Staff member")
    return attempt(request, lambda: svc.reset_staff_2fa(db, me, target), "2FA reset; they will enrol again at next sign-in.",
                   f"/admin/staff/{staff_id}")


# ---------------------------------------------------------------- roles
@router.get("/roles")
def roles(request: Request, me: Staff = Depends(staff_perm("staff.view")), db: Session = Depends(get_db)):
    return render(request, "admin/staff/roles.html", rows=db.scalars(select(Role).order_by(Role.name)).all())


@router.get("/roles/new")
def role_new(request: Request, me: Staff = Depends(staff_perm("staff.manage"))):
    return render(request, "admin/staff/role_form.html", r=None, permissions=PERMISSIONS)


@router.post("/roles/new")
async def role_create(request: Request, me: Staff = Depends(staff_perm("staff.manage")), db: Session = Depends(get_db)):
    f = await request.form()
    created = {}

    def run():
        created["r"] = svc.create_role(db, me, str(f.get("name", "")).strip(), list(f.getlist("permissions")),
                                       str(f.get("description", "")).strip())
    resp = attempt(request, run, "Role created.", "/admin/roles")
    return redirect("/admin/roles") if "r" in created else resp


@router.get("/roles/{role_id}")
def role_edit(request: Request, role_id: int, me: Staff = Depends(staff_perm("staff.view")), db: Session = Depends(get_db)):
    return render(request, "admin/staff/role_form.html", r=get_or_404(db, Role, role_id, "Role"), permissions=PERMISSIONS)


@router.post("/roles/{role_id}")
async def role_update(request: Request, role_id: int, me: Staff = Depends(staff_perm("staff.manage")),
                      db: Session = Depends(get_db)):
    role = get_or_404(db, Role, role_id, "Role")
    f = await request.form()
    return attempt(request, lambda: svc.update_role(db, me, role, list(f.getlist("permissions")),
                                                    str(f.get("description", "")).strip()),
                   "Role saved.", f"/admin/roles/{role_id}")


@router.get("/departments")
def departments(request: Request, me: Staff = Depends(staff_perm("staff.view")), db: Session = Depends(get_db)):
    rows = db.scalars(select(Department).order_by(Department.name)).all()
    counts = {}
    for s in db.scalars(select(Staff).where(Staff.department_id.is_not(None))):
        counts[s.department_id] = counts.get(s.department_id, 0) + 1
    return render(request, "admin/staff/departments.html", rows=rows, counts=counts)


@router.post("/departments")
def department_create(request: Request, name: str = Form(...), me: Staff = Depends(staff_perm("staff.manage")),
                      db: Session = Depends(get_db)):
    return attempt(request, lambda: svc.create_department(db, me, name.strip()), "Department created.", "/admin/departments")


# ---------------------------------------------------------------- security
@router.get("/audit")
def audit_log(request: Request, action: str = "", actor: str = "", entity: str = "", date_from: str = "", page: int = 1,
              me: Staff = Depends(staff_perm("security.view")), db: Session = Depends(get_db)):
    q = select(AuditLog)
    if action:
        q = q.where(AuditLog.action.startswith(action.strip()))
    if actor in ("staff", "client", "system"):
        q = q.where(AuditLog.actor_type == actor)
    if entity:
        q = q.where(AuditLog.entity == entity.strip())
    try:
        if date_from:
            q = q.where(AuditLog.created_at >= datetime.fromisoformat(date_from))
    except ValueError:
        pass
    pg = Page(db, q.order_by(AuditLog.id.desc()), page, per_page=100)
    staff_names = dict(db.execute(select(Staff.id, Staff.full_name)).all())
    return render(request, "admin/staff/audit.html", page=pg, action=action, actor=actor, entity=entity,
                  date_from=date_from, staff_names=staff_names)


@router.get("/login-attempts")
def login_attempts(request: Request, email: str = "", failed: int = 0, page: int = 1,
                   me: Staff = Depends(staff_perm("security.view")), db: Session = Depends(get_db)):
    q = select(LoginAttempt)
    if email:
        q = q.where(LoginAttempt.email == email.strip().lower())
    if failed:
        q = q.where(LoginAttempt.success.is_(False))
    pg = Page(db, q.order_by(LoginAttempt.id.desc()), page, per_page=100)
    return render(request, "admin/staff/login_attempts.html", page=pg, email=email, failed=failed)
