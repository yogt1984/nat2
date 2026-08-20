"""Whole-population position series from the captured tape (TASK_2/07).

Glue between the WORM store, the registry, `userFills` and `core/reconstruct`:
reads, anchors, reconstructs, audits drift, writes one parquet. Batch, on
demand, no daemon. Gaps in the capture demote every wallet to unanchored --
they are named in the summary, never bridged.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from nat2.core.registry import Registry
from nat2.core.reconstruct import (
    Anchor, anchors_from_checkpoints, checkpoints, drift_audit, series, ingest_silences, tape_gaps,
)
from nat2.features.fills import flatten, signed_parties
from nat2.io.worm import read_manifest, read_records

TRADES_CADENCE_S = 3600.0     # hourly file rotation; same figure as deploy/gapwatch.py
MAX_SILENCE_S = 60.0          # the venue-wide tape never goes quiet this long when capture is healthy


def published_anchors(registry: Registry, coin: str, from_ns: int) -> list[Anchor]:
    """The last sweep anchors a window that starts at or after it; an older window gets none."""
    ts = registry.published_ts()
    if ts is None or ts > from_ns:
        return []
    return [Anchor(p.address, coin, p.szi, ts, "published")
            for p in registry.positions(coin=coin, source="published")]


async def fetch_checkpoints(info, addresses: list[str], coin: str) -> list:
    cps = []
    for address in addresses:
        cps.extend(checkpoints(await info.post("userFills", user=address), address, coin))
    return cps


def most_active_unanchored(trades, coin: str, anchored: set[str], n: int) -> list[str]:
    counts: Counter[str] = Counter()
    for t in trades:
        if t.get("coin") == coin:
            counts.update(a for a, _ in signed_parties(t) if a not in anchored)
    return [a for a, _ in counts.most_common(n)]


async def reconstruct(root: Path, registry: Registry, coin: str, from_ns: int, to_ns: int,
                      out_dir: Path, info=None, fills: int = 0) -> dict:
    records = list(read_records(root, "hl.trades", since_ns=from_ns))
    gaps = (tape_gaps(read_manifest(root, "hl.trades"), from_ns, to_ns, TRADES_CADENCE_S)
            + ingest_silences(records, from_ns, to_ns, MAX_SILENCE_S))
    trades = flatten(records)
    anchors = published_anchors(registry, coin, from_ns)
    cps = []
    if fills and info is not None:
        targets = most_active_unanchored(trades, coin, {a.address for a in anchors}, fills)
        cps = await fetch_checkpoints(info, targets, coin)
        cps = [c for c in cps if from_ns <= c.ts_ns <= to_ns]
        anchors += anchors_from_checkpoints(cps, from_ns, to_ns)
    frame = series(trades, coin, anchors, from_ns, to_ns, gap_free=not gaps)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"reconstruct_{coin}_{from_ns}_{to_ns}.parquet"
    frame.sort(["ts_ns", "tid", "address"]).write_parquet(out)
    last = frame.sort(["ts_ns", "tid"]).group_by("address").last() if not frame.is_empty() else frame
    return {
        "coin": coin, "from_ns": from_ns, "to_ns": to_ns, "prints": frame["tid"].n_unique(),
        "wallets": last.height,
        "anchored_wallets": int(last["anchored"].sum()) if last.height else 0,
        "by_source": dict(Counter(last["anchor_source"].to_list())),
        "gaps": [[a, b] for a, b in gaps],
        "drift": drift_audit(frame, cps) if cps else None,
        "out": str(out),
    }
