"""Live prices for the website and client area, streamed over a WebSocket (/ws/quotes).

One broadcaster reads the quotes (market.quotes, cached) every INTERVAL seconds while anyone is connected and
pushes them to every open socket only when something changed, so MT5 sees the same load for one visitor or
ten thousand. /quotes.json serves the same data for browsers or proxies that cannot open a WebSocket.
"""

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.core.deps import client_ip
from app.services import ip_rules, market

log = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)

INTERVAL = 0.5  # seconds between reads while clients are connected
MAX_CLIENTS = 5000


def snapshot() -> dict:
    return {"quotes": [{k: q[k] for k in ("symbol", "bid", "ask", "spread")} for q in market.quotes()]}


class QuoteHub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.task: asyncio.Task | None = None
        self.last: str = ""

    async def join(self, ws: WebSocket) -> None:
        self.clients.add(ws)
        if self.last:
            await ws.send_text(self.last)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    def leave(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def _run(self) -> None:
        while self.clients:
            try:
                payload = json.dumps(await run_in_threadpool(snapshot), separators=(",", ":"))
            except Exception:  # noqa: BLE001 - keep streaming; the next read may work
                log.exception("reading quotes for the live stream failed")
                payload = self.last
            if payload != self.last and payload:
                self.last = payload
                await asyncio.gather(*(self._send(ws, payload) for ws in list(self.clients)))
            await asyncio.sleep(INTERVAL)
        self.last = ""  # nobody listening: the next visitor starts from a fresh read

    async def _send(self, ws: WebSocket, payload: str) -> None:
        try:
            await asyncio.wait_for(ws.send_text(payload), timeout=5)
        except Exception:  # noqa: BLE001 - a slow or closed socket is dropped
            self.leave(ws)


hub = QuoteHub()


@router.websocket("/ws/quotes")
async def quotes_socket(ws: WebSocket):
    # Middleware for IP rules is HTTP-only, so the website's rules are checked here.
    if await run_in_threadpool(ip_rules.check, client_ip(ws), "/") is not None or len(hub.clients) >= MAX_CLIENTS:
        await ws.close(code=1008)
        return
    await ws.accept()
    await hub.join(ws)
    try:
        while True:  # the browser never sends anything; this only notices the disconnect
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        hub.leave(ws)


@router.get("/quotes.json")
def quotes_json():
    return JSONResponse(snapshot(), headers={"Cache-Control": "no-store"})
