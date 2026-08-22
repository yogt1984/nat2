"""Persist the liquidation map as it was believed at the time.

The registry keeps only the present: `replace_positions` overwrites, so the map
exists in one state and has no history. That is fine for `nat2 map`, which asks
"where are the clusters now", and useless for everything else — a model trained
on liquidation features needs the map *as it stood* when each decision was
made, and `gate map`'s predictive check needs the map that existed *before* a
liquidation fired.

So each build is appended to the WORM store as `nat2.liqmap`, dual-timestamped
like anything else. It is derived rather than captured, which is why it carries
the `nat2.` prefix — the distinction matters when auditing what is evidence and
what is inference.

Marks and open interest come from the captured `hl.assetctxs` cross-section
rather than a fresh API call: the daemon already paid for that data, and
spending weight to re-ask would compete with the sweep.

The summary is deliberately narrow — band totals, nearest clusters, coverage —
not the full histogram. A histogram per coin every few minutes would be mostly
zeros, and the features actually named in the design are all derivable from
this.
"""

from __future__ import annotations

from pathlib import Path

from nat2.core.clock import now_ns
from nat2.core.registry import Registry
from nat2.features.context import iter_contexts, latest
from nat2.features.liqmap import DEFAULT_BANDS, WIDE_BANDS, build, nearest, sparse_buckets
from nat2.io.worm import WormWriter, read_records

STREAM = "nat2.liqmap"
# v2 (TASK_2/17): the same build, looked at wider and finer, in its own stream so v1 is
# byte-identical and the pre-registrations bound to it (seq 117-119, 153) are untouched.
# Nothing reads v2 yet: it accrues calendar time, which is the only thing that cannot be
# bought back later, and a long-horizon hypothesis is written against it after 30 days.
STREAM_V2 = "nat2.liqmap2"
V2_BUCKET_PCT = 0.0025
V2_SPAN = 0.30
# Below this a "cluster" is one stray position, and `d_near` would measure
# noise rather than a magnet.
CLUSTER_MIN_NOTIONAL = 50_000.0


def summarise(liqmap, min_notional: float = CLUSTER_MIN_NOTIONAL) -> dict:
    near = nearest(liqmap, min_notional)
    return {
        "coin": liqmap.coin,
        "mark": liqmap.mark,
        "coverage": liqmap.coverage,
        "notional": liqmap.total_notional,
        "cross_notional": liqmap.cross_notional,
        "published_frac": liqmap.published_frac,
        "positions": liqmap.positions,
        "skipped": liqmap.skipped,
        "outside_span": liqmap.outside_span,
        "oi_notional": liqmap.oi_notional,
        "up": {str(b): liqmap.up.get(b, 0.0) for b in DEFAULT_BANDS},
        "down": {str(b): liqmap.down.get(b, 0.0) for b in DEFAULT_BANDS},
        "imb": {str(b): liqmap.imbalance(b) for b in DEFAULT_BANDS},
        "imb_cross": {str(b): liqmap.imbalance_cross(b) for b in DEFAULT_BANDS},
        "near": near,
    }


def summarise_wide(liqmap, day_volume: float | None, min_notional: float = CLUSTER_MIN_NOTIONAL) -> dict:
    """The v2 row: bands to +-30%, per-bucket masses with the cross split, and the two
    fields a liquidity scaling would need (`day_volume`, `oi_notional`) carried at the
    time of observation rather than re-derived later from a different cross-section."""
    return {
        **summarise(liqmap, min_notional),
        "bands": [str(b) for b in WIDE_BANDS],
        "bucket_pct": V2_BUCKET_PCT,
        "span": V2_SPAN,
        "buckets": sparse_buckets(liqmap),
        "day_volume": day_volume,
        # `summarise` keys its band dicts on DEFAULT_BANDS, so the wide bands have to be
        # written back over them or a v2 row would silently carry v1's four -- which is
        # the whole reason this stream exists.
        "up": {str(b): liqmap.up.get(b, 0.0) for b in WIDE_BANDS},
        "down": {str(b): liqmap.down.get(b, 0.0) for b in WIDE_BANDS},
        "imb": {str(b): liqmap.imbalance(b) for b in WIDE_BANDS},
        "imb_cross": {str(b): liqmap.imbalance_cross(b) for b in WIDE_BANDS},
        "up_cross": {str(b): liqmap.up_cross.get(b, 0.0) for b in WIDE_BANDS},
        "down_cross": {str(b): liqmap.down_cross.get(b, 0.0) for b in WIDE_BANDS},
    }


def snapshot(registry: Registry, root: Path, coins: list[str] | None = None) -> dict:
    """Build and persist one map snapshot per coin. No API calls."""
    contexts = latest(iter_contexts(read_records(root, "hl.assetctxs")))
    if not contexts:
        return {"skipped": "no captured asset contexts"}

    positions = registry.positions()
    if not positions:
        # Refusing here rather than writing a map of nothing: an empty snapshot
        # would enter the history as a genuine observation that no clusters
        # existed, which is exactly the lie the sweep guard was added to stop.
        return {"skipped": "registry has no positions"}

    by_coin: dict[str, list] = {}
    for position in positions:
        by_coin.setdefault(position.coin, []).append(position)

    wanted = coins or sorted(by_coin)
    rows, rows_v2 = [], []
    for coin in wanted:
        ctx = contexts.get(coin)
        if ctx is None or coin not in by_coin:
            continue
        liqmap = build(by_coin[coin], coin, ctx.mark, ctx.oi_notional)
        if liqmap.total_notional <= 0:
            continue
        rows.append(summarise(liqmap))
        wide = build(by_coin[coin], coin, ctx.mark, ctx.oi_notional,
                     bands=WIDE_BANDS, bucket_pct=V2_BUCKET_PCT, span=V2_SPAN)
        rows_v2.append(summarise_wide(wide, ctx.day_volume))

    if not rows:
        return {"skipped": "no coin had both a mark and mapped positions"}

    t_ingest = now_ns()
    # v1 first and in its own writer, so a fault in the wider build cannot cost the
    # snapshot the gates actually read.
    with WormWriter(root, STREAM) as writer:
        writer.write({"coins": rows}, t_event=None, t_ingest=t_ingest)
    with WormWriter(root, STREAM_V2) as writer:
        writer.write({"coins": rows_v2}, t_event=None, t_ingest=t_ingest)
    return {"coins": len(rows), "t_ingest": t_ingest, "v2_coins": len(rows_v2)}


def iter_snapshots(records) -> list[dict]:
    """Flatten stored snapshots into per-coin observations, arrival ordered."""
    out = []
    for record in records:
        t_ingest = record.get("t_ingest")
        payload = record.get("payload")
        if t_ingest is None or not isinstance(payload, dict):
            continue
        for row in payload.get("coins", []):
            if isinstance(row, dict) and row.get("coin"):
                out.append({"t_ingest": t_ingest, **row})
    out.sort(key=lambda r: r["t_ingest"])
    return out


def series(records, coin: str) -> list[dict]:
    return [row for row in iter_snapshots(records) if row["coin"] == coin]


def as_of(rows: list[dict], t: int) -> dict | None:
    """The map as it was believed at time `t`.

    Strictly the last snapshot that had arrived — never the next one, which is
    the map a decision at `t` could not have seen.
    """
    found = None
    for row in rows:
        if row["t_ingest"] > t:
            break
        found = row
    return found
