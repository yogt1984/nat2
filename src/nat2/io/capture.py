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
import errno
import signal
import time
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


class CaptureWriteFailed(RuntimeError):
    """The store could not be written. Distinct from a stall on purpose.

    A stall says "nothing arrived"; this says "something arrived and could not
    be kept". They call for opposite investigations, and conflating them sends
    the operator to the venue when the answer is the disk.
    """


class CaptureTaskFailed(RuntimeError):
    """One of the daemon's tasks died of something it did not expect.

    The third way to go quiet, and until it had a name it wore the first one's
    clothes: `run()` parks on `_stop`, so an exception in `_tape`, `_poller` or
    `_flusher` that is not an `OSError` killed only that task, was swallowed by
    `gather(..., return_exceptions=True)`, and surfaced `stall_s` later as
    "silent hl.trades" -- pointing the operator at the venue for a `KeyError`
    in our own parser.
    """


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
    status_errors: int = 0

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
        self.write_failure: str | None = None
        self.task_failure: str | None = None
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

    def _watched(self) -> list[str]:
        """The streams this process is the producer of.

        `nat2.liqmap` shares the store but is written by `nat2 cycle`, so a
        capture handed it as a `--stream` was killed every `stall_s` forever for
        not writing something it was never going to write.
        """
        return [name for name in self.writers
                if name == "hl.assetctxs" or (name in STREAMS and STREAMS[name].channel)]

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop)

        tasks = []

        def spawn(name: str, coro) -> None:
            task = asyncio.create_task(coro)
            task.add_done_callback(lambda t: self._task_done(name, t))
            tasks.append(task)

        spawn("flusher", self._flusher())
        if self.config.stall_s:
            spawn("stall watch", self._stall_watch())
        if "hl.assetctxs" in self.writers:
            spawn("poller", self._poller())
        if self._subscriptions():
            spawn("tape", self._tape())
        if self.on_status:
            spawn("status", self._status())
        try:
            await self._stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Closed first: shutdown is what appends the manifest entries, so a stalled
            # capture still leaves a complete, checksummed part behind.
            self.close()
        # Ordered by how specific the diagnosis is, because all three end as
        # "no records arrived": a store that cannot be written looks silent, and
        # so does a task that is no longer running. The silence is the symptom
        # rather than the diagnosis, so it is reported last.
        if self.write_failure:
            raise CaptureWriteFailed(self.write_failure)
        if self.task_failure:
            raise CaptureTaskFailed(self.task_failure)
        if self.stalled:
            raise CaptureStalled(self.stalled)

    def _task_done(self, name: str, task: asyncio.Task) -> None:
        """Stand down because a task died, naming it. Never raises.

        Cancellation is how shutdown works, and `run()` cancels every task in
        its `finally`, so only a task that dies while the daemon still expects
        to be running is a failure.
        """
        if task.cancelled() or self._stop.is_set() or self.task_failure:
            return
        exc = task.exception()
        if exc is None:
            return
        self.task_failure = (
            f"{name} died: {reason(exc)} ({exc}) -- exiting so the supervisor can restart; "
            "this is our own defect, not the venue's"
        )
        self.stop()

    def _write_failed(self, stream: str, exc: OSError) -> None:
        """Stand down because the store rejected a write. Never raises.

        The same shape as `_stall_watch`, and for the same reason: `run()`'s
        `finally` is what appends the manifest entries, so standing down
        through `stop()` leaves complete, checksummed parts behind where
        raising out of a task would strand them.

        First writer wins. A full disk fails every stream at once, and the
        first one to notice is the one that has something useful to say.
        """
        if self.write_failure:
            return
        code = errno.errorcode.get(exc.errno, exc.errno) if exc.errno else type(exc).__name__
        self.write_failure = (
            f"cannot write {stream} to {self.config.root}: {code} ({exc}) -- exiting so the "
            "supervisor can restart; the tape is a hole either way, but the disk is the cause"
        )
        self.stop()

    def stop(self) -> None:
        self._stop.set()
        if self.ws:
            self.ws.stop()

    def close(self) -> None:
        """Close every writer, even if one of them cannot be.

        Unguarded, the first store that fails to manifest prevented every later
        one from manifesting -- and the OSError, raised out of `run()`'s
        `finally`, masked the stand-down entirely and landed as a traceback.
        Closing a part needs the disk twice (the frame, then the manifest
        line), so this is exactly the moment a full disk bites.
        """
        for name, writer in self.writers.items():
            try:
                writer.close()
            except OSError as exc:
                self._write_failed(name, exc)

    async def _tape(self) -> None:
        self.ws = WsClient(self._subscriptions(), testnet=self.config.testnet)
        async for channel, data, t_ingest in self.ws.stream():
            stream = CHANNEL_TO_STREAM.get(channel)
            if stream is None or stream not in self.writers:
                continue
            spec = STREAMS[stream]
            try:
                self.writers[stream].write(data, spec.event_time(data), t_ingest)
            except OSError as exc:
                self._write_failed(stream, exc)
                return
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
                except Exception as exc:  # noqa: BLE001 - attributed below
                    self.stats.poll_errors += 1
                    self.stats.poll_failures[reason(exc)] += 1
                else:
                    # Split from the fetch deliberately. A venue error is
                    # something to tolerate and count; a store that will not
                    # take the record is not -- and inside one broad handler a
                    # full disk became a silent no-op that never exited.
                    try:
                        writer.write(payload, None, now_ns())
                    except OSError as exc:
                        self._write_failed("hl.assetctxs", exc)
                        return
                    self.stats.bump("hl.assetctxs")
                    self.stats.polls += 1
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

        **Monotonic, not wall clock.** `asyncio.sleep` advances on `time.monotonic()`,
        which does not tick across s2idle, so measuring silence with `now_ns()` made every
        suspend longer than `stall_s` a guaranteed kill on resume -- three of them between
        09-13 and 09-14, one reporting an age of 95,360 s for a process that had been
        asleep, not silent. Nothing could have been written while the host slept, and
        nothing downstream wants the process dead for it: the hole belongs to `gapwatch`,
        which measures the tape rather than the daemon.
        """
        started, last, seen = time.monotonic(), {}, {}
        waited, excused, bounced = self.budget.wait_s, 0.0, set()
        while not self._stop.is_set():
            if self.write_failure or self.task_failure:
                # Same 30 s period as the flusher, and created after it, so
                # without this the "silent ..." message overwrites the one that
                # names the actual cause.
                return
            await asyncio.sleep(min(self.config.stall_s / 5, 30.0))
            now = time.monotonic()
            for stream in self.writers:
                written = self.stats.written.get(stream, 0)
                if written > seen.get(stream, 0):
                    seen[stream], last[stream] = written, now
            watched = self._watched()
            silent = {s: now - last.get(s, started) for s in watched
                      if now - last.get(s, started) > self.config.stall_s}
            # Being held off by our own rate limiter is not the venue going quiet. The
            # 6-hourly registry sweep reserves 60% of the per-IP budget and the poller
            # queues behind it; three non-outage kills landed inside one of those windows,
            # every one of them printing "no errors reported" because there had been no
            # error. `wait_s` rises only when *this* process was denied, so it is the
            # precise signal -- and it is spent from a fixed allowance of 2 x stall_s, so
            # an outage that merely coincides with budget pressure is still caught, and a
            # process cannot excuse itself indefinitely the way the 2026-08-22 one did.
            if "hl.assetctxs" in silent and self.budget.wait_s > waited \
                    and excused < 2 * self.config.stall_s:
                excused += silent.pop("hl.assetctxs")
                last["hl.assetctxs"] = now
            waited = self.budget.wait_s
            # Try the cheap repair before the expensive one. A stream that went quiet
            # while its siblings kept flowing is the shape of a subscription lost across
            # a reconnect -- the socket is healthy and carrying the others -- and killing
            # the process is a costly way to re-send a subscribe: a new part, a 10 s gap,
            # a universe resolve and a manifest rescan. Bounced once per stream; if it is
            # still silent `stall_s` later, stand down as before. Never when *everything*
            # is silent, which is an outage and has nothing to re-subscribe to.
            resubscribe = [s for s in silent if s not in bounced and STREAMS[s].channel]
            if silent and self.ws and len(silent) < len(watched) \
                    and len(resubscribe) == len(silent):
                bounced.update(resubscribe)
                self.ws.cycle()
                for stream in resubscribe:
                    last[stream] = now
                continue
            if silent:
                self.stalled = (
                    "silent " + ", ".join(f"{s} {age:.0f}s" for s, age in sorted(silent.items()))
                    + f" ({self.why() or 'no errors reported'})"
                    + (" after a resubscribe" if bounced & set(silent) else "")
                    + " -- exiting so the supervisor can restart; the tape is a hole either way")
                self.stop()
                return

    async def _flusher(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(FLUSH_INTERVAL_S)
            for name, writer in self.writers.items():
                try:
                    writer.flush()
                except OSError as exc:
                    # Where a full disk actually shows up: zstd buffers ~128 KB,
                    # so a per-record `write()` never reaches the fd and the
                    # flush is the first call that can fail. This tick is the
                    # detection path, not a fallback for it.
                    self._write_failed(name, exc)
                    return

    async def _status(self) -> None:
        """The heartbeat, and the one task whose death is not fatal.

        Every other task either writes the tape or guards it, so `_task_done`
        stands the daemon down when one dies. This one writes a line to a
        console: journald restarting under us, or any other broken pipe, must
        not take the capture with it. Counted, so the silence is not free.
        """
        while not self._stop.is_set():
            await asyncio.sleep(self.config.status_interval_s)
            try:
                self.on_status(self)
            except Exception:  # noqa: BLE001 - a status line is not the tape
                self.stats.status_errors += 1

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
