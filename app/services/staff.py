from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import Conflict, Forbidden, NotFound, ServiceError
from app.core.permissions import DEFAULT_ROLES, PERMISSIONS
from app.core.security import hash_password, validate_password_policy
from app.models import Department, Role, Staff


def _validate_permissions(perms: list[str]) -> None:
    modules = {p.split(".", 1)[0] for p in PERMISSIONS}
    for p in perms:
        if p == "*" or p in PERMISSIONS:
            continue
        if p.endswith(".*") and p[:-2] in modules:
            continue
        raise ServiceError(f"Unknown permission: {p}")


# ---------------------------------------------------------------- no privilege escalation
# Holding staff.manage lets someone manage staff and roles, but never beyond their own permissions:
# they can't grant what they don't hold (only "*" holders grant "*"), can't edit a role they hold,
# and can't touch the password, 2FA, roles, IPs or active flag of someone with permissions they lack.


def _covers(held: set[str], perm: str) -> bool:
    """Whether the permission set `held` includes `perm` ("*", "module.*" or "module.action")."""
    if "*" in held:
        return True
    if perm == "*":
        return False
    return perm in held or f"{perm.split('.', 1)[0]}.*" in held


def _check_grantable(actor: Staff | None, perms) -> None:
    if actor is None:  # bootstrap / CLI
        return
    missing = sorted(p for p in set(perms) if not _covers(actor.permissions, p))
    if missing:
        raise Forbidden(f"You can't grant permissions you don't hold: {', '.join(missing)}")


def _role_perms(roles: list[Role]) -> set[str]:
    return {p for r in roles for p in (r.permissions or [])}


def _check_outranks(actor: Staff | None, staff: Staff) -> None:
    """The actor holds every permission the staff member has (needed to change their sign-in or roles)."""
    if actor is None or actor.id == staff.id:
        return
    if not all(_covers(actor.permissions, p) for p in staff.permissions):
        raise Forbidden(f"{staff.full_name} has permissions you don't hold: only someone with at least "
                        "their permissions can change their password, 2FA, roles, IP allowlist or status")


def seed_roles(db: Session) -> None:
    existing = {r.name for r in db.scalars(select(Role))}
    for name, perms in DEFAULT_ROLES.items():
        if name not in existing:
            db.add(Role(name=name, permissions=perms, is_system=True, description=f"Default {name} role"))
    db.commit()


def create_role(db: Session, actor: Staff, name: str, permissions: list[str], description: str = "") -> Role:
    _validate_permissions(permissions)
    _check_grantable(actor, permissions)
    if db.scalar(select(Role).where(Role.name == name)):
        raise Conflict(f"Role {name} already exists")
    role = Role(name=name, permissions=permissions, description=description)
    db.add(role)
    db.flush()
    audit(db, actor, "role.create", "role", role.id, {"permissions": permissions})
    db.commit()
    return role


def update_role(db: Session, actor: Staff, role: Role, permissions: list[str] | None, description: str | None) -> Role:
    if role.is_system and role.name == "Admin":
        raise ServiceError("The Admin role cannot be modified")
    if actor is not None and "*" not in actor.permissions:
        if role.id in {r.id for r in actor.roles}:
            raise Forbidden("You cannot edit a role you hold")
        _check_grantable(actor, role.permissions or [])  # nor a role stronger than you
    before = list(role.permissions)
    if permissions is not None:
        _validate_permissions(permissions)
        _check_grantable(actor, permissions)
        role.permissions = permissions
    if description is not None:
        role.description = description
    audit(db, actor, "role.update", "role", role.id, {"before": before, "after": role.permissions})
    db.commit()
    return role


def _roles_by_ids(db: Session, role_ids: list[int]) -> list[Role]:
    roles = list(db.scalars(select(Role).where(Role.id.in_(role_ids)))) if role_ids else []
    if len(roles) != len(set(role_ids)):
        raise NotFound("One or more roles not found")
    return roles


def create_staff(db: Session, actor: Staff | None, *, email: str, full_name: str, password: str,
                 role_ids: list[int], department_id: int | None = None, manager_id: int | None = None,
                 allowed_ips: list[str] | None = None) -> Staff:
    validate_password_policy(password)
    email = email.lower()
    if db.scalar(select(Staff).where(Staff.email == email)):
        raise Conflict("Email already in use")
    roles = _roles_by_ids(db, role_ids)
    _check_grantable(actor, _role_perms(roles))
    staff = Staff(email=email, full_name=full_name, password_hash=hash_password(password),
                  department_id=department_id, manager_id=manager_id, allowed_ips=allowed_ips or [],
                  roles=roles)
    db.add(staff)
    db.flush()
    audit(db, actor, "staff.create", "staff", staff.id, {"email": email, "roles": role_ids})
    db.commit()
    return staff


def update_staff(db: Session, actor: Staff, staff: Staff, **changes) -> Staff:
    """Changing the password or deactivating signs the staff member out everywhere (token_version)."""
    from app.services.auth import bump_token_version

    new_roles = None
    if changes.get("role_ids") is not None:
        new_roles = _roles_by_ids(db, changes["role_ids"])
        if {r.id for r in new_roles} == {r.id for r in staff.roles}:
            new_roles = None  # unchanged (the web form always sends the roles)
        elif staff.id == actor.id:
            raise ServiceError("You cannot change your own roles")
    changes.pop("role_ids", None)
    if changes.get("is_active") is False and staff.id == actor.id:
        raise ServiceError("You cannot deactivate yourself")
    sensitive = (new_roles is not None or bool(changes.get("password"))
                 or (changes.get("is_active") is not None and changes["is_active"] != staff.is_active)
                 or (changes.get("allowed_ips") is not None
                     and list(changes["allowed_ips"]) != list(staff.allowed_ips or [])))
    if sensitive:
        _check_outranks(actor, staff)
    if new_roles is not None:
        _check_grantable(actor, _role_perms(new_roles))
        staff.roles = new_roles
    if changes.get("password"):
        validate_password_policy(changes["password"])
        staff.password_hash = hash_password(changes.pop("password"))
        bump_token_version(staff)
    changes.pop("password", None)
    if changes.get("is_active") is False and staff.is_active:
        bump_token_version(staff)
    for key, value in changes.items():
        if value is not None:
            setattr(staff, key, value)
    audit(db, actor, "staff.update", "staff", staff.id, {k: v for k, v in changes.items() if v is not None})
    db.commit()
    return staff


def reset_staff_2fa(db: Session, actor: Staff, staff: Staff) -> None:
    """Turns 2FA off (they enrol again at next sign-in) and signs them out everywhere."""
    from app.services.auth import bump_token_version

    _check_outranks(actor, staff)
    staff.totp_enabled = False
    staff.totp_secret = staff.totp_last_step = None
    bump_token_version(staff)
    audit(db, actor, "staff.reset_2fa", "staff", staff.id)
    db.commit()


def create_department(db: Session, actor: Staff, name: str) -> Department:
    name = " ".join(name.split())
    if not name:
        raise ServiceError("Give the department a name")
    if db.scalar(select(Department).where(Department.name == name)):
        raise Conflict("Department already exists")
    dep = Department(name=name)
    db.add(dep)
    db.flush()
    audit(db, actor, "department.create", "department", dep.id, {"name": name})
    db.commit()
    return dep


def update_department(db: Session, actor: Staff, dep: Department, name: str) -> Department:
    name = " ".join(name.split())
    if not name:
        raise ServiceError("Give the department a name")
    if db.scalar(select(Department.id).where(Department.name == name, Department.id != dep.id)):
        raise Conflict("Department already exists")
    if name != dep.name:
        audit(db, actor, "department.update", "department", dep.id, {"before": dep.name, "after": name})
        dep.name = name
    db.commit()
    return dep


def delete_department(db: Session, actor: Staff, dep: Department) -> int:
    """Deletes the department; its staff stay, without a department. Returns how many staff that was."""
    members = db.scalars(select(Staff).where(Staff.department_id == dep.id)).all()
    for s in members:
        s.department_id = None
    audit(db, actor, "department.delete", "department", dep.id, {"name": dep.name, "staff": len(members)})
    db.delete(dep)
    db.commit()
    return len(members)


def bootstrap_admin(db: Session, email: str, password: str, full_name: str = "Administrator") -> Staff:
    seed_roles(db)
    admin_role = db.scalar(select(Role).where(Role.name == "Admin"))
    return create_staff(db, None, email=email, full_name=full_name, password=password, role_ids=[admin_role.id])
