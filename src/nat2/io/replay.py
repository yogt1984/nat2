"""Fold captured tape into the registry.

Reads only what has arrived since the last watermark, so running it twice in a
row is a no-op rather than a double count -- the tape is a stream of *changes*,
and applying a change twice moves a position that never moved.

Marks come from the captured `hl.assetctxs` cross-section rather than a fresh
API call: the daemon already paid for that data, and spending weight to re-ask
would compete with the sweep that actually needs it.
"""

from __future__ import annotations

import json
from pathlib import Path

from nat2.core.reconstruct import apply
from nat2.core.registry import Registry
from nat2.features.fills import deltas, flatten
from nat2.hl.schemas import asset_contexts
from nat2.io.worm import read_records

WATERMARK = "tape_watermark"


def latest_marks(root: Path) -> dict[str, float]:
    """Most recent mark price per coin from the captured cross-section."""
    marks: dict[str, float] = {}
    for record in read_records(root, "hl.assetctxs"):
        for ctx in asset_contexts(record.get("payload")):
            mark = ctx.get("markPx")
            if ctx.get("coin") and mark:
                try:
                    marks[ctx["coin"]] = float(mark)
                except (TypeError, ValueError):
                    continue
    return marks


def replay(registry: Registry, root: Path, marks: dict[str, float] | None = None) -> dict:
    watermark = int(registry.get_state(WATERMARK, 0) or 0)
    records = [
        r
        for r in read_records(root, "hl.trades", since_ns=watermark or None)
        if r["t_ingest"] > watermark
    ]
    if not records:
        return {"skipped": "no new tape", "watermark": watermark}

    high_water = max(r["t_ingest"] for r in records)
    trades = flatten(records)
    addresses = set(registry.addresses())
    changes = deltas(trades, addresses)
    result = apply(
        registry.positions(), changes, marks if marks is not None else latest_marks(root)
    )

    registry.upsert_positions(result.upserts)
    registry.delete_positions(result.closes)
    registry.set_state(WATERMARK, high_water)

    return {
        "records": len(records),
        "trades": len(trades),
        "deltas": len(changes),
        **result.summary(),
        "watermark": high_water,
    }


def reset_watermark(registry: Registry) -> None:
    """Replay the whole store from the beginning. Only safe right after a sweep."""
    registry.set_state(WATERMARK, 0)


def watermark_age_ns(registry: Registry, now_ns: int) -> int | None:
    value = registry.get_state(WATERMARK)
    return now_ns - int(value) if value else None


def _decode(raw: str):  # pragma: no cover - convenience for debugging
    return json.loads(raw)
