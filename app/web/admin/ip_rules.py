"""Administration > IP access rules (blacklist / whitelist, optionally on the MT5 firewall)."""

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import client_ip
from app.core.errors import ServiceError, get_or_404
from app.db import get_db
from app.models import IpRule, MT5Server, Staff
from app.mt5 import MT5Error, get_gateway
from app.services import ip_rules as svc
from app.web.common import attempt, opt_int, render, staff_perm

router = APIRouter()
PERM = "security.ip_rules"


class RuleForm:
    """The rule form's fields (shared by create and edit)."""

    def __init__(self, network: str = Form(""), action: str = Form(""), scope: str = Form("all"),
                 description: str = Form(""), enabled: str = Form(""), expires_at: str = Form(""),
                 apply_mt5: str = Form(""), mt5_server_ids: list[str] = Form([])):
        self.raw = dict(network=network, action=action, scope=scope, description=description, enabled=enabled,
                        expires_at=expires_at, apply_mt5=apply_mt5, mt5_server_ids=mt5_server_ids)

    def fields(self) -> dict:
        f = self.raw
        try:
            expires_at = datetime.fromisoformat(f["expires_at"].strip()) if f["expires_at"].strip() else None
        except ValueError:
            raise ServiceError("Enter the expiry as a date and time")
        return dict(network=f["network"].strip(), action=f["action"], scope=f["scope"] or "all",
                    description=f["description"].strip()[:255], enabled=f["enabled"] == "on", expires_at=expires_at,
                    apply_mt5=f["apply_mt5"] == "on",
                    mt5_server_ids=[x for x in (opt_int(v) for v in f["mt5_server_ids"]) if x])


def _ctx(db: Session, request: Request) -> dict:
    return dict(scopes=svc.SCOPES, my_ip=client_ip(request),
                servers=db.scalars(select(MT5Server).order_by(MT5Server.id)).all())


def _report(rule: IpRule) -> str:
    bad = [v["message"] for v in (rule.mt5_state or {}).values() if not v.get("ok")]
    if bad:
        raise ServiceError("Saved in the CRM, but MT5 refused it: " + "; ".join(bad[:3]) + ". Use Re-apply to retry.")
    done = sum(1 for v in (rule.mt5_state or {}).values() if v.get("present"))
    return f" Applied on {done} MT5 server(s)." if done else ""


@router.get("/security/ip-rules")
def rules_page(request: Request, firewall: int | None = None, me: Staff = Depends(staff_perm(PERM)),
               db: Session = Depends(get_db)):
    rules = db.scalars(select(IpRule).order_by(IpRule.action, IpRule.id)).all()
    fw, fw_error = None, None
    if firewall:
        try:
            fw = get_gateway(firewall).get_firewall()
        except MT5Error as exc:
            fw_error = str(exc)
    return render(request, "admin/security/ip_rules.html", rules=rules, r=None, firewall_id=firewall,
                  firewall=fw, firewall_error=fw_error, **_ctx(db, request))


@router.post("/security/ip-rules")
def rule_create(request: Request, form: RuleForm = Depends(), me: Staff = Depends(staff_perm(PERM)),
                db: Session = Depends(get_db)):
    return attempt(request, lambda: _report(svc.create_rule(db, me, client_ip(request), **form.fields())),
                   "IP rule added.{result}", "/admin/security/ip-rules")


@router.get("/security/ip-rules/{rule_id}")
def rule_edit(request: Request, rule_id: int, me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    return render(request, "admin/security/ip_rule_form.html", r=get_or_404(db, IpRule, rule_id), **_ctx(db, request))


@router.post("/security/ip-rules/{rule_id}")
def rule_update(request: Request, rule_id: int, form: RuleForm = Depends(), me: Staff = Depends(staff_perm(PERM)),
                db: Session = Depends(get_db)):
    rule = get_or_404(db, IpRule, rule_id)
    return attempt(request, lambda: _report(svc.update_rule(db, me, client_ip(request), rule, form.fields())),
                   "IP rule saved.{result}", "/admin/security/ip-rules")


@router.post("/security/ip-rules/{rule_id}/reapply")
def rule_reapply(request: Request, rule_id: int, me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    rule = get_or_404(db, IpRule, rule_id)

    def run():
        errors = svc.reapply(db, me, rule)
        if errors:
            raise ServiceError("MT5 still refuses: " + "; ".join(errors[:3]))
    return attempt(request, run, "MT5 firewall updated.", f"/admin/security/ip-rules/{rule_id}")


@router.post("/security/ip-rules/{rule_id}/delete")
def rule_delete(request: Request, rule_id: int, me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    rule = get_or_404(db, IpRule, rule_id)
    return attempt(request, lambda: svc.delete_rule(db, me, client_ip(request), rule), "IP rule deleted.",
                   "/admin/security/ip-rules")
