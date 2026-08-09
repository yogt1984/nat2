"""Registry snapshot: the reconciliation sweep, not the primary data path.

A full sweep of ~3,600 wallets costs about five minutes and the whole IP weight
budget (measured 2026-08-07), which is exactly why the map is meant to be
maintained from the fill stream and only reconciled from here. Running this
continuously would make polling frequency the map's resolution.

**A failed sweep must never replace a good map.** Snapshots 4, 5 and 6 on
2026-08-08 each had all 2,177 requests fail, and each then called
`replace_positions([])` -- which deletes the table and inserts nothing. Three
transient outages silently destroyed the map and left every downstream
measurement reading an empty table while reporting success. A sweep that
learned nothing is not evidence that there is nothing, so it now refuses to
publish and says why.
"""

from __future__ import annotations

import asyncio
from collections import Counter

from nat2.core.clock import now_ns
from nat2.core.errors import reason, top_reasons
from nat2.core.reconstruct import drift
from nat2.core.registry import Registry
from nat2.features.liqmath import Position
from nat2.hl.info import InfoClient

CONCURRENCY = 24
# Above this share of failed requests the sweep is a sample of the API's mood,
# not of the venue's positions.
MAX_ERROR_FRACTION = 0.5


def parse_state(address: str, state: dict) -> list[Position]:
    cross = state.get("crossMarginSummary", {})
    account_value = float(cross.get("accountValue") or 0)
    maint = float(state.get("crossMaintenanceMarginUsed") or 0)
    out = []
    for entry in state.get("assetPositions", []):
        p = entry.get("position", {})
        szi = float(p.get("szi") or 0)
        coin = p.get("coin")
        if not coin or szi == 0:
            continue
        value = abs(float(p.get("positionValue") or 0))
        if value <= 0:
            continue
        leverage = p.get("leverage", {}) or {}
        published = p.get("liquidationPx")
        out.append(
            Position(
                address=address,
                coin=coin,
                szi=szi,
                mark=value / abs(szi),
                max_leverage=float(p.get("maxLeverage") or 0),
                margin_type=leverage.get("type", "cross"),
                account_value=account_value,
                maint_margin=maint,
                isolated_margin=float(leverage.get("rawUsd") or p.get("marginUsed") or 0),
                liquidation_px=float(published) if published else None,
            )
        )
    return out


def refusal(wallets: int, holders: int, errors: int) -> str | None:
    """Why this sweep must not be published, or None if it may be."""
    if not wallets:
        return "no wallets to sweep"
    if errors and not holders:
        return f"every request failed ({errors}/{wallets}); refusing to erase the map"
    if errors / wallets > MAX_ERROR_FRACTION:
        return (
            f"{errors}/{wallets} requests failed, over the "
            f"{MAX_ERROR_FRACTION:.0%} limit; publishing this would drop the "
            "wallets we simply could not reach"
        )
    return None


async def sweep(
    registry: Registry,
    info: InfoClient,
    addresses: list[str],
    on_progress=None,
) -> dict:
    started = now_ns()
    sem = asyncio.Semaphore(CONCURRENCY)
    positions: list[Position] = []
    failures: Counter = Counter()
    holders = 0
    done = 0

    async def fetch(address: str) -> None:
        nonlocal holders, done
        async with sem:
            try:
                state = await info.clearinghouse_state(address)
            except Exception as exc:  # noqa: BLE001 - attributed, not swallowed
                failures[reason(exc)] += 1
                return
        found = parse_state(address, state)
        if found:
            holders += 1
            positions.extend(found)
        done += 1
        if on_progress and done % 250 == 0:
            on_progress(done, len(addresses))

    await asyncio.gather(*(fetch(a) for a in addresses))
    errors = sum(failures.values())
    result = {
        "wallets": len(addresses),
        "holders": holders,
        "positions": len(positions),
        "errors": errors,
        "why": top_reasons(failures),
        "elapsed_s": (now_ns() - started) / 1e9,
    }

    blocked = refusal(len(addresses), holders, errors)
    if blocked:
        result["refused"] = blocked
        result["id"] = registry.record_snapshot(
            started, len(addresses), holders, 0, errors, refused=blocked
        )
        return result

    # The only check on tape reconstruction: the public trades channel carries
    # no per-fill checkpoint, so a fresh sweep is where drift becomes visible.
    # Measured before the replace, because the replace is what destroys it.
    carried = registry.positions(source="derived")
    result["drift"] = drift(carried, positions) if carried else {"compared": 0}
    registry.replace_positions([(p, "published") for p in positions])
    result["id"] = registry.record_snapshot(
        started, len(addresses), holders, len(positions), errors
    )
    return result
