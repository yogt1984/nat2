"""HL websocket -- one client, reconnecting, yielding stamped envelopes.

`t_ingest` is taken the instant a frame is read, before any parsing, so it
measures when we could have known the message rather than when we got round
to understanding it.  Reconnects are counted and surfaced: a gap in the tape
is a fact the feed audit needs, not an error to swallow.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import websockets

from nat2.core.clock import now_ns
from nat2.hl.schemas import WS_URL, WS_URL_TESTNET

PING_INTERVAL_S = 45.0


@dataclass
class Subscription:
    type: str
    coin: str | None = None

    def message(self) -> dict:
        sub: dict = {"type": self.type}
        if self.coin:
            sub["coin"] = self.coin
        return {"method": "subscribe", "subscription": sub}


@dataclass
class WsStats:
    frames: int = 0
    reconnects: int = 0
    last_frame_ns: int = 0
    errors: list[str] = field(default_factory=list)


class WsClient:
    def __init__(self, subs: list[Subscription], testnet: bool = False):
        self.url = WS_URL_TESTNET if testnet else WS_URL
        self.subs = subs
        self.stats = WsStats()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def stream(self):
        """Yield ``(channel, data, t_ingest)`` until stopped.

        Backoff is capped at 30s: HL outages are usually short, and a long
        backoff turns a blip into an hour-shaped hole in the store.
        """
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, max_size=None) as ws:
                    for sub in self.subs:
                        await ws.send(json.dumps(sub.message()))
                    backoff = 1.0
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        async for frame in ws:
                            t_ingest = now_ns()
                            if self._stop.is_set():
                                break
                            self.stats.frames += 1
                            self.stats.last_frame_ns = t_ingest
                            try:
                                msg = json.loads(frame)
                            except json.JSONDecodeError:
                                self.stats.errors.append("undecodable frame")
                                continue
                            channel = msg.get("channel")
                            if channel in (None, "pong", "subscriptionResponse"):
                                continue
                            yield channel, msg.get("data"), t_ingest
                    finally:
                        pinger.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                self.stats.reconnects += 1
                self.stats.errors.append(f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _ping(self, ws) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            await ws.send(json.dumps({"method": "ping"}))
