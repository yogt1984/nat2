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
