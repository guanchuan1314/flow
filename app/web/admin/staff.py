from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import get_or_404
from app.core.permissions import PERMISSIONS
from app.db import get_db
from app.models import AuditLog, Client, Department, LoginAttempt, ReferralLink, Role, Staff
from app.services import staff as svc
from app.services.clients import referral_link, staff_referral_code
from app.web import labels
from app.web.common import Page, attempt, opt_int, redirect, render, staff_perm

router = APIRouter()


def _ips(text: str) -> list[str]:
    return [x.strip() for x in text.replace(";", ",").replace("\n", ",").split(",") if x.strip()]


def _form_ctx(db: Session) -> dict:
    return {"roles": db.scalars(select(Role).order_by(Role.name)).all(),
            "departments": db.scalars(select(Department).order_by(Department.name)).all(),
            "managers": db.scalars(select(Staff).where(Staff.is_active.is_(True)).order_by(Staff.full_name)).all()}


@router.get("/staff/members")
def staff_list(request: Request, me: Staff = Depends(staff_perm("staff.view")), db: Session = Depends(get_db)):
    rows = db.scalars(select(Staff).order_by(Staff.is_active.desc(), Staff.full_name)).all()
    deps = {d.id: d.name for d in db.scalars(select(Department))}
    return render(request, "admin/staff/list.html", rows=rows, deps=deps)


@router.get("/staff/members/new")
def staff_new(request: Request, me: Staff = Depends(staff_perm("staff.manage")), db: Session = Depends(get_db)):
    return render(request, "admin/staff/form.html", s=None, **_form_ctx(db))


@router.post("/staff/members/new")
async def staff_create(request: Request, me: Staff = Depends(staff_perm("staff.manage")), db: Session = Depends(get_db)):
    f = await request.form()
    return attempt(request, lambda: svc.create_staff(
        db, me, email=str(f.get("email", "")), full_name=str(f.get("full_name", "")).strip(),
        password=str(f.get("password", "")), role_ids=[int(x) for x in f.getlist("role_ids")],
        department_id=opt_int(f.get("department_id")), manager_id=opt_int(f.get("manager_id")),
        allowed_ips=_ips(str(f.get("allowed_ips", "")))), "Staff member created.", "/admin/staff/members")


@router.get("/staff/members/{staff_id}")
def staff_edit(request: Request, staff_id: int, me: Staff = Depends(staff_perm("staff.view")), db: Session = Depends(get_db)):
    s = get_or_404(db, Staff, staff_id, "Staff member")
    link = referral_link(staff_referral_code(db, s)) if s.has_perm("clients.referrals") else None
    counts = dict(db.execute(select(Client.referral_link_id, func.count(Client.id))
                             .join(ReferralLink, ReferralLink.id == Client.referral_link_id)
                             .where(ReferralLink.staff_id == s.id).group_by(Client.referral_link_id)).all())
    tagged = [(x, counts.get(x.id, 0)) for x in
              db.scalars(select(ReferralLink).where(ReferralLink.staff_id == s.id).order_by(ReferralLink.tag))]
    return render(request, "admin/staff/form.html", s=s, referral_link=link, tagged_links=tagged, **_form_ctx(db))


@router.post("/staff/members/{staff_id}")
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
                   f"/admin/staff/members/{staff_id}")


@router.post("/staff/members/{staff_id}/reset-2fa")
def staff_reset_2fa(request: Request, staff_id: int, me: Staff = Depends(staff_perm("staff.manage")),
                    db: Session = Depends(get_db)):
    target = get_or_404(db, Staff, staff_id, "Staff member")
    return attempt(request, lambda: svc.reset_staff_2fa(db, me, target), "2FA reset; they will enrol again at next sign-in.",
                   f"/admin/staff/members/{staff_id}")


# ---------------------------------------------------------------- roles
@router.get("/staff/roles")
def roles(request: Request, me: Staff = Depends(staff_perm("staff.view")), db: Session = Depends(get_db)):
    return render(request, "admin/staff/roles.html", rows=db.scalars(select(Role).order_by(Role.name)).all(),
                  permissions=PERMISSIONS, perm_label=labels.permission_label)


@router.get("/staff/roles/new")
def role_new(request: Request, me: Staff = Depends(staff_perm("staff.manage"))):
    return render(request, "admin/staff/role_form.html", r=None, permissions=PERMISSIONS,
                  group_label=labels.permission_group)


@router.post("/staff/roles/new")
async def role_create(request: Request, me: Staff = Depends(staff_perm("staff.manage")), db: Session = Depends(get_db)):
    f = await request.form()
    created = {}

    def run():
        created["r"] = svc.create_role(db, me, str(f.get("name", "")).strip(), list(f.getlist("permissions")),
                                       str(f.get("description", "")).strip())
    resp = attempt(request, run, "Role created.", "/admin/staff/roles")
    return redirect("/admin/staff/roles") if "r" in created else resp


@router.get("/staff/roles/{role_id}")
def role_edit(request: Request, role_id: int, me: Staff = Depends(staff_perm("staff.view")), db: Session = Depends(get_db)):
    return render(request, "admin/staff/role_form.html", r=get_or_404(db, Role, role_id, "Role"), permissions=PERMISSIONS,
                  group_label=labels.permission_group)


@router.post("/staff/roles/{role_id}")
async def role_update(request: Request, role_id: int, me: Staff = Depends(staff_perm("staff.manage")),
                      db: Session = Depends(get_db)):
    role = get_or_404(db, Role, role_id, "Role")
    f = await request.form()
    return attempt(request, lambda: svc.update_role(db, me, role, list(f.getlist("permissions")),
                                                    str(f.get("description", "")).strip()),
                   "Role saved.", f"/admin/staff/roles/{role_id}")


@router.get("/staff/departments")
def departments(request: Request, me: Staff = Depends(staff_perm("staff.view")), db: Session = Depends(get_db)):
    rows = db.scalars(select(Department).order_by(Department.name)).all()
    counts = {}
    for s in db.scalars(select(Staff).where(Staff.department_id.is_not(None))):
        counts[s.department_id] = counts.get(s.department_id, 0) + 1
    return render(request, "admin/staff/departments.html", rows=rows, counts=counts)


@router.get("/staff/departments/new")
def department_new(request: Request, me: Staff = Depends(staff_perm("staff.manage"))):
    return render(request, "admin/staff/department_form.html", d=None, members=[])


@router.post("/staff/departments/new")
def department_create(request: Request, name: str = Form(...), me: Staff = Depends(staff_perm("staff.manage")),
                      db: Session = Depends(get_db)):
    return attempt(request, lambda: svc.create_department(db, me, name), "Department created.", "/admin/staff/departments")


@router.get("/staff/departments/{department_id}")
def department_edit(request: Request, department_id: int, me: Staff = Depends(staff_perm("staff.view")),
                    db: Session = Depends(get_db)):
    d = get_or_404(db, Department, department_id, "Department")
    members = db.scalars(select(Staff).where(Staff.department_id == d.id).order_by(Staff.full_name)).all()
    return render(request, "admin/staff/department_form.html", d=d, members=members)


@router.post("/staff/departments/{department_id}")
def department_update(request: Request, department_id: int, name: str = Form(...),
                      me: Staff = Depends(staff_perm("staff.manage")), db: Session = Depends(get_db)):
    d = get_or_404(db, Department, department_id, "Department")
    return attempt(request, lambda: svc.update_department(db, me, d, name), "Department saved.", "/admin/staff/departments")


@router.post("/staff/departments/{department_id}/delete")
def department_delete(request: Request, department_id: int, me: Staff = Depends(staff_perm("staff.manage")),
                      db: Session = Depends(get_db)):
    d = get_or_404(db, Department, department_id, "Department")
    return attempt(request, lambda: svc.delete_department(db, me, d), f'Department "{d.name}" deleted.', "/admin/staff/departments")


# ---------------------------------------------------------------- security
@router.get("/security/audit")
def audit_log(request: Request, action: str = "", actor: str = "", entity: str = "", date_from: str = "", page: int = 1,
              me: Staff = Depends(staff_perm("security.view")), db: Session = Depends(get_db)):
    q = select(AuditLog)
    if action:  # an area (the action's first part, see labels.ACTION_GROUPS) or a whole action
        action = action.strip()
        q = q.where(or_(AuditLog.action == action, AuditLog.action.startswith(action + ".")))
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
                  date_from=date_from, staff_names=staff_names,
                  areas=sorted(labels.ACTION_GROUPS.items(), key=lambda kv: kv[1]),
                  entities=sorted(labels.ENTITIES.items(), key=lambda kv: kv[1]))


@router.get("/security/login-attempts")
def login_attempts(request: Request, email: str = "", failed: int = 0, page: int = 1,
                   me: Staff = Depends(staff_perm("security.view")), db: Session = Depends(get_db)):
    q = select(LoginAttempt)
    if email:
        q = q.where(LoginAttempt.email == email.strip().lower())
    if failed:
        q = q.where(LoginAttempt.success.is_(False))
    pg = Page(db, q.order_by(LoginAttempt.id.desc()), page, per_page=100)
    return render(request, "admin/staff/login_attempts.html", page=pg, email=email, failed=failed)
