"""The recurring cycle that resolves the population question.

`gate map`'s predictive check needs liquidations that happened *after* a
snapshot. One snapshot followed by one scan cannot supply that -- a wallet
liquidated before we looked no longer holds the position, so there was nothing
to predict. Only a repeating snapshot-then-observe cycle separates "different
populations" from "we looked too late".

Every pass appends its overlap reading to the ledger, so
`mapped_notional_frac` becomes a series that either climbs off zero or does
not. That series is the answer, and it accrues in wall-clock time rather than
by thinking harder about it.

Runs alongside the capture daemon and shares its weight budget, which is why
the sweep interval is measured in hours: one sweep costs about five minutes of
the entire IP allowance.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from dataclasses import dataclass
from pathlib import Path

from nat2.core.clock import NS
from nat2.core.registry import Registry
from nat2.core.schedule import Job, JobStore
from nat2.features.liqmath import effective
from nat2.features.liquidations import population_overlap
from nat2.hl.info import InfoClient
from nat2.io.liqscan import candidate_observers, scan
from nat2.io.snapshot import sweep
from nat2.ledger.chain import Ledger

TICK_S = 20.0


@dataclass
class CycleConfig:
    registry_path: Path
    ledger_path: Path
    snapshot_interval_ns: int
    scan_interval_ns: int
    replay_interval_ns: int = 5 * 60 * NS
    raw_root: Path = Path("data/raw")
    observers: int = 40
    wallet_limit: int = 0
    testnet: bool = False


class Cycle:
    def __init__(self, config: CycleConfig, budget, on_event=None):
        self.config = config
        self.registry = Registry(config.registry_path)
        self.ledger = Ledger(config.ledger_path)
        self.budget = budget
        self.on_event = on_event or (lambda *_: None)
        self.store = JobStore(self.registry)
        self.jobs = {
            # Replay first and often: it is free (no API weight, only captured
            # tape) and it is what keeps the map from going stale between the
            # expensive sweeps.
            "replay": self.store.load("replay", config.replay_interval_ns),
            "snapshot": self.store.load("snapshot", config.snapshot_interval_ns),
            "scan": self.store.load("scan", config.scan_interval_ns),
        }
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run_once(self, force: bool = False) -> dict:
        """One pass. Returns what ran; anything not due is simply not run."""
        results: dict = {}
        for name in ("replay", "snapshot", "scan"):
            job = self.jobs[name]
            if not force and not job.due():
                continue
            job.started()
            ok = True
            try:
                results[name] = await getattr(self, f"_run_{name}")()
            except Exception as exc:  # noqa: BLE001 - a failed pass is a hole
                ok = False
                results[name] = {"error": f"{type(exc).__name__}: {exc}"}
                self.on_event(name, results[name])
            finally:
                job.finished(ok)
                self.store.save(job)
        if "scan" in results:
            results["coverage"] = self._measure_overlap()
        return results

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop)
        while not self._stop.is_set():
            results = await self.run_once()
            for name, result in results.items():
                self.on_event(name, result)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_S)

    async def _run_replay(self) -> dict:
        from nat2.io.replay import replay

        return replay(self.registry, self.config.raw_root)

    async def _run_snapshot(self) -> dict:
        addresses = self.registry.addresses(limit=self.config.wallet_limit or None)
        if not addresses:
            return {"skipped": "registry is empty"}
        info = InfoClient(self.budget, testnet=self.config.testnet)
        try:
            return await sweep(self.registry, info, addresses)
        finally:
            await info.aclose()

    async def _run_scan(self) -> dict:
        observers = candidate_observers(self.registry, self.config.observers)
        if not observers:
            return {"skipped": "registry is empty"}
        info = InfoClient(self.budget, testnet=self.config.testnet)
        try:
            return await scan(self.registry, info, observers)
        finally:
            await info.aclose()

    def _measure_overlap(self) -> dict:
        events = self.registry.liquidations()
        if not events:
            return {"skipped": "no liquidations yet"}
        addresses = set(self.registry.addresses())
        mapped = {
            (p.address, p.coin)
            for p in self.registry.positions()
            if (effective(p)[0] or 0) > 0
        }
        summary = population_overlap(events, addresses, mapped).summary()
        self.ledger.append("observation", {"name": "liq_population", **summary})
        return summary

    def status(self) -> str:
        parts = []
        for name, job in self.jobs.items():
            parts.append(
                f"{name}: {job.runs} run(s), {job.failures} failed, "
                f"next in {job.next_due_in_s() / 60:.0f}m"
            )
        return " | ".join(parts)


def job_summary(job: Job) -> dict:
    return {
        "name": job.name,
        "runs": job.runs,
        "failures": job.failures,
        "interval_h": job.interval_ns / NS / 3600,
    }
