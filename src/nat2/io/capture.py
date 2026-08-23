"""The capture daemon.

Websocket tape plus a polled cross-section, both landing in the WORM store
with dual timestamps.  This process is the project's real start date:
point-in-time series cannot be recovered later from any source that revises
its history, so everything downstream is bounded by how long this has been
running.

Shutdown closes every writer, which is what appends their manifest entries.
A hard kill therefore leaves an unmanifested file -- deliberately visible to
`nat2 audit feed` rather than silently forgiven.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from nat2.core.clock import NS, now_ns
from nat2.core.errors import reason, top_reasons
from nat2.hl.info import InfoClient
from nat2.hl.ratelimit import WeightBudget
from nat2.hl.schemas import CHANNEL_TO_STREAM, STREAMS
from nat2.hl.ws import Subscription, WsClient
from nat2.io.worm import WormWriter

FLUSH_INTERVAL_S = 30.0
# A capture that cannot resolve HL's host stays *alive*: the websocket reconnects forever
# and the poller swallows every error, so systemd sees an active unit and `Restart=always`
# never fires. On 2026-08-22 that cost 5.1 hours of tape in one stretch (gaierror x11 per
# minute, `trades` frozen at 22944) and 408 minutes over 30 hours. A process that has
# written nothing for this long is not running, so it exits and lets systemd restart it --
# safe by construction, because a restart opens a new WORM part.
STALL_S = 300.0


class CaptureStalled(RuntimeError):
    pass


@dataclass
class CaptureConfig:
    root: Path
    coins: list[str]
    streams: list[str]
    testnet: bool = False
    poll_interval_s: float = 10.0
    status_interval_s: float = 60.0
    stall_s: float = STALL_S        # 0 disables the watchdog


@dataclass
class CaptureStats:
    started_ns: int = field(default_factory=now_ns)
    written: dict[str, int] = field(default_factory=dict)
    polls: int = 0
    poll_errors: int = 0
    # Why, not just how many: 2,298 identical failures are one bug, 2,298
    # different ones are another, and a bare counter cannot tell them apart.
    poll_failures: Counter = field(default_factory=Counter)

    def bump(self, stream: str) -> None:
        self.written[stream] = self.written.get(stream, 0) + 1


class Capture:
    def __init__(self, config: CaptureConfig, on_status=None, budget=None):
        self.config = config
        self.stats = CaptureStats()
        self.budget = budget or WeightBudget()
        self.on_status = on_status
        self.writers: dict[str, WormWriter] = {
            name: WormWriter(config.root, name) for name in config.streams
        }
        self.ws: WsClient | None = None
        self.stalled: str | None = None
        self._stop = asyncio.Event()

    def _subscriptions(self) -> list[Subscription]:
        subs = []
        for name in self.config.streams:
            spec = STREAMS[name]
            if not spec.channel:
                continue
            if spec.per_coin:
                subs += [Subscription(spec.sub_type, coin) for coin in self.config.coins]
            else:
                subs.append(Subscription(spec.sub_type))
        return subs

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop)

        tasks = [asyncio.create_task(self._flusher())]
        if self.config.stall_s:
            tasks.append(asyncio.create_task(self._stall_watch()))
        if "hl.assetctxs" in self.writers:
            tasks.append(asyncio.create_task(self._poller()))
        if self._subscriptions():
            tasks.append(asyncio.create_task(self._tape()))
        if self.on_status:
            tasks.append(asyncio.create_task(self._status()))
        try:
            await self._stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Closed first: shutdown is what appends the manifest entries, so a stalled
            # capture still leaves a complete, checksummed part behind.
            self.close()
        if self.stalled:
            raise CaptureStalled(self.stalled)

    def stop(self) -> None:
        self._stop.set()
        if self.ws:
            self.ws.stop()

    def close(self) -> None:
        for writer in self.writers.values():
            writer.close()

    async def _tape(self) -> None:
        self.ws = WsClient(self._subscriptions(), testnet=self.config.testnet)
        async for channel, data, t_ingest in self.ws.stream():
            stream = CHANNEL_TO_STREAM.get(channel)
            if stream is None or stream not in self.writers:
                continue
            spec = STREAMS[stream]
            self.writers[stream].write(data, spec.event_time(data), t_ingest)
            self.stats.bump(stream)

    async def _poller(self) -> None:
        """metaAndAssetCtxs: mark, oracle, funding, OI for the whole universe.

        One request per cycle for every coin, which is why this is polled
        rather than subscribed per-coin -- it is cheaper on the weight budget
        and yields a coherent cross-section at a single ingest time.
        """
        info = InfoClient(self.budget, testnet=self.config.testnet)
        writer = self.writers["hl.assetctxs"]
        try:
            while not self._stop.is_set():
                try:
                    payload = await info.meta_and_asset_ctxs()
                    writer.write(payload, None, now_ns())
                    self.stats.bump("hl.assetctxs")
                    self.stats.polls += 1
                except Exception as exc:  # noqa: BLE001 - attributed below
                    self.stats.poll_errors += 1
                    self.stats.poll_failures[reason(exc)] += 1
                await asyncio.sleep(self.config.poll_interval_s)
        finally:
            await info.aclose()

    async def _stall_watch(self) -> None:
        """Exit if any stream we are supposed to be filling goes silent for `stall_s`.

        **Per stream, not in total.** In the 2026-08-22 outage the poller recovered while
        the websocket stayed dead -- `assetctxs` ticked 117 -> 118 with `trades` frozen at
        22944 -- so a watchdog on the sum would have reset its own clock and stayed blind.

        The clock starts at start-up, so a daemon that comes up during an outage and never
        connects also exits: that is precisely the observed case, one process alive for 5.4
        hours writing nothing. Assumes each subscribed stream is naturally sub-minute
        (18 coins of trades, a 10 s poll); a deliberately thin capture should raise
        `--stall-s` rather than be killed for being quiet.
        """
        started, last, seen = now_ns(), {}, {}
        while not self._stop.is_set():
            await asyncio.sleep(min(self.config.stall_s / 5, 30.0))
            now = now_ns()
            for stream in self.writers:
                written = self.stats.written.get(stream, 0)
                if written > seen.get(stream, 0):
                    seen[stream], last[stream] = written, now
            silent = {s: (now - last.get(s, started)) / NS for s in self.writers
                      if now - last.get(s, started) > self.config.stall_s * NS}
            if silent:
                self.stalled = (
                    "silent " + ", ".join(f"{s} {age:.0f}s" for s, age in sorted(silent.items()))
                    + f" ({self.why() or 'no errors reported'}) -- exiting so the supervisor can "
                      "restart; the tape is a hole either way")
                self.stop()
                return

    async def _flusher(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(FLUSH_INTERVAL_S)
            for writer in self.writers.values():
                writer.flush()

    async def _status(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.config.status_interval_s)
            self.on_status(self)

    def why(self) -> str:
        """The dominant failure reasons, for the status line."""
        parts = []
        if self.stats.poll_failures:
            parts.append(f"poll: {top_reasons(self.stats.poll_failures)}")
        if self.ws and self.ws.stats.reasons:
            parts.append(f"ws: {top_reasons(self.ws.stats.reasons)}")
        return " | ".join(parts)

    @property
    def uptime_s(self) -> float:
        return (now_ns() - self.stats.started_ns) / NS
