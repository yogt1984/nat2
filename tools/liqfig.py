#!/usr/bin/env python3
"""nat2 liqfig -- liqview's heatmap, painted in pixels instead of characters.

This is a second *painter*, not a second renderer. Every decision about what the
picture means was already made in `deploy/liqview.py` and is imported rather than
restated: which stream to read, how buckets land in cells, which frame of
reference is the default, and where the colour scale clips. If a constant appears
in both files, the copy here is a bug.

The four that matter, all `liqview`'s and none of them re-derived:

*   **`grid()` builds the matrix.** Absolute view by default -- the frame in
    which "price travelled to the cluster" is a statement about the picture
    rather than about the axes. `math.floor`, not `int()`, so the bottom row is
    not a permanent cluster that is not there. Out-of-range buckets dropped,
    never clamped: a clamped bucket is an observation moved to a price it was
    not at.
*   **The scale is logarithmic, clipped p20..p99** (`_percentile`). Cluster
    notional spans four decades between adjacent buckets; a linear ramp shows
    one hot cell and calls the rest empty.
*   **The clip is computed over *visible* cells**, so it moves when `--span`
    moves. That is correct and it will still surprise you, so the values are
    printed on every figure.
*   **`coverage` and `published_frac` are different numbers** and both are
    printed, as liqview prints them.

One thing is added that liqview has no room for: **`outside_span/positions`**.
About half of BTC's mapped positions sit outside the ±30% the map spans, so a
heatmap without that count reads as the whole book when it is half of it.

`pcolormesh`, not `imshow`, and the x edges are real timestamps. `columns()`
bins by snapshot *index*, and capture rate varies about twofold across days, so
columns are unequal in wall-clock duration. Real edges make that visible instead
of quietly averaging it away.

**This decides nothing.** liqview's docstring says the moment it acquires a
threshold it stops being an instrument and becomes a detector nobody registered;
the same holds here, and the footer says so on every figure. `gate magnet` and
`gate accelerator` decide, under a pre-registration, on the ledger.

Usage:

    /usr/bin/python3 tools/liqfig.py --coin BTC --since 24h --out /tmp/btc.png
    /usr/bin/python3 tools/liqfig.py --coin ETH --since 3d --span 0.10 --out eth.png
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# `nat`'s feature store, read-only and by absolute path. This tool never writes
# to that repo and never imports from it; the two projects stay independent, as
# CASCADE_MICROSTRUCTURE_BRIDGE.md requires.
NAT_FEATURES = Path("/home/onat/nat/data/features")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy"))

import liqview  # noqa: E402  -- the path insert above has to land first

import matplotlib  # noqa: E402
matplotlib.use("Agg")  # noqa: E402  -- headless; must precede pyplot
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import LogNorm  # noqa: E402

# Raster, not terminal. liqview's 32x120 is a terminal budget, not a property of
# the data; the grid code is resolution-independent.
HEIGHT = 240
WIDTH = 720
# Empty reads as background, exactly as liqview's ramp starts at a space: the
# lowest visible cell and an absent one are both "nothing here", and inventing a
# colour for one of them would claim a distinction the p20 clip already erased.
GROUND = "#0d0d12"
# Money, escaped. Mathtext is left enabled because the colorbar's own decade
# labels are mathtext ("10^7"); disabling it globally prints those raw. So the
# dollar signs in *our* strings are escaped instead -- unescaped, mathtext eats
# everything between two of them and "p20 $343,906 .. p99 $21,385,463" renders
# as "p20 343,906..p9921,385,463", the two clip values fused into one number.
USD = r"\$"
# A display threshold, in liqview's sense -- it chooses whether pixels are
# painted, not whether anything is true -- and like liqview's glyph thresholds it
# is printed on the figure. Capture stutters (median cadence 64s, but multi-hour
# holes happen), and `pcolormesh` would smear the last snapshot before a hole
# across the whole hole as though the map had stood still.
GAP_FACTOR = 8.0
# Panel defaults: a signed book-asymmetry measure and a tick-entropy one. Starting
# points for the contact sheet, not a claim that these two are the informative ones.
DEFAULT_IMB = "imbalance_depth_weighted"
DEFAULT_ENT = "ent_tick_30s"


def nat_series(coin: str, since_ns: int, until_ns: int, cols: list[str]) -> dict:
    """`nat`'s 100 ms feature vector for one coin, read-only, by absolute path.

    `nat` is a separate repo and is never written to. Day directories are globbed
    *before* the dataset is constructed: pointing pyarrow at the 31 GB root and
    filtering afterwards reads every file to discard almost all of them.
    """
    import pyarrow.compute as pc
    import pyarrow.dataset as ds

    days = set()
    day = datetime.fromtimestamp(since_ns / liqview.NS, timezone.utc).date()
    end = datetime.fromtimestamp(until_ns / liqview.NS, timezone.utc).date()
    while day <= end:
        d = NAT_FEATURES / day.isoformat()
        if d.is_dir():
            days.add(str(d))
        day += timedelta(days=1)
    if not days:
        return {}

    files = sorted(f for d in days for f in Path(d).glob("*.parquet"))
    if not files:
        return {}
    table = ds.dataset([str(f) for f in files], format="parquet").to_table(
        columns=["timestamp_ns", *cols],
        filter=(ds.field("symbol") == coin)
        & (ds.field("timestamp_ns") >= since_ns)
        & (ds.field("timestamp_ns") <= until_ns),
    )
    if table.num_rows == 0:
        return {}
    order = pc.sort_indices(table, sort_keys=[("timestamp_ns", "ascending")])
    table = table.take(order)
    return {name: table.column(name).to_numpy(zero_copy_only=False)
            for name in table.column_names}


def reduce_to_columns(t_ns: np.ndarray, v: np.ndarray, edges: np.ndarray):
    """Mean, min and max of a 100 ms series within each heatmap column.

    This is a **display** aggregation and nothing else. A snapshot column is
    ~64 s of wall clock and the feature series is 100 ms, so a column swallows
    roughly 640 observations; the bridge doc's §2 point about per-row statistics
    overstating significance applies to any number read off this panel. The band
    is drawn so the mean is never mistaken for the range it came from.
    """
    idx = np.searchsorted(edges, t_ns / liqview.NS, side="right") - 1
    n = len(edges) - 1
    mean = np.full(n, np.nan)
    lo = np.full(n, np.nan)
    hi = np.full(n, np.nan)
    ok = (idx >= 0) & (idx < n) & np.isfinite(v)
    if not ok.any():
        return mean, lo, hi
    idx, v = idx[ok], v[ok]
    order = np.argsort(idx, kind="stable")
    idx, v = idx[order], v[order]
    bounds = np.searchsorted(idx, np.arange(n + 1))
    for i in range(n):
        a, b = bounds[i], bounds[i + 1]
        if b > a:
            chunk = v[a:b]
            mean[i], lo[i], hi[i] = chunk.mean(), chunk.min(), chunk.max()
    return mean, lo, hi


def _column_edges(binned: list[list[dict]]) -> np.ndarray:
    """Wall-clock edges of each column, in seconds since epoch.

    A column's left edge is its first snapshot. The right edge of the last column
    is its last snapshot, so the axis ends where the data ends rather than at an
    invented interval. Unequal widths are the point -- capture stutters, and the
    picture should show that rather than smooth it.
    """
    edges = [c[0]["t"] / liqview.NS for c in binned]
    edges.append(binned[-1][-1]["t"] / liqview.NS)
    return np.array(edges)


def figure(rows: list[dict], coin: str, view: str, span: float, out: Path,
           height: int = HEIGHT, width: int = WIDTH, dpi: int = 130,
           features: list[str] | None = None):
    features = features or []
    binned = liqview.columns(rows, width)
    cells, _, lo, hi = liqview.grid(rows, view, span, height, width)

    grid = np.array(cells, dtype=float)          # (height, n_columns), row 0 = axis floor
    seen = grid[grid > 0]
    floor = liqview._percentile(list(seen), 0.20)
    ceiling = liqview._percentile(list(seen), 0.99)
    # An all-empty or single-valued window would make LogNorm throw; say so rather
    # than ship a figure whose scale is a fiction.
    if seen.size == 0 or not (floor > 0 and ceiling > floor):
        raise SystemExit(f"{coin}: {seen.size} non-empty cells in view -- "
                         f"nothing to scale (try a wider --span)")

    x = _column_edges(binned)
    y = np.linspace(lo, hi, height + 1)
    masked = np.ma.masked_less_equal(grid, 0.0)

    # Blank the holes rather than stretch the last snapshot across them. A column
    # is one snapshot's map; a column 460 minutes wide is one snapshot's map drawn
    # as though it had been observed for 460 minutes.
    secs = np.diff(x)
    median = float(np.median(secs))
    gaps = secs > GAP_FACTOR * median
    masked[:, gaps] = np.ma.masked

    cmap = matplotlib.colormaps["magma"].with_extremes(bad=GROUND)
    nrows = 1 + len(features)
    fig, axes = plt.subplots(
        nrows, 1, sharex=True, constrained_layout=True,
        figsize=(14, 7 + 1.7 * len(features)),
        height_ratios=[3] + [1] * len(features) if features else None)
    axes = np.atleast_1d(axes)
    ax = axes[0]
    ax.set_facecolor(GROUND)
    mesh = ax.pcolormesh(x, y, masked, norm=LogNorm(vmin=floor, vmax=ceiling),
                         cmap=cmap, shading="flat")

    # True mark in data coordinates. The terminal has to quantise it to a row; a
    # figure with real axes does not, so this is the one place the pixel version
    # is allowed to be more faithful than the ASCII one.
    mx = np.array([(c[-1]["t"]) / liqview.NS for c in binned])
    my = np.array([c[-1]["mark"] if view == "absolute" else 0.0 for c in binned])
    # Break the mark line over the same holes, so it does not draw a straight
    # segment through hours nobody observed.
    my = np.ma.masked_where(gaps, my)
    ax.plot(mx, my, color="#39d0ff", lw=1.1, label="mark")

    first, last = rows[0], rows[-1]
    ax.set_xlim(x[0], x[-1])
    ax.set_ylim(lo, hi)
    ax.set_ylabel("price" if view == "absolute" else "distance from mark")
    if view == "relative":
        ax.yaxis.set_major_formatter(lambda v, _: f"{v * 100:+.1f}%")
    axes[-1].xaxis.set_major_formatter(
        lambda v, _: datetime.fromtimestamp(v, timezone.utc).strftime("%m-%d %H:%M"))
    axes[-1].tick_params(axis="x", rotation=0, labelsize=8)
    ax.legend(loc="upper left", fontsize=8, framealpha=0.3)

    # nat's microstructure, on the heatmap's own x axis. Read once for all panels.
    missing: list[str] = []
    if features:
        series = nat_series(coin, rows[0]["t"], rows[-1]["t"], features)
        for panel, name in zip(axes[1:], features):
            panel.set_ylabel(name, fontsize=7)
            panel.tick_params(labelsize=7)
            panel.grid(alpha=0.15, lw=0.5)
            if name not in series:
                missing.append(name)
                panel.text(0.5, 0.5, f"no nat data for {coin} in this window",
                           transform=panel.transAxes, ha="center", va="center",
                           fontsize=8, color="#999")
                continue
            mean, flo, fhi = reduce_to_columns(series["timestamp_ns"], series[name], x)
            centres = (x[:-1] + x[1:]) / 2
            # `nat` keeps recording through a liqmap hole -- the two captures are
            # independent -- so a gap column holds hours of feature data and its
            # min-max is the range over those hours. Blank the band on the same
            # columns as the heatmap, or the hole reads as a real excursion.
            blank = gaps | ~np.isfinite(mean)
            mean = np.ma.masked_where(blank, mean)
            panel.fill_between(centres, flo, fhi, where=~blank, color="#39d0ff",
                               alpha=0.18, lw=0, label="min-max in column")
            panel.plot(centres, mean, color="#0b6e8f", lw=0.9, label="mean")
            if float(np.nanmin(flo)) < 0 < float(np.nanmax(fhi)):
                panel.axhline(0, color="#888", lw=0.6, ls=":")
            panel.legend(loc="upper left", fontsize=6, framealpha=0.3, ncol=2)

    bar = fig.colorbar(mesh, ax=ax, pad=0.01)
    bar.set_label("cluster notional (USD, log)", fontsize=8)

    ax.set_title(
        f"{coin}   {liqview.iso(first['t'])} -> {liqview.iso(last['t'])}   "
        f"{len(rows)} snapshots   view={view} span=±{span * 100:.0f}%",
        fontsize=11, loc="left")

    held = secs[~gaps]
    gap_note = (f"{gaps.sum()} gap columns blanked (>{GAP_FACTOR:g}x median {median:.0f}s, "
                f"largest {secs.max() / 60:.0f} min)" if gaps.any() else "no gaps")
    feat_note = (f"\nnat panels: column mean, band = min-max in column "
                 f"(~{held.mean() / 0.1:,.0f} obs at 100ms)" if features else "")
    fig.text(
        0.005, -0.005,
        f"coverage {last['coverage']:.3f}   published {last['published_frac']:.3f}   "
        f"outside_span {last.get('outside_span')}/{last.get('positions')}   "
        f"bucket {last['bucket_pct'] * 100:.2f}%   mark {last['mark']:,.0f}\n"
        f"log scale clipped p20 {USD}{floor:,.0f} .. p99 {USD}{ceiling:,.0f} over visible cells "
        f"(moves with --span, --rows, --width)   column {held.min():.0f}-{held.max():.0f}s   "
        f"{gap_note}{feat_note}\n"
        f"descriptive; no threshold, no decision",
        fontsize=7.5, va="top", family="monospace", color="#444")

    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return {"floor": floor, "ceiling": ceiling, "columns": len(binned),
            "gaps": int(gaps.sum()), "missing": missing}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--coin", required=True)
    parser.add_argument("--since", default="24h")
    parser.add_argument("--until", default=None)
    parser.add_argument("--view", choices=("absolute", "relative"), default="absolute")
    parser.add_argument("--span", type=float, default=0.05,
                        help="half-height of the price axis, as a fraction of mark")
    parser.add_argument("--rows", type=int, default=HEIGHT)
    parser.add_argument("--width", type=int, default=WIDTH)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--imb", default=None, nargs="?", const=DEFAULT_IMB,
                        help=f"nat imbalance column (bare flag = {DEFAULT_IMB})")
    parser.add_argument("--ent", default=None, nargs="?", const=DEFAULT_ENT,
                        help=f"nat entropy column (bare flag = {DEFAULT_ENT})")
    parser.add_argument("--feature", action="append", default=[],
                        help="any other nat column; repeatable")
    args = parser.parse_args(argv)

    # Two panels is the cap by intent. Browsing 8 imbalance x 27 entropy columns is
    # a re-run, not thirty-five lines on one axis.
    features = [f for f in (args.imb, args.ent) if f] + args.feature

    now_ns = time.time_ns()
    since = liqview.parse_when(args.since, now_ns)
    until = liqview.parse_when(args.until, now_ns) if args.until else now_ns

    rows = liqview.snapshots(args.coin, since, until)
    if not rows:
        print(f"no {args.coin} snapshots in "
              f"{liqview.iso(since)} -> {liqview.iso(until)}", file=sys.stderr)
        return 1

    info = figure(rows, args.coin, args.view, args.span, args.out,
                  height=args.rows, width=args.width, features=features)
    print(f"{args.coin}  {len(rows)} snapshots  {info['columns']} columns  "
          f"{info['gaps']} gap columns  "
          f"p20 ${info['floor']:,.0f} .. p99 ${info['ceiling']:,.0f}  -> {args.out}")
    if info["missing"]:
        print(f"  no nat data for: {', '.join(info['missing'])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
