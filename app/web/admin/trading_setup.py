"""MT5 > Trading setup (one page per server: quotes in, symbols, routing, orders out) and MT5 > Routing."""

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from app.core.errors import ServiceError, get_or_404
from app.db import get_db
from app.models import LiquidityConnection, RoutingRule, Staff
from app.services import liquidity, mt5_setup, routing_rules as svc
from app.services.servers import pick, require_server
from app.web import syncs
from app.web.common import WebForbidden, attempt, opt_int, render, staff_perm, web_staff

router = APIRouter()
SETUP_PERMS = ("symbols.manage", "liquidity.manage", "routing.manage")


def any_setup_perm(staff: Staff = Depends(web_staff)) -> Staff:
    if not any(staff.has_perm(p) for p in SETUP_PERMS):
        raise WebForbidden()
    return staff


# ---------------------------------------------------------------- trading setup
@router.get("/trading-setup/overview")
def setup_page(request: Request, server: str = "", me: Staff = Depends(any_setup_perm), db: Session = Depends(get_db)):
    srv = pick(db, server)
    return render(request, "admin/trading_setup.html", server=srv,
                  o=mt5_setup.overview(db, srv) if srv else None, kinds=liquidity.KINDS)


@router.post("/trading-setup/overview/sync")
def setup_sync(request: Request, server_id: str = Form(""), me: Staff = Depends(any_setup_perm),
               db: Session = Depends(get_db)):
    return syncs.start(request, db, me, "trading_setup", syncs.server_id(db, server_id), f"/admin/trading-setup/overview?server={server_id}")


# ---------------------------------------------------------------- routing rules
def _choices(db: Session, server_id: int) -> dict:
    gateways = [c for c in svc.gateway_targets(db, server_id).values()]
    return dict(actions=svc.ACTIONS, intermediate=svc.INTERMEDIATE, action_params=svc.ACTION_PARAMS,
                order_types=svc.TYPES, request_kinds=svc.REQUESTS, all_requests=svc.ALL_REQUESTS,
                rules_ops=svc.RULES, conditions=svc.CONDITIONS, gateways=sorted(gateways, key=lambda c: c.name),
                condition_label=svc.condition_label)


class RuleForm:
    """The routing rule form. Conditions come as aligned lists, one entry per row; empty rows are skipped."""

    def __init__(self, enabled: str = Form(""), order_type: list[str] = Form([]), request_mode: str = Form("all"),
                 request_kind: list[str] = Form([]), other_requests: str = Form("0"), action: str = Form(""),
                 param: str = Form(""), target: str = Form(""), dealer_logins: str = Form(""),
                 cond: list[str] = Form([]), cond_rule: list[str] = Form([]), cond_value: list[str] = Form([])):
        self.raw = {k: v for k, v in locals().items() if k != "self"}

    def values(self, db: Session, server_id: int, current: list[dict] | None = None) -> dict:
        f = self.raw
        action = opt_int(f["action"])
        if action is None:
            raise ServiceError("Choose what the rule does")
        if f["request_mode"] == "all":
            requests = svc.ALL_REQUESTS
        else:
            requests = (opt_int(f["other_requests"]) or 0) | sum(int(x) for x in f["request_kind"] if x.isdigit())
        conditions = []
        for code, rule, value in zip(f["cond"], f["cond_rule"], f["cond_value"]):
            if not code.strip():
                continue
            code = int(code)
            conditions.append({"condition": code, "rule": opt_int(rule) or 0, "type": svc.condition_type(code, current),
                               "value": value.strip()})
        dealers = []
        if action in (svc.ACTION_DEALER, svc.ACTION_DEALER_ONLINE):
            if f["target"].isdigit():
                gw = db.get(LiquidityConnection, int(f["target"]))
                if gw is None or gw.server_id != server_id or gw.kind != "gateway":
                    raise ServiceError("Choose a gateway of this server")
                dealers.append({"login": gw.gateway_login, "name": gw.name})
            for login in filter(None, (x.strip() for x in f["dealer_logins"].replace(",", " ").split())):
                if not login.isdigit():
                    raise ServiceError(f"Dealer logins are numbers (not \"{login}\")")
                if all(d["login"] != int(login) for d in dealers):
                    dealers.append({"login": int(login), "name": ""})
        return dict(enabled=f["enabled"] == "on", types=sum(int(x) for x in f["order_type"] if x.isdigit()),
                    requests=requests, action=action, param=f["param"].strip(), conditions=conditions,
                    dealers=dealers)


def _rule_ctx(db: Session, rule: RoutingRule | None, server) -> dict:
    targets = svc.gateway_targets(db, server.id) if server else {}
    dealers = rule.dealers if rule else []
    target = next((targets[d["login"]].id for d in dealers if d["login"] in targets), None)
    other_logins = " ".join(str(d["login"]) for d in dealers if d["login"] not in targets)
    requests = rule.requests if rule else svc.ALL_REQUESTS
    offered = sum(svc.REQUESTS)
    return dict(target=target, other_logins=other_logins, request_all=requests == svc.ALL_REQUESTS,
                requests_on=[b for b in svc.REQUESTS if requests & b], other_requests=requests & ~offered,
                **_choices(db, server.id if server else 0))


@router.get("/trading-setup/routing")
def routing_page(request: Request, server: str = "", me: Staff = Depends(staff_perm("routing.manage")), db: Session = Depends(get_db)):
    srv = pick(db, server)
    rows = svc.rules(db, srv.id) if srv else []
    targets = svc.gateway_targets(db, srv.id) if srv else {}
    return render(request, "admin/routing/list.html", server=srv,
                  rows=[(r, svc.describe(r, targets)) for r in rows], presets=svc.LOAD_PRESETS,
                  added={r.name for r in rows})


@router.post("/trading-setup/routing/preset")
def routing_preset(request: Request, key: str = Form(...), value: str = Form(""), server_id: str = Form(""),
                   me: Staff = Depends(staff_perm("routing.manage")), db: Session = Depends(get_db)):
    def run():
        rule = svc.add_load_preset(db, me, require_server(db, server_id).id, key, value)
        return f"Rule '{rule.name}' added on MT5 at the top, switched off. Switch it on when you want it to apply."
    return attempt(request, run, "{result}", f"/admin/trading-setup/routing?server={server_id}")


@router.post("/trading-setup/routing/sync")
def routing_sync(request: Request, server_id: str = Form(""), me: Staff = Depends(staff_perm("routing.manage")),
                 db: Session = Depends(get_db)):
    return syncs.start(request, db, me, "routing", syncs.server_id(db, server_id), f"/admin/trading-setup/routing?server={server_id}")


@router.get("/trading-setup/routing/new")
def rule_new(request: Request, gateway: str = "", server: str = "",
             me: Staff = Depends(staff_perm("routing.manage")), db: Session = Depends(get_db)):
    srv = pick(db, server)
    ctx = _rule_ctx(db, None, srv)
    if gateway.isdigit():  # "Route orders here" from a gateway
        ctx["target"] = int(gateway)
    return render(request, "admin/routing/form.html", r=None, server=srv,
                  preset_action=svc.ACTION_DEALER if gateway.isdigit() else 1006, **ctx)


@router.post("/trading-setup/routing/new")
def rule_create(request: Request, server_id: str = Form(...), name: str = Form(...), at_top: str = Form(""),
                form: RuleForm = Depends(), me: Staff = Depends(staff_perm("routing.manage")),
                db: Session = Depends(get_db)):
    def run():
        return svc.create_rule(db, me, server_id=int(server_id), name=name, values=form.values(db, int(server_id)),
                               at_top=at_top == "on").name
    return attempt(request, run, "Routing rule {result} created on MT5.", f"/admin/trading-setup/routing?server={server_id}")


@router.get("/trading-setup/routing/{rule_id}")
def rule_edit(request: Request, rule_id: int, me: Staff = Depends(staff_perm("routing.manage")),
              db: Session = Depends(get_db)):
    rule = get_or_404(db, RoutingRule, rule_id)
    return render(request, "admin/routing/form.html", r=rule, server=rule.server, preset_action=rule.action,
                  **_rule_ctx(db, rule, rule.server))


@router.post("/trading-setup/routing/{rule_id}")
def rule_update(request: Request, rule_id: int, form: RuleForm = Depends(),
                me: Staff = Depends(staff_perm("routing.manage")), db: Session = Depends(get_db)):
    rule = get_or_404(db, RoutingRule, rule_id)
    return attempt(request, lambda: svc.update_rule(db, me, rule, form.values(db, rule.server_id, rule.conditions)),
                   f"Routing rule {rule.name} saved on MT5.", f"/admin/trading-setup/routing/{rule_id}")


@router.post("/trading-setup/routing/{rule_id}/toggle")
def rule_toggle(request: Request, rule_id: int, me: Staff = Depends(staff_perm("routing.manage")),
                db: Session = Depends(get_db)):
    rule = get_or_404(db, RoutingRule, rule_id)
    state = "disabled" if rule.enabled else "enabled"
    return attempt(request, lambda: svc.update_rule(db, me, rule, {"enabled": not rule.enabled}),
                   f"Routing rule {rule.name} {state} on MT5.", f"/admin/trading-setup/routing?server={rule.server_id}")


@router.post("/trading-setup/routing/{rule_id}/move")
def rule_move(request: Request, rule_id: int, direction: str = Form(...),
              me: Staff = Depends(staff_perm("routing.manage")), db: Session = Depends(get_db)):
    rule = get_or_404(db, RoutingRule, rule_id)
    return attempt(request, lambda: svc.move_rule(db, me, rule, direction), None,
                   f"/admin/trading-setup/routing?server={rule.server_id}")


@router.post("/trading-setup/routing/{rule_id}/delete")
def rule_delete(request: Request, rule_id: int, me: Staff = Depends(staff_perm("routing.manage")),
                db: Session = Depends(get_db)):
    rule = get_or_404(db, RoutingRule, rule_id)
    name, server_id = rule.name, rule.server_id
    return attempt(request, lambda: svc.delete_rule(db, me, rule), f"Routing rule {name} deleted on MT5 and in the CRM.",
                   f"/admin/trading-setup/routing?server={server_id}")
