"""The names people see for codes stored in the database: statuses, types, audit actions and their details.

Templates never show a stored code as it is: they go through the `label` filter (`{{ x.status|label }}`), the
audit log through `action_label`, `entity_label` and `detail_rows`. A code missing from the tables below gets
a readable fallback ("bank_transfer" -> "Bank Transfer", "kyc" -> "KYC").
"""
from datetime import date, datetime
from decimal import Decimal

# Words always written in capitals.
ACRONYMS = {"kyc", "ib", "ibs", "pamm", "mt5", "ip", "ips", "id", "2fa", "api", "url", "usd", "usdt", "eur", "gbp",
            "sl", "tp", "nav", "ea", "smtp", "utm", "cidr", "csv", "pdf", "sms", "otp", "crm"}

# Codes whose name is not just the code with spaces (enum values and coded string columns).
VALUES = {
    "not_submitted": "Not submitted", "id_card": "ID card", "driving_license": "Driving licence",
    "proof_of_address": "Proof of address", "selfie": "Selfie",
    "to_mt5": "Wallet → MT5", "from_mt5": "MT5 → wallet", "ib_commission": "IB commission",
    "pamm_invest": "PAMM investment", "pamm_redeem": "PAMM withdrawal", "copy_fee": "Copy-trading fee",
    "performance_fee": "Performance fee", "management_fee": "Management fee",
    "copy_master": "Copy-trading master", "pamm_master": "PAMM master",
    "equity_ratio": "Equity ratio", "balance_ratio": "Balance ratio", "lot_multiplier": "Lot multiplier",
    "fixed_lot": "Fixed lot", "per_lot": "Per lot", "spread_share": "Share of spread",
    "commission_pct": "Share of commission", "manager": "MT5 Manager API", "simulator": "Simulator",
    "ok": "OK", "void": "Cancelled", "mismatch": "Amount mismatch", "redirected": "Sent to provider", "started": "Started",
    "crm": "Created in Flow", "mt5": "Imported from MT5", "gateway": "Gateway", "feed": "Data feed",
    "block": "Block", "allow": "Allow", "all": "Everywhere", "admin": "Back office", "portal": "Client portal",
    "website": "Website", "both": "Open and close", "open": "Open", "close": "Close",
    "staff": "Staff", "client": "Client", "system": "System", "queued": "Queued", "running": "Running",
    "mt5-import": "Imported from MT5", "website-contact": "Website contact form",
    "business development": "Business development",
}

# Audit log actions (app.core.audit). Dynamic ones are listed with every value they take.
ACTIONS = {
    "account.delete_on_mt5": "Account deleted on MT5", "account.group": "Account group changed",
    "account.leverage": "Leverage changed", "account.move_type": "Account moved between live and demo",
    "account.open": "Account opened", "account.password_reset": "Account password reset",
    "account.status": "Account status changed", "account.trading_enabled": "Trading enabled / disabled",
    "account_group.create": "Account group created", "account_group.delete": "Account group deleted",
    "account_group.update": "Account group updated",
    "auth.2fa_disabled": "Two-factor authentication turned off", "auth.2fa_enabled": "Two-factor authentication turned on",
    "auth.login": "Signed in", "auth.password_changed": "Password changed", "auth.password_reset": "Password reset",
    "auth.password_reset_requested": "Password reset requested",
    "automation.create": "Automation rule created", "automation.delete": "Automation rule deleted",
    "automation.enable": "Automation rule turned on", "automation.disable": "Automation rule turned off",
    "automation.update": "Automation rule updated", "automation.done": "Automation rule acted",
    "automation.failed": "Automation rule failed",
    "branding.update": "Branding changed",
    "campaign.active": "Campaign turned on / off", "campaign.create": "Campaign created",
    "client.assign": "Client assigned", "client.register": "Client registered", "client.update": "Client updated",
    "commission_plan.create": "Commission plan created",
    "copy.fee": "Copy-trading fee charged", "copy.strategy.create": "Strategy created",
    "copy.strategy.status": "Strategy status changed", "copy.subscribe": "Follower subscribed",
    "copy.unsubscribe": "Follower unsubscribed",
    "demo_cleanup.mt5_inactivity": "MT5 demo inactivity changed", "demo_cleanup.settings": "Demo clean-up settings changed",
    "demo_cleanup.archive": "Demo accounts archived", "demo_cleanup.delete": "Demo accounts deleted",
    "department.create": "Department created", "department.delete": "Department deleted",
    "department.update": "Department renamed",
    "ib.create": "IB created", "ib.payout": "IB commission paid out", "ib.update": "IB updated",
    "ip_rule.create": "IP rule created", "ip_rule.delete": "IP rule deleted", "ip_rule.expired": "IP rule expired",
    "ip_rule.reapply": "IP rules applied to MT5 again", "ip_rule.update": "IP rule updated",
    "kyc.review": "KYC document reviewed", "kyc.session_failed": "Verification failed to start",
    "kyc.session_start": "Verification started", "kyc.status": "KYC status changed", "kyc.upload": "KYC document uploaded",
    "kyc_method.activate": "KYC method turned on / off", "kyc_method.auto_approve_on": "KYC auto-approval turned on",
    "kyc_method.create": "KYC method created", "kyc_method.delete": "KYC method deleted",
    "kyc_method.new_callback_url": "KYC callback URL changed", "kyc_method.secret_change": "KYC method secret changed",
    "kyc_method.update": "KYC method updated",
    "lead.website_enquiry": "Website enquiry",
    "liquidity.gateway.create": "Gateway created", "liquidity.gateway.update": "Gateway updated",
    "liquidity.gateway.delete": "Gateway deleted", "liquidity.gateway.restart": "Gateways restarted",
    "liquidity.feed.create": "Data feed created", "liquidity.feed.update": "Data feed updated",
    "liquidity.feed.delete": "Data feed deleted", "liquidity.feed.restart": "Data feeds restarted",
    "mt5_server.create": "MT5 server added", "mt5_server.delete": "MT5 server deleted",
    "mt5_server.restart": "MT5 component restarted", "mt5_server.test": "MT5 connection tested",
    "mt5_server.update": "MT5 server updated",
    "mt5_sync.account_groups": "Account groups imported from MT5", "mt5_sync.liquidity": "Liquidity providers synced from MT5",
    "mt5_sync.routes": "Routing synced from MT5", "mt5_sync.symbols": "Symbols synced from MT5",
    "mt5_sync.trading_accounts": "Trading accounts imported from MT5",
    "pamm.fees": "PAMM fees charged", "pamm.fund.create": "PAMM fund created", "pamm.fund.status": "PAMM fund status changed",
    "pamm.invest": "PAMM investment", "pamm.withdraw": "PAMM withdrawal",
    "payment.amount_mismatch": "Payment amount mismatch", "payment.approve": "Payment approved",
    "payment.bonus_failed": "Bonus not credited", "payment.checkout": "Sent to payment provider",
    "payment.checkout_failed": "Payment provider unavailable", "payment.failed": "Payment failed",
    "payment.paid": "Payment confirmed by provider", "payment.reject": "Payment rejected",
    "payment.deposit.create": "Deposit requested", "payment.withdrawal.create": "Withdrawal requested",
    "payment_method.auto_credit_on": "Auto-credit turned on", "payment_method.create": "Payment method created",
    "payment_method.delete": "Payment method deleted", "payment_method.new_callback_url": "Payment callback URL changed",
    "payment_method.secret_change": "Payment method secret changed", "payment_method.update": "Payment method updated",
    "pricing.apply": "Prices applied to MT5", "pricing.settings": "Pricing settings changed",
    "role.create": "Role created", "role.update": "Role updated",
    "routing_rule.create": "Routing rule created", "routing_rule.delete": "Routing rule deleted",
    "routing_rule.move": "Routing rule moved", "routing_rule.update": "Routing rule updated",
    "server_health.disconnected": "MT5 component disconnected", "server_health.reconnected": "MT5 component reconnected",
    "staff.create": "Staff member created", "staff.reset_2fa": "Staff two-factor authentication reset",
    "staff.update": "Staff member updated",
    "staff_transfer.to_mt5": "Staff moved money wallet → MT5", "staff_transfer.from_mt5": "Staff moved money MT5 → wallet",
    "symbol.create": "Symbol created", "symbol.delete": "Symbol deleted", "symbol.update": "Symbol updated",
    "sync_job.cancel": "Trade import cancelled", "sync_job.create": "Trade import started",
    "trader_styles.rules": "Trader styles changed",
    "transfer.from_mt5": "Transfer MT5 → wallet", "transfer.to_mt5": "Transfer wallet → MT5",
    "website_prices.update": "Website prices changed",
}

# What the first part of an action is about: the audit log's filter.
ACTION_GROUPS = {
    "account": "Trading accounts", "account_group": "Account groups", "auth": "Sign-in and passwords",
    "automation": "Automation", "branding": "Branding", "campaign": "Campaigns", "client": "Clients",
    "commission_plan": "Commission plans", "copy": "Copy trading", "demo_cleanup": "Demo clean-up",
    "department": "Departments", "ib": "IBs", "ip_rule": "IP rules", "kyc": "KYC", "kyc_method": "KYC methods",
    "lead": "Leads", "liquidity": "Liquidity providers", "mt5_server": "MT5 servers", "mt5_sync": "Import from MT5",
    "pamm": "PAMM", "payment": "Payments", "referral_link": "Referral links", "payment_method": "Payment methods", "pricing": "Pricing", "role": "Roles",
    "routing_rule": "Routing", "server_health": "Server health", "staff": "Staff", "staff_transfer": "Staff transfers",
    "symbol": "Symbols", "sync_job": "Trade import", "trader_styles": "Trader styles", "transfer": "Transfers",
    "website_prices": "Website prices",
}

ENTITIES = {
    "client": "Client", "trading_account": "Trading account", "payment": "Payment", "mt5_server": "MT5 server",
    "liquidity_connection": "Liquidity provider", "automation_rule": "Automation rule", "role": "Role",
    "staff": "Staff member", "account_group": "Account group", "kyc_document": "KYC document",
    "kyc_method": "KYC method", "payment_method": "Payment method", "ip_rule": "IP rule", "symbol": "Symbol",
    "routing_rule": "Routing rule", "pamm_fund": "PAMM fund", "ib": "IB", "campaign": "Campaign", "commission_plan": "Commission plan",
    "department": "Department", "referral_link": "Referral link", "sync_job": "Trade import", "app_settings": "Settings",
    "copy_strategy": "Strategy", "copy_subscription": "Copy subscription",
}

# Keys in audit details.
FIELDS = {
    "client_id": "Client", "staff_id": "Staff member", "login": "Login", "logins": "Logins", "amount": "Amount",
    "deal": "MT5 deal", "ticket": "Ticket", "before": "Before", "after": "After", "am": "Account manager",
    "sales": "Sales / business development", "sales_staff_id": "Sales / business development",
    "account_manager_id": "Account manager", "referrer": "Referred by (client)", "utm_campaign": "UTM campaign",
    "email": "Email", "roles": "Roles", "permissions": "Permissions", "rule": "Rule", "target": "Applied to",
    "message": "Message", "action": "Action", "outcome": "Outcome", "status": "Status", "reason": "Reason",
    "method": "Method", "reference": "Reference", "component": "Component", "type": "Type", "address": "Address",
    "done": "Done", "failed": "Failed", "active_again": "Active again", "errors": "Errors", "remaining": "Remaining",
    "connections": "Connections", "group": "Group", "leverage": "Leverage", "investor": "Investor password",
    "enabled": "Enabled", "currency": "Currency", "fields": "Fields", "changed": "Changed", "name": "Name",
    "tag": "Tag", "code": "Referral code", "referral_link": "Referral link", "staff": "Staff",
    # synchronization results (Import from MT5 > History)
    "seen": "MT5 accounts", "created": "New", "updated": "Updated", "missing": "No longer on MT5",
    "removed": "Removed (no longer on MT5)", "clients_created": "New clients", "clients_linked": "Linked to clients",
    "conflicts": "Conflicts", "failed_groups": "Groups that failed", "account_groups": "Account groups",
    "groups": "MT5 groups", "new_deals": "New deals", "open_positions": "Open positions", "open_orders": "Open orders",
    "accounts": "Accounts updated", "symbols": "Symbols", "gateways": "Gateways", "feeds": "Data feeds",
    "routes": "Routing rules", "liquidity": "Liquidity providers", "pricing": "Pricing", "swaps": "Symbol swaps",
}


def _words(code: str) -> list[str]:
    return code.replace("_", " ").replace("-", " ").split()


def label(v) -> str:
    """A code shown to people: "bank_transfer" -> "Bank Transfer", "kyc" -> "KYC", "not_submitted" -> "Not
    submitted". Each word starts upper case and keeps the rest, so "USDT" stays "USDT"."""
    if v is None or v == "":
        return ""
    code = str(getattr(v, "value", v))
    if code in VALUES:
        return VALUES[code]
    return " ".join(w.upper() if w.lower() in ACRONYMS else w[:1].upper() + w[1:] for w in _words(code))


def _sentence(code: str) -> str:
    words = [w.upper() if w.lower() in ACRONYMS else w.lower() for w in _words(code)]
    return " ".join(words)[:1].upper() + " ".join(words)[1:] if words else ""


def action_label(action: str | None) -> str:
    if not action:
        return ""
    if action in ACTIONS:
        return ACTIONS[action]
    group, _, rest = action.partition(".")
    return f"{ACTION_GROUPS.get(group, label(group))}: {_sentence(rest)}" if rest else label(action)


def entity_label(entity: str | None) -> str:
    return ENTITIES.get(entity, label(entity)) if entity else ""


# The first part of a permission code (app.core.permissions).
PERMISSION_GROUPS = {
    "staff": "Staff", "clients": "Clients", "kyc": "KYC", "accounts": "Trading accounts",
    "account_groups": "Account groups", "mt5_servers": "MT5 servers", "mt5_health": "Server health",
    "symbols": "Symbols", "liquidity": "Liquidity providers", "routing": "Routing", "finance": "Finance",
    "trading": "Trading", "copy": "Copy trading and PAMM", "marketing": "Marketing", "automation": "Automation",
    "settings": "Settings", "security": "Security",
}


def permission_group(module: str | None) -> str:
    return PERMISSION_GROUPS.get(module, label(module)) if module else ""


def permission_label(code: str | None) -> str:
    """A permission code as a short name: "*" -> "Everything", "clients.*" -> "Clients: all",
    "clients.view_all" -> "Clients: view all"."""
    if not code:
        return ""
    if code == "*":
        return "Everything"
    module, _, rest = code.partition(".")
    if not rest:
        return permission_group(module)
    words = " ".join(w.upper() if w.lower() in ACRONYMS else w.lower() for w in _words(rest))
    return f"{permission_group(module)}: {'all' if rest == '*' else words}"


def field_label(key) -> str:
    key = str(key)
    if key in FIELDS:
        return FIELDS[key]
    if key.endswith("_id"):
        return _sentence(key[:-3])
    return _sentence(key)


MAX_LIST = 50
# Detail keys whose values are codes (shown through label) or staff ids (shown as the staff member's name).
CODE_KEYS = {"status", "kind", "type", "outcome", "action", "direction", "before", "after", "source", "scope"}
STAFF_KEYS = {"sales", "am", "sales_staff_id", "account_manager_id", "staff_id"}


def _value(v, key: str = "", staff_names: dict | None = None) -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if key in STAFF_KEYS and staff_names and v in staff_names:
        return staff_names[v]
    if isinstance(v, (int, float, Decimal)):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat(sep=" ", timespec="minutes") if isinstance(v, datetime) else v.isoformat()
    text = str(v)
    if text in VALUES or text in ACTIONS:
        return VALUES.get(text) or ACTIONS[text]
    if key in CODE_KEYS and text.replace("_", "").isalnum() and text.islower():
        return label(text)
    return text


def detail_rows(details, staff_names: dict | None = None, prefix: str = "", key: str = "") -> list[tuple[str, str]]:
    """Audit details as (name, value) rows: nested objects become "Before › Leverage", lists of values one
    comma-separated row (the first 50, then how many more). Staff ids show as names (staff_names: id -> name)."""
    rows: list[tuple[str, str]] = []
    if isinstance(details, dict):
        for k, v in details.items():
            name = f"{prefix} › {field_label(k)}" if prefix else field_label(k)
            if isinstance(v, dict) or (isinstance(v, list) and any(isinstance(x, (dict, list)) for x in v)):
                rows += detail_rows(v, staff_names, name, str(k)) if v else [(name, "—")]
            elif isinstance(v, list):
                shown = ", ".join(_value(x, str(k), staff_names) for x in v[:MAX_LIST]) or "—"
                rows.append((name, shown + (f" … and {len(v) - MAX_LIST} more" if len(v) > MAX_LIST else "")))
            else:
                rows.append((name, _value(v, str(k), staff_names)))
    elif isinstance(details, list):
        for i, v in enumerate(details, 1):
            name = f"{prefix} › {i}" if prefix else str(i)
            rows += (detail_rows(v, staff_names, name, key) if isinstance(v, (dict, list))
                     else [(name, _value(v, key, staff_names))])
    elif details not in (None, ""):
        rows.append((prefix or "Details", _value(details, key, staff_names)))
    return rows
