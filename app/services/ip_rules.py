"""IP blacklist / whitelist for the CRM, optionally mirrored to the MT5 servers' firewall.

CRM side (enforced by IpFilterMiddleware), per scope (admin, portal, website, or all):
  * block: matching addresses are refused.
  * allow: once a scope has at least one allow rule, only matching addresses may use it.
  Static files and health checks are never filtered; provider callbacks ignore allow lists.
  A change that would lock out the staff member making it is refused.

MT5 side (when "apply on MT5" is ticked): the rule is written to the MT5 server firewall
and kept in step on update and delete. The MT5 firewall covers every
connection to that server (traders' terminals, managers, this CRM), whatever the CRM scope.
"""

import ipaddress
import logging
import threading
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import ServiceError
from app.db import SessionLocal, utcnow
from app.models import IpRule, MT5Server, Staff
from app.mt5 import MT5Error, get_gateway
from app.mt5.gateway import MT5FirewallRule

log = logging.getLogger(__name__)
SCOPES = {"all": "Whole CRM", "admin": "Staff back-office", "portal": "Client area", "website": "Public website"}
ACTIONS = ("block", "allow")
NEVER_FILTERED = ("/static", "/health", "/ready")
CACHE_SECONDS = 5

_lock = threading.Lock()
_cache: tuple[float, list[tuple]] = (0.0, [])


def scope_of(path: str) -> str:
    if path.startswith(("/portal", "/api/portal", "/api/auth/client")):
        return "portal"
    if path.startswith(("/admin", "/api", "/docs", "/openapi.json")):
        return "admin"
    return "website"


def _parse(network: str):
    try:
        return ipaddress.ip_network(network.strip(), strict=False)
    except ValueError:
        raise ServiceError(f"'{network}' is not an IP address or range (e.g. 203.0.113.7 or 203.0.113.0/24)")


def _compiled(rules) -> list[tuple]:
    now = utcnow()
    return [(_parse(r.network), r.action, r.scope) for r in rules
            if r.enabled and (r.expires_at is None or r.expires_at > now)]


def invalidate() -> None:
    global _cache
    with _lock:
        _cache = (0.0, [])


def _active() -> list[tuple]:
    global _cache
    with _lock:
        if time.monotonic() < _cache[0]:
            return _cache[1]
    with SessionLocal() as db:
        rules = _compiled(db.scalars(select(IpRule)))
    with _lock:
        _cache = (time.monotonic() + CACHE_SECONDS, rules)
    return rules


def _decide(rules: list[tuple], ip: str | None, scope: str, allow_lists: bool = True) -> str | None:
    """Why `ip` is refused for `scope`, or None when it may pass."""
    try:
        addr = ipaddress.ip_address(ip) if ip else None
    except ValueError:
        addr = None
    applicable = [(net, action) for net, action, s in rules if s in ("all", scope)]

    def matches(net):
        return addr is not None and addr.version == net.version and addr in net
    if any(action == "block" and matches(net) for net, action in applicable):
        return "blocked"
    allows = [net for net, action in applicable if action == "allow"]
    if allow_lists and allows and not any(matches(net) for net in allows):
        return "not on the allow list"
    return None


def check(ip: str | None, path: str) -> str | None:
    """Middleware entry point."""
    if path.startswith(NEVER_FILTERED):
        return None
    return _decide(_active(), ip, scope_of(path), allow_lists=not path.startswith("/callbacks"))


# ---------------------------------------------------------------- management
def _validate(db: Session, rule: IpRule, my_ip: str | None) -> None:
    if rule.action not in ACTIONS:
        raise ServiceError("Choose block or allow")
    if rule.scope not in SCOPES:
        raise ServiceError("Unknown scope")
    rule.network = str(_parse(rule.network))
    known = set(db.scalars(select(MT5Server.id)))
    if set(rule.mt5_server_ids or []) - known:
        raise ServiceError("Unknown MT5 server")
    others = db.scalars(select(IpRule).where(IpRule.id != rule.id) if rule.id else select(IpRule)).all()
    if _decide(_compiled([*others, rule]), my_ip, "admin"):
        raise ServiceError(f"This would lock you out of the back-office (your address is {my_ip or 'unknown'}). "
                           "Add an allow rule for your own address first.")


def _targets(db: Session, rule: IpRule) -> list[int]:
    if not rule.apply_mt5:
        return []
    if rule.mt5_server_ids:
        return list(rule.mt5_server_ids)
    return list(db.scalars(select(MT5Server.id).where(MT5Server.enabled.is_(True)).order_by(MT5Server.id)))


def sync_mt5(db: Session, rule: IpRule, deleting: bool = False) -> dict:
    """Bring the MT5 firewalls in line with the rule: present on its target servers while it is
    enabled and applied to MT5, removed everywhere else it was written before."""
    net = _parse(rule.network)
    wanted = set() if deleting or not rule.enabled else set(_targets(db, rule))
    previously = {int(k) for k, v in (rule.mt5_state or {}).items() if v.get("present")}
    state = dict(rule.mt5_state or {})
    for sid in sorted(wanted | previously):
        try:
            gw = get_gateway(sid)
            if sid in wanted:
                gw.put_firewall_rule(rule.mt5_tag, MT5FirewallRule(
                    from_ip=str(net[0]), to_ip=str(net[-1]), action=rule.action,
                    comment=f"{rule.mt5_tag} {rule.description}".strip()))
                state[str(sid)] = {"ok": True, "present": True, "message": "applied", "at": utcnow().isoformat()}
            else:
                gw.delete_firewall_rule(rule.mt5_tag)
                state.pop(str(sid), None)
        except MT5Error as exc:
            log.warning("IP rule %s on server %s: %s", rule.id, sid, exc)
            state[str(sid)] = {"ok": False, "present": sid in previously, "message": str(exc)[:200],
                               "at": utcnow().isoformat()}
    rule.mt5_state = state
    return state


def _mt5_errors(rule: IpRule) -> list[str]:
    return [f"server {sid}: {v['message']}" for sid, v in (rule.mt5_state or {}).items() if not v.get("ok")]


def create_rule(db: Session, actor, my_ip: str | None, **fields) -> IpRule:
    defaults = dict(enabled=True, apply_mt5=False, mt5_server_ids=[], scope="all", description="")
    rule = IpRule(**(defaults | fields), mt5_state={}, created_by_id=actor.id if isinstance(actor, Staff) else None)
    _validate(db, rule, my_ip)
    db.add(rule)
    db.flush()
    sync_mt5(db, rule)
    audit(db, actor, "ip_rule.create", "ip_rule", rule.id, {k: v for k, v in fields.items()}, ip=my_ip)
    db.commit()
    invalidate()
    return rule


def update_rule(db: Session, actor, my_ip: str | None, rule: IpRule, fields: dict) -> IpRule:
    before = {k: getattr(rule, k) for k in fields}
    for k, v in fields.items():
        setattr(rule, k, v)
    try:
        _validate(db, rule, my_ip)
    except ServiceError:
        db.rollback()
        raise
    sync_mt5(db, rule)
    audit(db, actor, "ip_rule.update", "ip_rule", rule.id, {"before": before, "after": fields}, ip=my_ip)
    db.commit()
    invalidate()
    return rule


def delete_rule(db: Session, actor, my_ip: str | None, rule: IpRule) -> list[str]:
    """Delete the rule, removing it from the MT5 firewalls it was written to. Returns MT5 errors."""
    others = db.scalars(select(IpRule).where(IpRule.id != rule.id)).all()
    if _decide(_compiled(others), my_ip, "admin"):
        raise ServiceError("Deleting this rule would lock you out of the back-office")
    sync_mt5(db, rule, deleting=True)
    errors = _mt5_errors(rule)
    if errors:  # keep the rule so the MT5 entry is not orphaned; staff can retry
        db.commit()
        raise ServiceError("Could not remove it from MT5 (" + "; ".join(errors) + "). The rule was kept; try again.")
    audit(db, actor, "ip_rule.delete", "ip_rule", rule.id, {"network": rule.network, "action": rule.action}, ip=my_ip)
    db.delete(rule)
    db.commit()
    invalidate()
    return errors


def reapply(db: Session, actor, rule: IpRule) -> list[str]:
    sync_mt5(db, rule)
    audit(db, actor, "ip_rule.reapply", "ip_rule", rule.id, {})
    db.commit()
    return _mt5_errors(rule)


def expire(db: Session) -> int:
    """Worker: switch off expired rules (and take them off MT5)."""
    count = 0
    for rule in db.scalars(select(IpRule).where(IpRule.enabled.is_(True), IpRule.expires_at.is_not(None),
                                                IpRule.expires_at <= utcnow())):
        rule.enabled = False
        sync_mt5(db, rule)
        audit(db, None, "ip_rule.expired", "ip_rule", rule.id, {"network": rule.network})
        count += 1
    db.commit()
    if count:
        invalidate()
    return count
