#!/usr/bin/env python3
"""nat2 liqfill -- the liqmap2 tape on a regular minute grid, holes carried forward and marked.

`deploy/liqview.py` draws a hole in the tape as a blank column, which is the honest
picture: no snapshot, nothing known. This keeps the *other* file -- the one where every
minute has a map -- for work that needs a dense grid (as-of joins, feature rows, a frame
of the whole tape without gaps). A filled minute carries the newest real snapshot before
it, unchanged, and is stamped `extrapolated=True` with its `age_s` and the `src_t` it came
from. Nothing is interpolated: a hole is the last position held still, which is what "we
do not know what changed" looks like as data.

The raw store is never touched. The cache is two parquet files per coin under `data/cache/`:

    <COIN>_filled.parquet   one row per (real snapshot, bucket), liqcache's columns + imb_json
    <COIN>_grid.parquet     one row per minute: t, src_t, extrapolated, age_s

and it is incremental: a run scans only the snapshots newer than the cache and appends.
The first run over the whole tape costs ~5 min per coin; every run after it costs seconds.

    /usr/bin/python3 tools/liqfill.py --coin ETH --render            # update cache, draw all of it
    /usr/bin/python3 tools/liqfill.py --coin ETH --render --since 48h
    /usr/bin/python3 tools/liqfill.py --coin ETH --rebuild           # throw the cache away first

`--render` draws the liqview frame from the filled minutes. The first two lines are the
interval and the step. A column made only of carried minutes shows `E` in its top row.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy"))
sys.path.insert(0, str(ROOT / "tools"))

import liqview  # noqa: E402
import liqcache  # noqa: E402
import numpy as np  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

NS = liqview.NS
STEP_NS = 60 * NS
CACHE = ROOT / "data" / "cache"
PREFIX = 12          # "    89,724 |" -- the price gutter liqview prints before each grid row
MARK = "E"
SNAP_KEYS = ("t", "mark", "coverage", "published_frac", "positions", "outside_span",
             "span", "bucket_pct")


# --- the grid ---------------------------------------------------------------

def grid_minutes(real_ts: list[int], step_ns: int = STEP_NS) -> list[tuple[int, int, bool, float]]:
    """`(t, src_t, extrapolated, age_s)` per minute from the first to the last real snapshot."""
    if not real_ts:
        return []
    out, i, last = [], 0, None
    t = real_ts[0] - real_ts[0] % step_ns
    end = real_ts[-1]
    while t <= end:
        newest = None
        while i < len(real_ts) and real_ts[i] < t + step_ns:
            newest = real_ts[i]
            i += 1
        if newest is not None:
            last = newest
            out.append((t, newest, False, 0.0))
        elif last is not None:
            out.append((t, last, True, (t - last) / NS))
        t += step_ns
    return out


def fill(rows: list[dict], step_ns: int = STEP_NS) -> list[dict]:
    """liqview rows on the minute grid, holes carried forward. `rows` oldest first."""
    by_t = {r["t"]: r for r in rows}
    return [{**by_t[src], "t": t, "src_t": src, "extrapolated": e, "age_s": age}
            for t, src, e, age in grid_minutes(sorted(by_t))]


# --- the cache ---------------------------------------------------------------

def explode(rows: list[dict], coin: str) -> pa.Table:
    table = liqcache.explode(rows, coin)
    imb = {r["t"]: json.dumps(r.get("imb") or {}) for r in rows}
    return table.append_column("imb_json", pa.array([imb[t] for t in table.column("t").to_pylist()]))


def implode(table: pa.Table) -> list[dict]:
    """Bucket rows back into liqview snapshot rows, oldest first. numpy slicing, not a dict loop."""
    if table.num_rows == 0:
        return []
    table = table.sort_by("t")
    t = table.column("t").to_numpy()
    cuts = np.flatnonzero(np.diff(t)) + 1
    starts = np.concatenate(([0], cuts))
    cols = {k: table.column(k).to_numpy(zero_copy_only=False) for k in
            ("lo_pct", "notional", "cross_notional", "n_positions")}
    snap = {k: table.column(k).to_pylist() for k in SNAP_KEYS}
    imb = table.column("imb_json").to_pylist()
    rows = []
    for a, b in zip(starts, np.concatenate((cuts, [len(t)]))):
        r = {k: snap[k][a] for k in SNAP_KEYS}
        r["imb"] = json.loads(imb[a])
        r["buckets"] = [list(x) for x in zip(cols["lo_pct"][a:b].tolist(), cols["notional"][a:b].tolist(),
                                              cols["cross_notional"][a:b].tolist(),
                                              cols["n_positions"][a:b].tolist())]
        rows.append(r)
    return rows


def paths(coin: str) -> tuple[Path, Path]:
    return CACHE / f"{coin}_filled.parquet", CACHE / f"{coin}_grid.parquet"


def update(coin: str, rebuild: bool = False, log=print) -> list[dict]:
    """Bring the coin's cache up to the tape and return its real snapshot rows."""
    buckets_path, grid_path = paths(coin)
    CACHE.mkdir(parents=True, exist_ok=True)
    have: pa.Table | None = None
    since = liqview.earliest_ns()
    if buckets_path.exists() and not rebuild:
        have = pq.read_table(buckets_path)
        if have.num_rows:
            since = int(pa.compute.max(have.column("t")).as_py()) + 1
    t0 = time.monotonic()
    new = liqview.snapshots(coin, since, time.time_ns())
    if new:
        table = explode(new, coin)
        table = pa.concat_tables([have, table]) if have is not None and have.num_rows else table
        pq.write_table(table, buckets_path, compression="zstd")
    else:
        table = have
    if table is None or table.num_rows == 0:
        return []
    rows = implode(table)
    g = grid_minutes([r["t"] for r in rows])
    pq.write_table(pa.table({
        "t": pa.array([x[0] for x in g], pa.int64()), "src_t": pa.array([x[1] for x in g], pa.int64()),
        "extrapolated": pa.array([x[2] for x in g]), "age_s": pa.array([x[3] for x in g], pa.float64()),
    }), grid_path, compression="zstd")
    n_e = sum(1 for x in g if x[2])
    log(f"cache {coin}: +{len(new):,} new snapshots in {time.monotonic() - t0:.0f}s -> "
        f"{len(rows):,} real, {len(g):,} minutes, {n_e:,} carried ({n_e / max(len(g), 1):.1%}), "
        f"longest hole {max((x[3] for x in g), default=0) / 3600:.1f} h   "
        f"{buckets_path.stat().st_size / 1e6:.0f} MB + {grid_path.stat().st_size / 1e6:.1f} MB")
    return rows


# --- the frame ---------------------------------------------------------------

def mark_frame(text: str, rows: list[dict], width: int, window: tuple[int, int]) -> str:
    """Interval and step first; `E` in the top grid row of every column of carried minutes only."""
    bins = liqview.columns(rows, width, window)
    carried = [bool(b) and all(r["extrapolated"] for r in b) for b in bins]
    lines = text.split("\n")
    top = next(i for i, line in enumerate(lines) if line[PREFIX - 1:PREFIX] == "|"
               and line[:PREFIX - 2].strip().replace(",", "").replace(".", "").isdigit())
    row = list(lines[top].ljust(PREFIX + width))
    for x, c in enumerate(carried):
        if c:
            row[PREFIX + x] = MARK
    lines[top] = "".join(row)
    n_e = sum(r["extrapolated"] for r in rows)
    head = [
        f"interval  {liqview.iso(window[0])} -> {liqview.iso(window[1])}   "
        f"{liqview.human_s((window[1] - window[0]) / NS)}",
        f"step      {liqview.human_s(liqview.col_seconds(window, width))} per column   "
        f"grid 1 min   {len(rows):,} minutes, {n_e:,} carried forward   "
        f"{sum(carried)}/{width} columns entirely carried = '{MARK}' on the top row",
    ]
    return "\n".join(head + lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--coin", required=True)
    parser.add_argument("--since", default="max", help="window to render; the cache always holds all")
    parser.add_argument("--rebuild", action="store_true", help="discard the cache and rescan the tape")
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--span", type=float, default=0.03)
    parser.add_argument("--rows", type=int, default=30)
    parser.add_argument("--width", type=int, default=120)
    parser.add_argument("--bands", default="0.01,0.05")
    args = parser.parse_args(argv)

    rows = update(args.coin, args.rebuild, log=lambda s: print(s, file=sys.stderr))
    if not rows:
        print(f"no {args.coin} snapshots", file=sys.stderr)
        return 1
    if not args.render:
        return 0
    filled = fill(rows)
    if args.since != "max":
        since = liqview.parse_when(args.since, time.time_ns())
        filled = [r for r in filled if r["t"] >= since]
    window = (filled[0]["t"], filled[-1]["t"])
    text = liqview.frame(filled, args.coin, "absolute", args.span, args.rows, args.width,
                         False, bands=[b for b in args.bands.split(",") if b], window=window)
    print(mark_frame(text, filled, args.width, window))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
