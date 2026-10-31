"""The local demo server's background trader must never trade on a real MT5 server."""

import os
import threading
from unittest import mock

import pytest


@pytest.fixture
def devserver():
    saved = dict(os.environ)  # importing devserver sets demo defaults in the environment
    try:
        from app import devserver
        yield devserver
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_demo_trader_never_trades_on_a_real_server(devserver):
    stop = threading.Event()
    real = mock.MagicMock()

    def no_simulator():
        stop.set()  # one round is enough
        return None

    with mock.patch.object(devserver, "simulator", side_effect=no_simulator), \
            mock.patch("app.mt5.routed", return_value=real), mock.patch("app.mt5.get_gateway", return_value=real):
        devserver.demo_trader(stop, every=0)
    assert not real.open_position.called and not real.close_position.called and not real.place_order.called

