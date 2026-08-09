"""One rate-limit budget, shared by capture and (later) execution.

Two independent limits at HL: request weight per IP, and actions per address.
They are tracked separately because they fail differently -- exceeding the IP
budget throttles data collection and silently coarsens the liquidation map,
while exceeding the address budget rejects orders.

Every constant below is a **verify-before-coding** item from the design.  None
of them are load-bearing guesses: they are recorded with the date a human last
checked them against HL's docs, and ``gate feed`` warns once that date goes
stale.  A limit that moved without anyone noticing is exactly the failure this
stamp exists to surface.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

# --- verified-against-docs table -------------------------------------------
VERIFIED_ON = "2026-08-07"
STALE_AFTER_DAYS = 90

IP_WEIGHT_PER_MINUTE = 1200
DEFAULT_INFO_WEIGHT = 20
INFO_WEIGHT: dict[str, int] = {
    "l2Book": 2,
    "allMids": 2,
    "clearinghouseState": 2,
    "orderStatus": 2,
    "spotClearinghouseState": 2,
    "exchangeStatus": 2,
    "userRole": 60,
}
# ---------------------------------------------------------------------------

# A reservation never takes the whole account: a starved capture daemon drops
# tape it can never recover, which is a worse loss than a slow sweep.
MIN_SHARE_FRACTION = 0.15


def weight_of(info_type: str) -> int:
    return INFO_WEIGHT.get(info_type, DEFAULT_INFO_WEIGHT)


def table_age_days(now: float | None = None) -> float:
    import datetime as _dt

    checked = _dt.date.fromisoformat(VERIFIED_ON)
    today = _dt.date.today() if now is None else _dt.date.fromtimestamp(now)
    return (today - checked).days


@dataclass
class WeightBudget:
    """Sliding-window weight limiter.

    Blocks rather than raising: a capture daemon that dies on a burst is worse
    than one that waits, and every wait is counted so the audit can see how
    close to the ceiling we run.
    """

    limit: int = IP_WEIGHT_PER_MINUTE
    window_s: float = 60.0
    _events: list[tuple[float, int]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    waits: int = 0
    wait_s: float = 0.0

    def _spent(self, now: float) -> int:
        cutoff = now - self.window_s
        self._events = [(t, w) for t, w in self._events if t > cutoff]
        return sum(w for _, w in self._events)

    def _try(self, weight: int) -> float:
        """Reserve `weight`, or return the seconds to wait before retrying."""
        with self._lock:
            now = time.monotonic()
            if self._spent(now) + weight <= self.limit:
                self._events.append((now, weight))
                return 0.0
            sleep_for = max(0.01, self._events[0][0] + self.window_s - now)
            self.waits += 1
            self.wait_s += sleep_for
            return sleep_for

    def acquire(self, weight: int) -> None:
        while (sleep_for := self._try(weight)) > 0:
            time.sleep(sleep_for)

    async def acquire_async(self, weight: int) -> None:
        import asyncio

        while (sleep_for := self._try(weight)) > 0:
            await asyncio.sleep(sleep_for)

    def headroom(self) -> float:
        with self._lock:
            return 1.0 - self._spent(time.monotonic()) / self.limit

    @contextmanager
    def reserve(self, weight: int, ttl_s: float = 120.0):
        """Claim `weight` of the account for this process.

        A no-op here: one process competing with itself needs no protocol. The
        shared budget overrides this, and callers reserve unconditionally so
        the in-memory budget stays usable in tests.
        """
        yield self


class SharedWeightBudget(WeightBudget):
    """Weight budget shared across processes.

    HL's limit is per IP, not per process, so an in-memory budget lets two
    commands run back to back and collectively blow through it -- which is
    exactly how `nat2 gate map` earned a 429 immediately after `nat2 liq scan`.
    Spend is journalled to SQLite so every process on this machine draws from
    one account.

    Uses the wall clock rather than a monotonic one because the record has to
    mean the same thing to a process that started later.

    **Sharing is not the same as fairness.** First-come-first-served let the
    capture daemon's 15s poll crowd out the registry sweep: on 2026-08-08 all
    2,177 sweep requests failed against a budget the poller had already spent,
    three times running. So a process may *reserve* part of the account with
    `reserve()`, and every other process then admits against the remainder. The
    lease is a heartbeat rather than a promise -- it expires on its own, so a
    sweep that dies holding one cannot starve the daemon forever.
    """

    def __init__(
        self,
        path,
        limit: int = IP_WEIGHT_PER_MINUTE,
        window_s: float = 60.0,
        owner: str | None = None,
    ):
        super().__init__(limit=limit, window_s=window_s)
        import os
        import sqlite3
        from pathlib import Path

        # Identity is per process, so a lease taken by the cycle is foreign to
        # the capture daemon without either being told the other's name.
        self.owner = owner or f"pid{os.getpid()}"
        self._lease: tuple[int, float] | None = None
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS spend (ts REAL NOT NULL, weight INT)")
            conn.execute("CREATE INDEX IF NOT EXISTS spend_ts ON spend(ts)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS leases ("
                " owner TEXT PRIMARY KEY, weight INT NOT NULL, expires REAL NOT NULL)"
            )

    def _try(self, weight: int) -> float:
        import sqlite3

        now = time.time()
        cutoff = now - self.window_s
        with sqlite3.connect(self.path, timeout=10.0, isolation_level="IMMEDIATE") as conn:
            conn.execute("DELETE FROM spend WHERE ts <= ?", (cutoff,))
            conn.execute("DELETE FROM leases WHERE expires <= ?", (now,))
            spent = conn.execute("SELECT COALESCE(SUM(weight), 0) FROM spend").fetchone()[0]
            reserved = conn.execute(
                "SELECT COALESCE(SUM(weight), 0) FROM leases WHERE owner <> ?", (self.owner,)
            ).fetchone()[0]
            allowed = max(int(self.limit * MIN_SHARE_FRACTION), self.limit - reserved)
            if spent + weight <= allowed:
                conn.execute("INSERT INTO spend VALUES (?, ?)", (now, weight))
                if self._lease is not None:
                    # Progress renews the claim, so the lease outlives a slow
                    # sweep but not a dead one.
                    lease_weight, ttl_s = self._lease
                    conn.execute(
                        "INSERT INTO leases VALUES (?,?,?) ON CONFLICT(owner) DO UPDATE SET"
                        " weight=excluded.weight, expires=excluded.expires",
                        (self.owner, lease_weight, now + ttl_s),
                    )
                return 0.0
            oldest = conn.execute("SELECT MIN(ts) FROM spend").fetchone()[0] or now
        sleep_for = max(0.05, oldest + self.window_s - now)
        with self._lock:
            self.waits += 1
            self.wait_s += sleep_for
        return sleep_for

    @contextmanager
    def reserve(self, weight: int, ttl_s: float = 120.0):
        import sqlite3

        weight = max(0, min(int(weight), int(self.limit * (1 - MIN_SHARE_FRACTION))))
        expires = time.time() + ttl_s
        with sqlite3.connect(self.path, timeout=10.0, isolation_level="IMMEDIATE") as conn:
            conn.execute(
                "INSERT INTO leases VALUES (?,?,?) ON CONFLICT(owner) DO UPDATE SET"
                " weight=excluded.weight, expires=excluded.expires",
                (self.owner, weight, expires),
            )
        self._lease = (weight, ttl_s)
        try:
            yield self
        finally:
            self._lease = None
            with sqlite3.connect(self.path, timeout=10.0, isolation_level="IMMEDIATE") as conn:
                conn.execute("DELETE FROM leases WHERE owner = ?", (self.owner,))

    def reserved_by_others(self) -> int:
        import sqlite3

        with sqlite3.connect(self.path, timeout=10.0) as conn:
            return conn.execute(
                "SELECT COALESCE(SUM(weight), 0) FROM leases"
                " WHERE owner <> ? AND expires > ?",
                (self.owner, time.time()),
            ).fetchone()[0]
