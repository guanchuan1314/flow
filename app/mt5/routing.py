"""A gateway that forwards every call to the MT5 server of the account it is about.

A login belongs to its server, and two servers can have the same one. So code that has the account says which server:
gw.on(account.server_id).balance_operation(...). A call with only a login goes to the one server Flow knows it on, and
a call with several logins is split between their servers and the answers put together. A login on two servers, or one
Flow does not know while more than one server is set up, is an error: a guess could move money on the wrong account.

A call about the server itself (groups, symbols, quotes) goes to the default server, the website's price feed. To work
on another one, use its gateway: mt5.get_gateway(server_id), or services.mt5_calls. Calls by ticket cannot be routed
(a ticket number means something different on each server) and must use the server's gateway too.

The server's gateway is looked up on every call, so edits in the back-office are picked up.
"""

from __future__ import annotations

from app.mt5.gateway import MT5Error, MT5Gateway


def _servers_of(logins: list[int]) -> dict[int | None, list[int]]:
    """server id -> those of the logins on it (None: Flow does not know them)."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import TradingAccount

    with SessionLocal() as db:
        rows = db.execute(select(TradingAccount.login, TradingAccount.server_id)
                          .where(TradingAccount.login.in_(logins))).all()
    known: dict[int, list[int]] = {}
    for login, server_id in rows:
        known.setdefault(login, []).append(server_id)
    found: dict[int | None, list[int]] = {}
    for login in logins:
        servers = known.get(login, [None])
        if len(servers) > 1:
            raise MT5Error(f"login {login} is on more than one MT5 server, so this call has to say which")
        found.setdefault(servers[0], []).append(login)
    return found


def _gateway_of(server_id: int | None, logins: list[int]) -> MT5Gateway:
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.models import MT5Server
    from app.mt5 import get_gateway

    if server_id is not None:
        return get_gateway(server_id)
    with SessionLocal() as db:  # with one server there is nothing to get wrong
        servers = db.scalar(select(func.count()).select_from(MT5Server).where(MT5Server.enabled.is_(True)))
    if servers and servers > 1:
        raise MT5Error(f"trading account {logins[0]} is not known to the CRM, so its MT5 server is not known")
    return get_gateway()


def _server_gateway() -> MT5Gateway:
    from app.mt5 import get_gateway

    return get_gateway()


def _delegate(name: str):
    def call(self, *args, **kwargs):
        return getattr(_server_gateway(), name)(*args, **kwargs)

    call.__name__ = name
    return call


def _by_login(name: str):
    """Calls whose first argument is a login."""
    def call(self, login, *args, **kwargs):
        server_id, logins = next(iter(_servers_of([login]).items()))
        return getattr(_gateway_of(server_id, logins), name)(login, *args, **kwargs)

    call.__name__ = name
    return call


def _by_logins(name: str):
    """Calls whose first argument is a list of logins, answered with a list or a dict."""
    def call(self, logins, *args, **kwargs):
        answers = [getattr(_gateway_of(server_id, part), name)(part, *args, **kwargs)
                   for server_id, part in _servers_of(list(logins)).items()]
        if answers and isinstance(answers[0], dict):
            return {key: value for answer in answers for key, value in answer.items()}
        return [row for answer in answers for row in answer]

    call.__name__ = name
    return call


def _no_route(name: str):
    def call(self, *args, **kwargs):
        raise MT5Error(f"{name} needs the gateway of a server: ticket numbers are per server")

    call.__name__ = name
    return call


def forget_logins() -> None:
    """Kept for callers of the multi-server version; there is no per-login cache any more."""


class RoutingGateway(MT5Gateway):
    # -- connection: each server's gateway manages its own
    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    @property
    def connected(self) -> bool:
        return True

    def ensure_connected(self) -> None:
        pass

    def on(self, server_id: int) -> MT5Gateway:
        from app.mt5 import get_gateway

        return get_gateway(server_id)

    create_user = _delegate("create_user")
    get_user = _by_login("get_user")
    update_user = _by_login("update_user")
    delete_user = _by_login("delete_user")
    get_last_access = _delegate("get_last_access")
    archive_users = _by_logins("archive_users")
    change_password = _by_login("change_password")
    check_password = _by_login("check_password")
    get_accounts = _by_logins("get_accounts")
    get_account = _by_login("get_account")
    balance_operation = _by_login("balance_operation")
    get_deals = _by_logins("get_deals")
    get_positions = _by_logins("get_positions")
    get_orders = _by_logins("get_orders")
    get_order_history = _by_logins("get_order_history")
    delete_orders = _no_route("delete_orders")
    delete_deals = _no_route("delete_deals")
    rewrite_deal = _no_route("rewrite_deal")
    check_balance = _by_login("check_balance")
    server_time = _delegate("server_time")
    open_position = _by_login("open_position")
    close_position = _by_login("close_position")
    get_groups = _delegate("get_groups")
    get_users_in_group = _delegate("get_users_in_group")
    get_group = _delegate("get_group")
    create_group = _delegate("create_group")
    update_group = _delegate("update_group")
    set_group_pricing = _delegate("set_group_pricing")
    delete_group = _delegate("delete_group")
    get_firewall = _delegate("get_firewall")
    put_firewall_rule = _delegate("put_firewall_rule")
    delete_firewall_rule = _delegate("delete_firewall_rule")
    get_symbols = _delegate("get_symbols")
    put_symbol = _delegate("put_symbol")
    delete_symbol = _delegate("delete_symbol")
    get_links = _delegate("get_links")
    put_link = _delegate("put_link")
    delete_link = _delegate("delete_link")
    get_link_modules = _delegate("get_link_modules")
    get_routes = _delegate("get_routes")
    put_route = _delegate("put_route")
    delete_route = _delegate("delete_route")
    move_route = _delegate("move_route")
    get_server_health = _delegate("get_server_health")
    restart_component = _delegate("restart_component")
    restart_links = _delegate("restart_links")
    get_quotes = _delegate("get_quotes")
    subscribe_deals = _delegate("subscribe_deals")
    unsubscribe_deals = _delegate("unsubscribe_deals")


_routed = RoutingGateway()


def routed() -> RoutingGateway:
    return _routed
