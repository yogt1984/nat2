#!/usr/bin/env python3
"""nat2 liqcache -- the liqmap2 bucket store, exploded once into one parquet.

`deploy/liqview.py` reads the WORM store correctly and is the only reader; this
does not add a second one, it imports that one. What it adds is *not having to
run it again*: twelve days of `nat2.liqmap2` is ~12k zst parts, each decompressed
and JSON-parsed for 176 coins to keep one, and the contact sheet in the plan
re-renders the same window dozens of times. Reading it once is a prerequisite for
that being usable, not an optimisation.

One row per (snapshot, bucket). The snapshot-level fields repeat down the rows --
storage is not the constraint here and a self-contained row means the figure code
never has to join anything back.

`positions` and `outside_span` ride along because every figure has to print them.
About half of BTC's mapped positions sit outside the ±30% span the map covers, so
a bucket table without that count reads as complete when it is half the story.

Stdlib plus pyarrow, on `/usr/bin/python3` (which has it in the user site). This
lives in `tools/` and not `deploy/` for exactly that reason: `deploy/` is
stdlib-only so the ops tools survive a broken venv, and this is not an ops tool.

Usage:

    /usr/bin/python3 tools/liqcache.py --coin BTC --since 24h --out /tmp/btc.parquet
    /usr/bin/python3 tools/liqcache.py --coin ETH --since 2026-08-22 --until 2026-09-02 \
        --out data/cache/eth.parquet
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy"))

import liqview  # noqa: E402  -- the path insert above has to land first

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402


# Bucket entries are `[lo_pct, notional, cross_notional, positions]` (liqmap.sparse_buckets).
# Older parts carry only the first two; index defensively rather than unpacking.
def explode(rows: list[dict], coin: str) -> pa.Table:
    """One row per (snapshot, bucket), snapshot fields repeated."""
    cols: dict[str, list] = {k: [] for k in (
        "t", "coin", "mark", "coverage", "published_frac", "positions", "outside_span",
        "span", "bucket_pct", "lo_pct", "price", "notional", "cross_notional", "n_positions",
    )}
    for row in rows:
        for entry in row["buckets"]:
            lo_pct = entry[0]
            cols["t"].append(row["t"])
            cols["coin"].append(coin)
            cols["mark"].append(row["mark"])
            cols["coverage"].append(row["coverage"])
            cols["published_frac"].append(row["published_frac"])
            cols["positions"].append(row.get("positions"))
            cols["outside_span"].append(row.get("outside_span"))
            cols["span"].append(row["span"])
            cols["bucket_pct"].append(row["bucket_pct"])
            cols["lo_pct"].append(lo_pct)
            # Absolute price of the bucket's low edge, at that snapshot's mark. Stored
            # rather than derived later because `mark` moves: the same lo_pct is a
            # different price in every row, and recomputing it downstream is where a
            # relative-frame reading gets mistaken for an absolute one.
            cols["price"].append(row["mark"] * (1 + lo_pct))
            cols["notional"].append(entry[1])
            cols["cross_notional"].append(entry[2] if len(entry) > 2 else None)
            cols["n_positions"].append(entry[3] if len(entry) > 3 else None)
    return pa.table(cols)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--coin", required=True)
    parser.add_argument("--since", default="24h",
                        help="'6h', '3d', or an ISO date -- liqview.parse_when")
    parser.add_argument("--until", default=None)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    now_ns = time.time_ns()
    since = liqview.parse_when(args.since, now_ns)
    until = liqview.parse_when(args.until, now_ns) if args.until else now_ns

    t0 = time.monotonic()
    rows = liqview.snapshots(args.coin, since, until)
    if not rows:
        print(f"no {args.coin} snapshots in "
              f"{liqview.iso(since)} -> {liqview.iso(until)}", file=sys.stderr)
        return 1

    table = explode(rows, args.coin)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, args.out, compression="zstd")

    last = rows[-1]
    print(f"{args.coin}  {liqview.iso(rows[0]['t'])} -> {liqview.iso(last['t'])}")
    print(f"  {len(rows):,} snapshots  {table.num_rows:,} bucket rows  "
          f"{time.monotonic() - t0:.1f}s  -> {args.out}")
    print(f"  coverage {last['coverage']:.3f}  published {last['published_frac']:.3f}  "
          f"outside_span {last.get('outside_span')}/{last.get('positions')}")
    lo = table.column("lo_pct")
    print(f"  lo_pct {pa.compute.min(lo).as_py():+.4f} .. {pa.compute.max(lo).as_py():+.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
