"""Dual-timestamp discipline.

Every captured record carries two clocks:

    t_event   the exchange's clock -- when the thing happened
    t_ingest  our clock -- when we could first have known it

A feature computed for a bar closing at T may read only rows with
``t_ingest <= T``.  Not ``t_event <= T``.  That single rule is what makes
lookahead a queryable property of the store rather than a code-review hope,
and it is what makes reconstructed positions safe to use as features.

Both are integer nanoseconds since the UNIX epoch, UTC.  Never floats: a
float64 cannot represent nanosecond epochs exactly, and silent rounding in a
timestamp is the one bug this whole design exists to prevent.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

NS = 1_000_000_000
MS_TO_NS = 1_000_000


def now_ns() -> int:
    """Ingest timestamp: our clock, at the moment of receipt."""
    return time.time_ns()


def ms_to_ns(ms: int | float | str | None) -> int | None:
    """Exchange millisecond timestamp -> nanoseconds, or None if absent.

    HL reports event times in integer milliseconds.  Absent is a legitimate
    answer for streams that carry no exchange clock (``allMids``,
    ``activeAssetCtx``); the audit knows which streams those are and does not
    penalise them.
    """
    if ms is None:
        return None
    return int(ms) * MS_TO_NS


def to_dt(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / NS, tz=UTC)


def hour_key(ns: int) -> str:
    """UTC hour bucket used for WORM file rotation."""
    return to_dt(ns).strftime("%Y%m%dT%H")


def day_key(ns: int) -> str:
    return to_dt(ns).strftime("%Y-%m-%d")


def parse_window(spec: str) -> int:
    """Parse a window like '24h', '30m', '7d', '90s' into nanoseconds."""
    spec = spec.strip().lower()
    units = {"s": NS, "m": 60 * NS, "h": 3600 * NS, "d": 86400 * NS}
    if not spec or spec[-1] not in units:
        raise ValueError(f"bad window {spec!r}: expected e.g. 30m, 24h, 7d")
    try:
        value = float(spec[:-1])
    except ValueError as exc:
        raise ValueError(f"bad window {spec!r}") from exc
    return int(value * units[spec[-1]])
