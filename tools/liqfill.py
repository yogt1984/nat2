#!/usr/bin/env python3
"""nat2 liqfill -- the liqmap2 tape on a regular minute grid, holes carried forward and marked.

`deploy/liqview.py` draws a hole in the tape as a blank column, which is the honest
picture: no snapshot, nothing known. This writes the *other* file -- the one where every
minute has a map -- for work that needs a dense grid (as-of joins, feature rows, a frame
without gaps). A filled minute carries the newest real snapshot before it, unchanged, and
is stamped `extrapolated=True` with its `age_s` and the `src_t` it came from. Nothing is
interpolated: a hole is the last position held still, which is what "we do not know what
changed" looks like as data.

The raw store is never touched. Output is one parquet per coin, bucket-exploded like
`tools/liqcache.py` (same columns plus the three stamps), under `data/cache/`.

    /usr/bin/python3 tools/liqfill.py --coin BTC --out data/cache/BTC_filled.parquet
    /usr/bin/python3 tools/liqfill.py --coin BTC --render --from data/cache/BTC_filled.parquet

`--render` draws the liqview frame from the filled rows; a column made only of carried
minutes shows `E` in its top row, so the eye can tell a held position from a seen one.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy"))
sys.path.insert(0, str(ROOT / "tools"))

import liqview  # noqa: E402
import liqcache  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.compute as pc  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

NS = liqview.NS
STEP_NS = 60 * NS
PREFIX = 12          # "    89,724 |" -- the price gutter liqview prints before each grid row
MARK = "E"


def fill(rows: list[dict], step_ns: int = STEP_NS) -> list[dict]:
    """One row per grid minute from the first to the last snapshot, oldest first.

    A minute with a real snapshot keeps the newest one inside it (`extrapolated=False`,
    `age_s=0`). A minute without one carries the previous row's map forward.
    """
    if not rows:
        return []
    rows = sorted(rows, key=lambda r: r["t"])
    out: list[dict] = []
    i, last = 0, None
    t = rows[0]["t"] - rows[0]["t"] % step_ns
    end = rows[-1]["t"]
    while t <= end:
        newest = None
        while i < len(rows) and rows[i]["t"] < t + step_ns:
            newest = rows[i]
            i += 1
        if newest is not None:
            last = newest
            out.append({**newest, "t": t, "src_t": newest["t"], "extrapolated": False, "age_s": 0.0})
        elif last is not None:
            out.append({**last, "t": t, "src_t": last["t"], "extrapolated": True,
                        "age_s": (t - last["t"]) / NS})
        t += step_ns
    return out


def explode(rows: list[dict], coin: str) -> pa.Table:
    table = liqcache.explode(rows, coin)
    per_row = {r["t"]: r for r in rows}
    ts = table.column("t").to_pylist()
    return (table
            .append_column("src_t", pa.array([per_row[t]["src_t"] for t in ts], pa.int64()))
            .append_column("extrapolated", pa.array([per_row[t]["extrapolated"] for t in ts]))
            .append_column("age_s", pa.array([per_row[t]["age_s"] for t in ts], pa.float64())))


def implode(table: pa.Table) -> list[dict]:
    """Bucket rows back into liqview's snapshot rows, for `--render`."""
    cols = table.to_pydict()
    rows: dict[int, dict] = {}
    for k in range(table.num_rows):
        t = cols["t"][k]
        r = rows.get(t)
        if r is None:
            r = rows[t] = {key: cols[key][k] for key in (
                "t", "mark", "coverage", "published_frac", "positions", "outside_span",
                "span", "bucket_pct", "src_t", "extrapolated", "age_s")}
            r["buckets"], r["imb"] = [], {}
        r["buckets"].append([cols["lo_pct"][k], cols["notional"][k],
                             cols["cross_notional"][k], cols["n_positions"][k]])
    return [rows[t] for t in sorted(rows)]


def mark_frame(text: str, rows: list[dict], width: int, window: tuple[int, int]) -> str:
    """Put `E` in the top grid row of every column that holds only carried minutes."""
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
    n = sum(carried)
    lines.insert(3, f"filled grid: {len(rows)} minutes, {sum(r['extrapolated'] for r in rows)} "
                    f"carried forward   {n}/{width} columns entirely carried = '{MARK}' on the top row")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--coin", required=True)
    parser.add_argument("--since", default="max")
    parser.add_argument("--until", default=None)
    parser.add_argument("--out", type=Path, help="parquet to write (default data/cache/<COIN>_filled.parquet)")
    parser.add_argument("--from", dest="src", type=Path, help="render from an existing filled parquet")
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--span", type=float, default=0.03)
    parser.add_argument("--rows", type=int, default=30)
    parser.add_argument("--width", type=int, default=120)
    parser.add_argument("--bands", default="0.01,0.05")
    args = parser.parse_args(argv)

    t0 = time.monotonic()
    if args.src:
        filled = implode(pq.read_table(args.src))
    else:
        now_ns = time.time_ns()
        until = liqview.parse_when(args.until, now_ns) if args.until else now_ns
        since = liqview.earliest_ns() if args.since == "max" else liqview.parse_when(args.since, now_ns)
        rows = liqview.snapshots(args.coin, since, until)
        if not rows:
            print(f"no {args.coin} snapshots", file=sys.stderr)
            return 1
        filled = fill(rows)
        out = args.out or ROOT / "data" / "cache" / f"{args.coin}_filled.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        table = explode(filled, args.coin)
        pq.write_table(table, out, compression="zstd")
        n_e = sum(r["extrapolated"] for r in filled)
        print(f"{args.coin}  {liqview.iso(filled[0]['t'])} -> {liqview.iso(filled[-1]['t'])}  "
              f"{len(rows):,} real snapshots -> {len(filled):,} minutes, {n_e:,} carried "
              f"({n_e / len(filled):.1%}), longest hole "
              f"{max((r['age_s'] for r in filled), default=0) / 3600:.1f} h")
        print(f"  {table.num_rows:,} bucket rows  {out.stat().st_size / 1e6:.1f} MB  "
              f"{time.monotonic() - t0:.0f}s  -> {out}")
    if args.render:
        window = (filled[0]["t"], filled[-1]["t"])
        text = liqview.frame(filled, args.coin, "absolute", args.span, args.rows, args.width,
                             False, bands=[b for b in args.bands.split(",") if b], window=window)
        print(mark_frame(text, filled, args.width, window))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
