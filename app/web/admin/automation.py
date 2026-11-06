"""MT5 > Automation: rules the CRM runs on its own data (nothing is synced to MT5)."""

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import get_or_404
from app.db import get_db
from app.models import AccountGroup, AutomationRule, Staff
from app.services import automation as svc
from app.services import trader_styles
from app.web.common import attempt, opt_int, redirect, render, staff_perm

router = APIRouter()
PERM = "automation.manage"


def _form_ctx(db: Session) -> dict:
    return dict(triggers=svc.TRIGGERS, actions=svc.ACTIONS, kinds=svc.KINDS, styles=trader_styles.STYLES,
                groups=db.scalars(select(AccountGroup).order_by(AccountGroup.is_demo, AccountGroup.name)).all())


def _values(form) -> dict:
    """The form's fields -> save_rule() arguments (only the chosen trigger's / action's fields are sent)."""
    trigger, action = str(form.get("trigger", "")), str(form.get("action", ""))
    t_params = svc.TRIGGERS.get(trigger, (None, None, {}))[2]
    a_params = svc.ACTIONS.get(action, (None, None, {}))[2]
    return dict(name=str(form.get("name", "")), trigger=trigger, action=action,
                params={k: form.get(f"t_{trigger}_{k}") for k in t_params},
                action_params={k: form.get(f"a_{action}_{k}") for k in a_params},
                filters={"groups": form.get("groups", ""), "kind": form.get("kind", "live")},
                cooldown_hours=opt_int(form.get("cooldown_hours")) or 0, server_id=opt_int(form.get("server_id")))


@router.get("/automation")
def automation_page(request: Request, me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    rules = db.scalars(select(AutomationRule).order_by(AutomationRule.name)).all()
    return render(request, "admin/automation/list.html", rules=rules, runs=svc.runs(db, limit=50),
                  names={r.id: r.name for r in rules}, triggers=svc.TRIGGERS, actions=svc.ACTIONS,
                  every=svc.EVERY_MINUTES, styles=trader_styles.STYLES)


@router.get("/automation/new")
def rule_new(request: Request, me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    return render(request, "admin/automation/form.html", rule=None, preview=None, runs=[], **_form_ctx(db))


@router.post("/automation/new")
async def rule_create(request: Request, me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    form = await request.form()
    made = {}

    def run():
        made["rule"] = svc.save_rule(db, me, None, **_values(form))
    resp = attempt(request, run, "Rule saved, disabled: check Preview, then enable it.", "/admin/automation")
    return redirect(f"/admin/automation/{made['rule'].id}") if "rule" in made else resp


@router.get("/automation/{rule_id}")
def rule_edit(request: Request, rule_id: int, preview: str = "", me: Staff = Depends(staff_perm(PERM)),
              db: Session = Depends(get_db)):
    rule = get_or_404(db, AutomationRule, rule_id, "Automation rule")
    return render(request, "admin/automation/form.html", rule=rule, preview=svc.preview(db, rule) if preview else None,
                  runs=svc.runs(db, rule, limit=100), **_form_ctx(db))


@router.post("/automation/{rule_id}")
async def rule_update(request: Request, rule_id: int, me: Staff = Depends(staff_perm(PERM)),
                      db: Session = Depends(get_db)):
    rule = get_or_404(db, AutomationRule, rule_id, "Automation rule")
    form = await request.form()
    return attempt(request, lambda: svc.save_rule(db, me, rule, **_values(form)), "Rule saved.",
                   f"/admin/automation/{rule_id}")


@router.post("/automation/{rule_id}/toggle")
def rule_toggle(request: Request, rule_id: int, me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    rule = get_or_404(db, AutomationRule, rule_id, "Automation rule")
    enable = not rule.enabled
    message = f"Rule enabled: the worker checks it every {svc.EVERY_MINUTES} minutes." if enable else "Rule disabled."
    return attempt(request, lambda: svc.set_enabled(db, me, rule, enable), message, f"/admin/automation/{rule_id}")


@router.post("/automation/{rule_id}/run")
def rule_run(request: Request, rule_id: int, me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    rule = get_or_404(db, AutomationRule, rule_id, "Automation rule")
    return attempt(request, lambda: svc.describe(svc.run_rule(db, rule, me)), "Ran now: {result}.",
                   f"/admin/automation/{rule_id}")


@router.post("/automation/{rule_id}/delete")
def rule_delete(request: Request, rule_id: int, me: Staff = Depends(staff_perm(PERM)), db: Session = Depends(get_db)):
    rule = get_or_404(db, AutomationRule, rule_id, "Automation rule")
    name = rule.name
    return attempt(request, lambda: svc.delete_rule(db, me, rule), f"Rule {name} deleted, with its run log.",
                   "/admin/automation")
