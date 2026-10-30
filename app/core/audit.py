from decimal import Decimal
from enum import Enum

from sqlalchemy.orm import Session

from app.models import AuditLog


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def audit(db: Session, actor, action: str, entity: str | None = None, entity_id=None,
          details: dict | None = None, ip: str | None = None) -> None:
    """Record an action. `actor` is a Staff, a Client, or None for the system.

    Added to the caller's session so it commits atomically with the change it describes.
    """
    from app.models import Client, Staff

    actor_type = "staff" if isinstance(actor, Staff) else "client" if isinstance(actor, Client) else "system"
    db.add(AuditLog(
        actor_type=actor_type, actor_id=getattr(actor, "id", None), action=action, entity=entity,
        entity_id=None if entity_id is None else str(entity_id), details=_jsonable(details or {}), ip=ip,
    ))
