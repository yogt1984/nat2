"""A Hyperliquid on loopback that can be broken on purpose.

Every failure this project has actually suffered was discovered in production
and then argued about from a journal: a socket that answered pings but sent no
data for 5.1 hours, a resolver that was down at start-up so the daemon died
before it opened a writer, a 429 nobody had budgeted for. None of them had a
test, because there was nowhere to reproduce them -- the only fake in the suite
was `httpx.MockTransport`, which cannot express a websocket at all.

This is that venue. It drives the *real* `WsClient`, `InfoClient` and `Capture`
over 127.0.0.1, so a scenario exercises the reconnect loop, the stall watchdog
and the weight accounting as shipped rather than a mock of them. Both clients
take their URL as an instance attribute set in `__init__`, so pointing them here
needs no patching of module constants.

Nothing here reaches the network, and every server binds port 0.
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from websockets.asyncio.server import serve

# Wide enough that a normal machine cannot miss it, short enough that a hung
# scenario fails the suite rather than hanging it.
SETTLE_S = 0.05


def trade(tid: int, coin: str = "BTC", t_ms: int = 1_755_000_000_000, px: str = "100.0") -> dict:
    """One print in the shape `_max_trade_time` and the WORM writer expect."""
    return {"coin": coin, "side": "B", "px": px, "sz": "0.01",
            "time": t_ms + tid, "hash": f"0x{tid:064x}", "tid": tid,
            "users": [f"0x{tid:040x}", f"0x{tid + 1:040x}"]}


class FakeVenue:
    """The websocket half. One behaviour per instance, chosen at construction.

    behaviour:
      "stream"  -- push `batch` prints every `interval_s`, forever
      "silent"  -- accept, subscribe, answer pings, and never send data again.
                   The 2026-08-22 outage: alive, connected, writing nothing.
      "replay"  -- push `batch` prints, close, and replay the same tids on the
                   next connection before continuing. HL resends a backlog after
                   a reconnect and nothing in the store dedupes on write.
    """

    def __init__(self, behaviour: str = "stream", batch: int = 3,
                 interval_s: float = 0.02, coin: str = "BTC"):
        self.behaviour = behaviour
        self.batch = batch
        self.interval_s = interval_s
        self.coin = coin
        self.connections = 0
        self.subscriptions: list[dict] = []
        self.pings = 0
        self.frames_sent = 0
        self.tids_sent: list[int] = []
        self.url = ""
        self._server = None
        self._next_tid = 0

    async def _read(self, connection) -> None:
        """Consume what the client sends: subscribe frames, and app-level pings."""
        async for raw in connection:
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if message.get("method") == "ping":
                self.pings += 1
                await connection.send(json.dumps({"channel": "pong"}))
            elif message.get("method") == "subscribe":
                self.subscriptions.append(message.get("subscription", {}))
                await connection.send(json.dumps(
                    {"channel": "subscriptionResponse", "data": message}))

    async def _send(self, connection, tids: list[int]) -> None:
        await connection.send(json.dumps({
            "channel": "trades",
            "data": [trade(tid, self.coin) for tid in tids],
        }))
        self.frames_sent += 1
        self.tids_sent.extend(tids)

    async def _handler(self, connection) -> None:
        self.connections += 1
        nth = self.connections
        reader = asyncio.create_task(self._read(connection))
        try:
            await asyncio.sleep(SETTLE_S)          # let the subscribes land first
            if self.behaviour == "silent":
                # Parked on the connection, not on a bare Future: the server has to be
                # able to wake this handler when it shuts the socket, or closing the
                # venue hangs the very test that is proving a hang.
                await connection.wait_closed()     # connected, and nothing more
            elif self.behaviour == "replay":
                first = list(range(self.batch))
                await self._send(connection, first)
                if nth > 1:                        # the backlog, a second time
                    fresh = [self.batch + nth, self.batch + nth + 100]
                    await self._send(connection, fresh)
                return                             # close: the client reconnects
            else:
                while True:
                    tids = [self._next_tid + i for i in range(self.batch)]
                    self._next_tid += self.batch
                    await self._send(connection, tids)
                    await asyncio.sleep(self.interval_s)
        except Exception:                          # noqa: BLE001 - a closed peer is normal
            pass
        finally:
            reader.cancel()

    async def start(self) -> "FakeVenue":
        self._server = await serve(self._handler, "127.0.0.1", 0)
        port = next(iter(self._server.sockets)).getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


@asynccontextmanager
async def fake_venue(**kwargs):
    venue = await FakeVenue(**kwargs).start()
    try:
        yield venue
    finally:
        await venue.close()


class FakeInfo:
    """The REST half: a scripted `/info` endpoint on a background thread.

    `statuses` is consumed one entry per request; when it runs out every further
    request succeeds. An entry is a status code, or (status, {header: value}).
    """

    def __init__(self, body=None, statuses=None):
        self.body = body if body is not None else {"universe": [{"name": "BTC"}]}
        self.statuses = list(statuses or [])
        self.calls = 0
        self.requested: list[dict] = []
        self.url = ""
        self._httpd = None
        self._thread = None

    def _next_status(self):
        return self.statuses.pop(0) if self.statuses else 200

    def start(self) -> "FakeInfo":
        venue = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):                      # noqa: N802 - BaseHTTPRequestHandler's name
                length = int(self.headers.get("content-length", 0))
                raw = self.rfile.read(length)
                venue.calls += 1
                try:
                    venue.requested.append(json.loads(raw))
                except json.JSONDecodeError:
                    venue.requested.append({})

                entry = venue._next_status()
                status, headers = entry if isinstance(entry, tuple) else (entry, {})
                payload = json.dumps(venue.body).encode()
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):           # keep pytest output readable
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/info" % self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()


@contextmanager
def fake_info(**kwargs):
    venue = FakeInfo(**kwargs).start()
    try:
        yield venue
    finally:
        venue.close()


def closed_port() -> int:
    """A port with nothing behind it -- the shape of a venue that is simply down."""
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
