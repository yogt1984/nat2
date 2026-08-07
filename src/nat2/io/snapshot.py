"""Registry snapshot: the reconciliation sweep, not the primary data path.

A full sweep of ~3,600 wallets costs about five minutes and the whole IP
weight budget (measured 2026-08-07), which is exactly why the map is meant to
be maintained from the fill stream and only reconciled from here. Running this
continuously would make polling frequency the map's resolution.
"""

from __future__ import annotations

import asyncio

from nat2.core.clock import now_ns
from nat2.core.registry import Registry
from nat2.features.liqmath import Position
from nat2.hl.info import InfoClient

CONCURRENCY = 24


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


async def sweep(
    registry: Registry,
    info: InfoClient,
    addresses: list[str],
    on_progress=None,
) -> dict:
    started = now_ns()
    sem = asyncio.Semaphore(CONCURRENCY)
    positions: list[Position] = []
    errors = 0
    holders = 0
    done = 0

    async def fetch(address: str) -> None:
        nonlocal errors, holders, done
        async with sem:
            try:
                state = await info.clearinghouse_state(address)
            except Exception:  # noqa: BLE001 - a failed wallet is a hole, not a crash
                errors += 1
                return
        found = parse_state(address, state)
        if found:
            holders += 1
            positions.extend(found)
        done += 1
        if on_progress and done % 250 == 0:
            on_progress(done, len(addresses))

    await asyncio.gather(*(fetch(a) for a in addresses))
    registry.replace_positions([(p, "published") for p in positions])
    snapshot_id = registry.record_snapshot(
        started, len(addresses), holders, len(positions), errors
    )
    return {
        "id": snapshot_id,
        "wallets": len(addresses),
        "holders": holders,
        "positions": len(positions),
        "errors": errors,
        "elapsed_s": (now_ns() - started) / 1e9,
    }
