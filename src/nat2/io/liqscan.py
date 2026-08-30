"""Scan observers for realized liquidations.

Liquidations are observed through whoever took the other side, so the observer
set is a handful of wallets that absorb forced flow -- not the whole registry.
Ranking candidates by weekly volume is a proxy for that; `rate` reports how
many events each observer actually contributed, so the set can be pruned on
evidence rather than kept on faith.

`userFills` costs real weight and returns up to 2,000 fills per wallet, so
this is a periodic job. It is never on a bar-frequency path.
"""

from __future__ import annotations

import asyncio

from nat2.core.registry import Registry
from nat2.features.liquidations import dedupe, from_fills
from nat2.hl.info import InfoClient

CONCURRENCY = 8


async def scan(
    registry: Registry,
    info: InfoClient,
    observers: list[str],
    on_progress=None,
) -> dict:
    sem = asyncio.Semaphore(CONCURRENCY)
    events = []
    per_observer: dict[str, int] = {}
    errors = 0
    done = 0

    async def one(address: str) -> None:
        nonlocal errors, done
        async with sem:
            try:
                fills = await info.post("userFills", user=address)
            except Exception:  # noqa: BLE001 - a failed observer is a hole, not a crash
                errors += 1
                return
        found = from_fills(fills, address)
        per_observer[address] = len(found)
        events.extend(found)
        done += 1
        if on_progress and done % 10 == 0:
            on_progress(done, len(observers))

    await asyncio.gather(*(one(a) for a in observers))
    unique = dedupe(events)
    inserted = registry.record_liquidations(unique)
    productive = sum(1 for n in per_observer.values() if n)
    return {
        "observers": len(observers),
        "productive_observers": productive,
        "raw_events": len(events),
        "unique_events": len(unique),
        "new_events": inserted,
        "errors": errors,
    }


def candidate_observers(registry: Registry, limit: int) -> list[str]:
    """Highest-volume wallets first: whoever trades most absorbs most forced flow."""
    from contextlib import closing

    # Through the registry's own factory, not a bare `sqlite3.connect`: that is
    # where the busy timeout lives, and a reader that opens its own connection
    # would silently keep the driver default.
    with closing(registry._connect()) as conn:
        rows = conn.execute(
            "SELECT address FROM wallets ORDER BY vlm_week DESC LIMIT ?", (limit,)
        )
        return [r[0] for r in rows]
