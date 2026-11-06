"""A gateway that forwards every call to the CRM's MT5 server.

The CRM works with exactly one MT5 server, so there is nothing to route: each call goes to
that server's gateway, looked up on every call so edits in the admin (address, password)
and the gateway rebuilds they cause are picked up. Kept so services written against
`routed()` work unchanged.
"""

from __future__ import annotations

from app.mt5.gateway import MT5Gateway


def _server_gateway() -> MT5Gateway:
    from app.mt5 import get_gateway

    return get_gateway()


def _delegate(name: str):
    def call(self, *args, **kwargs):
        return getattr(_server_gateway(), name)(*args, **kwargs)

    call.__name__ = name
    return call


def forget_logins() -> None:
    """Kept for callers of the multi-server version; there is no per-login cache any more."""


class RoutingGateway(MT5Gateway):
    # -- connection: the server's gateway manages its own
    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    @property
    def connected(self) -> bool:
        return True

    def ensure_connected(self) -> None:
        pass

    create_user = _delegate("create_user")
    get_user = _delegate("get_user")
    update_user = _delegate("update_user")
    delete_user = _delegate("delete_user")
    get_last_access = _delegate("get_last_access")
    archive_users = _delegate("archive_users")
    change_password = _delegate("change_password")
    check_password = _delegate("check_password")
    get_accounts = _delegate("get_accounts")
    get_account = _delegate("get_account")
    balance_operation = _delegate("balance_operation")
    get_deals = _delegate("get_deals")
    get_positions = _delegate("get_positions")
    get_orders = _delegate("get_orders")
    get_order_history = _delegate("get_order_history")
    delete_orders = _delegate("delete_orders")
    delete_deals = _delegate("delete_deals")
    rewrite_deal = _delegate("rewrite_deal")
    check_balance = _delegate("check_balance")
    server_time = _delegate("server_time")
    open_position = _delegate("open_position")
    close_position = _delegate("close_position")
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
