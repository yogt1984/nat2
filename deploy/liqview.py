#!/usr/bin/env python3
"""nat2 liqview -- did price move toward the cluster, and was the map lopsided first?

Renders the liquidation map over time as an ASCII heatmap with the mark price
drawn over it. It answers that question *by eye*. It decides nothing: `gate
magnet` tests the claim formally under a pre-registration, and the moment this
file acquires a threshold it stops being an instrument and becomes a detector
that nobody registered.

Stdlib only, on `/usr/bin/python3`. It joins gapwatch, statuspage and tapecheck
as an ops tool that must survive a broken venv -- the venv being broken is one
of the moments you most want to look at the tape. `zstandard` is a venv
package, so decompression shells out to `/usr/bin/zstd`. `--aa` shades through
aalib, a system library reached by `ctypes`, and a box without it draws the
block ramp instead -- optional in the same sense and for the same reason.

Two things here are less obvious than they look.

*   **Buckets are stored relative to a mark that moves.** `lo_pct` is a
    fraction of mark, so a cluster standing still in absolute price *drifts* in
    the relative view, and one that drifts stands still. Reading one view as
    though it were the other is the easiest way to see a magnet that is not
    there, so the header always names which is on screen and absolute is the
    default -- it is the view in which "price travelled to the cluster" is a
    statement about the picture rather than about the frame of reference.

*   **The scale is logarithmic and the number that maps to `@` is printed.**
    Cluster notional spans four orders of magnitude between adjacent buckets on
    real data; a linear ramp shows one hot cell and calls everything else empty.

Coverage is on every frame. The map is built from the wallets we can see --
about 38% of BTC open interest today -- and a heatmap that omits that invites
exactly the conclusion it cannot support.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
REGISTRY = ROOT / "data" / "registry.sqlite"
STREAM = "nat2.liqmap2"
MANIFEST = "_manifest.jsonl"
SUFFIX = ".ndjson.zst"
NS = 1_000_000_000

# Densest last. Ten levels is what a log scale over four decades can carry
# without pretending to a precision the eye cannot read.
# Five levels, not nine. Shade blocks come in three plus a full block, and a
# ramp with more steps than the eye can rank is precision theatre: the previous
# " .:-=+*#%@" could not be read without the legend, because `-`, `=` and `+`
# look directional and `#`, `%`, `@` do not order against each other at all.
RAMP = " ·░▒▓█"
MARK_GLYPH = "◆"
# For pipes, notes and anywhere a block might not render. Digits carry their own
# ordering, so the legend is optional rather than load-bearing.
RAMP_ASCII = " 12345"
MARK_ASCII = "o"

# Signed, for the asymmetry strip: which side of price the mass sits on.
IMB_RAMP = ("V", "v", "-", "^", "A")
# Presentation only. These decide nothing -- they choose a character -- and they
# are printed in the legend so nobody mistakes them for a signal. A threshold
# that gates a decision goes on the ledger first; this one gates a glyph.
IMB_MILD, IMB_STRONG = 0.20, 0.60
LIQ_SMALL, LIQ_LARGE = "x", "X"
LIQ_LATE = "!"
# Digits, for the realized-notional strip: they carry their own ordering, and
# the strip sits under a map whose own ramp is already doing the shading.
LIQ_RAMP = "123456789"
# aalib's ramp is 23 glyphs of ordinary ASCII, and it swallows two of the
# overlays: 'X' draws exactly like a dense cell and 'o' like a middling one. A
# large liquidation that cannot be told from a big cluster, on the picture whose
# entire subject is big clusters, is worse than not drawing it -- so under
# aalib tone these stand in, chosen from outside every ramp here.
MARK_ASCII_AA = "@"
LIQ_LARGE_AA = "*"
# Batched so 360 snapshots are a few subprocesses rather than 360 of them.
BATCH = 200


# --- the store -------------------------------------------------------------

def _salvage(record: str) -> dict | None:
    """A complete JSON object at the end of a damaged line, or None."""
    decoder = json.JSONDecoder()
    for start in range(len(record)):
        if record[start] != "{":
            continue
        try:
            value, end = decoder.raw_decode(record[start:])
        except json.JSONDecodeError:
            continue
        if start + end == len(record) and isinstance(value, dict):
            return value
    return None


def read_manifest(root: Path = RAW) -> list[dict]:
    """Manifest entries, tolerating both torn shapes the live store contains.

    A deliberate copy of the rule in `src/nat2/io/worm.py` rather than an
    import: this file must run when the venv does not. A crash leaves
    NUL-filled blocks; a full disk leaves a truncated line onto which the next
    start appends its own good entry.
    """
    path = root / MANIFEST
    if not path.exists():
        return []
    out, lines = [], path.read_text(errors="replace").splitlines()
    for i, line in enumerate(lines):
        record = line.strip("\x00 \t\r\n�")
        if not record:
            continue
        try:
            out.append(json.loads(record))
        except json.JSONDecodeError:
            recovered = _salvage(record)
            if recovered is not None:
                out.append(recovered)
            elif "\x00" in line or "�" in line or i == len(lines) - 1:
                continue
            else:
                raise
    return out


def parts_for(stream: str, since_ns: int, until_ns: int, root: Path = RAW) -> list[Path]:
    """Parts overlapping the window, manifested or not.

    The open part carries the newest snapshots and has no manifest entry yet,
    so a manifest-only selector would silently omit the present -- which is the
    part you are usually looking at.
    """
    claimed, chosen = set(), []
    for entry in read_manifest(root):
        if entry.get("stream") != stream:
            continue
        claimed.add(entry["path"])
        if entry["last_ingest"] >= since_ns and entry["first_ingest"] <= until_ns:
            chosen.append(root / entry["path"])
    directory = root / stream
    if directory.exists():
        for part in directory.rglob(f"*{SUFFIX}"):
            if str(part.relative_to(root)) in claimed:
                continue
            if part.stat().st_mtime * NS >= since_ns:      # unmanifested: mtime is all we have
                chosen.append(part)
    return sorted(set(chosen))


def _decompress(paths: list[Path]) -> list[str]:
    try:
        import zstandard                                    # noqa: PLC0415 - venv only
        out = []
        dctx = zstandard.ZstdDecompressor()
        for path in paths:
            with path.open("rb") as fh:
                out.extend(dctx.stream_reader(fh).read().decode().splitlines())
        return out
    except ImportError:
        pass
    out = []
    for i in range(0, len(paths), BATCH):
        done = subprocess.run(["zstd", "-dcq", *[str(p) for p in paths[i:i + BATCH]]],
                              capture_output=True, text=True)
        out.extend(done.stdout.splitlines())
    return out


def snapshots(coin: str, since_ns: int, until_ns: int, root: Path = RAW) -> list[dict]:
    """One row per snapshot for one coin, oldest first."""
    rows = []
    for line in _decompress(parts_for(STREAM, since_ns, until_ns, root)):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue                                        # an open part's torn tail
        t = record.get("t_ingest", 0)
        if not (since_ns <= t <= until_ns):
            continue
        for entry in (record.get("payload") or {}).get("coins", []):
            if entry.get("coin") != coin:
                continue
            rows.append({
                "t": t, "mark": entry["mark"], "coverage": entry.get("coverage"),
                "published_frac": entry.get("published_frac"),
                "buckets": entry.get("buckets") or [], "imb": entry.get("imb") or {},
                "span": entry.get("span"), "bucket_pct": entry.get("bucket_pct"),
                # Projected for `tools/liqfig.py`, which stamps them on every figure:
                # about half of BTC's mapped positions sit outside the ±30% span, so a
                # picture that omits the count overstates what it is showing. Unused
                # here -- the terminal frame has no room -- and additive, so nothing
                # downstream of `snapshots()` changes.
                "positions": entry.get("positions"), "outside_span": entry.get("outside_span"),
            })
    rows.sort(key=lambda r: r["t"])
    return rows


# --- the grid --------------------------------------------------------------

def columns(rows: list[dict], width: int,
            window: tuple[int, int] | None = None) -> list[list[dict]]:
    """Snapshots binned into terminal columns, oldest left.

    Binned by TIME, not by snapshot index, whenever the window is known. Index
    binning made every column carry the same number of snapshots, which silently
    closed up the gaps: a 23.7 h hole in the tape drew as one narrow seam
    between two dense columns, and the x axis had no scale at all. Time binning
    gives every column the same duration -- so a column is a unit that can be
    printed in the header -- and leaves a hole in the tape as blank columns,
    which is what it is. Empty buckets are KEPT, so the frame always spans the
    full window and always fills the width.
    """
    if window is None:                                      # legacy: index bins
        if len(rows) <= width:
            return [[r] for r in rows]
        per = len(rows) / width
        out: list[list[dict]] = [[] for _ in range(width)]
        for i, row in enumerate(rows):
            out[min(int(i / per), width - 1)].append(row)
        return [c for c in out if c]
    out = [[] for _ in range(width)]
    for row in rows:
        out[time_column(row["t"], window, width)].append(row)
    return out


def time_column(t: int, window: tuple[int, int], width: int) -> int:
    """Which character column a timestamp falls in.

    The one place this is computed, because it used to be computed in three and
    one of them was wrong. Snapshots were binned here, realized liquidations
    were placed by walking a list of the first timestamp of each NON-EMPTY
    column -- which is an index into the non-empty columns, not into the frame.
    Since time binning started keeping empty bins, every blank column to the
    left of an event shifted its glyph one column left: on the repo's own gapped
    fixture a liquidation at the end of the window drew at column 1 of 40, four
    hours early, in the middle of a hole where there is no price line to sit on.

    That is the same inversion `liquidations` refuses to make when it selects by
    event time rather than arrival, one step further down the pipe -- a cascade
    drawn before the move that caused it, on the one picture whose subject is
    whether the lopsidedness came first.
    """
    since, until = window
    return min(max((t - since) * width // ((until - since) or 1), 0), width - 1)


# Measured on su-35: 13.6 MB of hl.liqmap2 parts decompress and parse in 5.6 s.
# Every part holds all ~176 coins and we keep one, so the cost is bytes on disk,
# not snapshots kept. Conservative on purpose -- overestimating trims the window
# and says so, underestimating hangs the terminal for minutes with no warning.
SCAN_MB_S = 2.4


def part_span(stream: str, since_ns: int, until_ns: int,
              root: Path = RAW) -> list[tuple[int, int]]:
    """`(first_ingest, bytes)` per part in the window, oldest first."""
    claimed, out = set(), []
    for entry in read_manifest(root):
        if entry.get("stream") != stream:
            continue
        claimed.add(entry["path"])
        if entry["last_ingest"] >= since_ns and entry["first_ingest"] <= until_ns:
            path = root / entry["path"]
            if path.exists():
                out.append((entry["first_ingest"], path.stat().st_size))
    directory = root / stream
    if directory.exists():
        for part in directory.rglob(f"*{SUFFIX}"):
            if str(part.relative_to(root)) in claimed:
                continue
            stat = part.stat()
            if stat.st_mtime * NS >= since_ns:
                out.append((int(stat.st_mtime * NS), stat.st_size))
    return sorted(out)


def budget_window(since_ns: int, until_ns: int, budget_s: float,
                  root: Path = RAW) -> tuple[int, float, float]:
    """`(since, full_cost_s, kept_cost_s)` -- the widest window inside the budget.

    Walks the parts newest-first, accumulating bytes, and stops when the
    estimated scan would blow the budget. Returns the original `since` when the
    whole window fits, so the common case is untouched. The floor is a part's
    own `first_ingest`, never a part COUNT scaled by an assumed cadence -- parts
    run ~34/hour here, not one, and that assumption made the trim a no-op.
    """
    parts = part_span(STREAM, since_ns, until_ns, root)
    full = sum(n for _, n in parts) / 1e6 / SCAN_MB_S
    if budget_s <= 0 or full <= budget_s or not parts:
        return since_ns, full, full
    mb, floor = 0.0, parts[-1][0]
    for first, size in reversed(parts):
        if (mb + size / 1e6) / SCAN_MB_S > budget_s:
            break
        mb += size / 1e6
        floor = first
    return max(since_ns, floor), full, mb / SCAN_MB_S


def col_seconds(window: tuple[int, int], width: int) -> float:
    """Wall-clock seconds one character column stands for."""
    return (window[1] - window[0]) / NS / max(width, 1)


def grid(rows: list[dict], view: str, span: float, height: int, width: int,
         window: tuple[int, int] | None = None):
    """`(cells, mark_row, axis_lo, axis_hi)`.

    Absolute: the y axis is price and a cluster sits still. Relative: the y axis
    is distance from mark and price is the centre line.
    """
    binned = columns(rows, width, window)
    if view == "absolute":
        marks = [r["mark"] for r in rows]
        lo, hi = min(marks) * (1 - span), max(marks) * (1 + span)
    else:
        lo, hi = -span, span
    step = (hi - lo) / height if hi > lo else 1.0

    cells = [[0.0] * len(binned) for _ in range(height)]
    mark_row = []
    for x, bucket in enumerate(binned):
        if not bucket:                                      # a hole in the tape
            mark_row.append(None)
            continue
        for row in bucket:
            for entry in row["buckets"]:
                lo_pct, notional = entry[0], entry[1]
                value = row["mark"] * (1 + lo_pct) if view == "absolute" else lo_pct
                # floor, not int: int() truncates toward zero, so every bucket
                # in the half-row BELOW the axis floor mapped to row 0 and the
                # bottom of every frame read as a permanent cluster that is not
                # there. Out of range is dropped, never clamped -- a clamped
                # bucket is an observation moved to a price it was not at.
                y = math.floor((value - lo) / step)
                if 0 <= y < height:
                    cells[y][x] += notional
        last = bucket[-1]["mark"]
        centre = last if view == "absolute" else 0.0
        y = math.floor((centre - lo) / step)
        mark_row.append(min(max(y, 0), height - 1))
    return cells, mark_row, lo, hi


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(int(q * (len(ordered) - 1)), len(ordered) - 1)]


# --- aalib -----------------------------------------------------------------
#
# aalib turns a grayscale buffer into characters. Given a FLAT cell -- all four
# of its subpixels equal -- and no dithering, its glyph table is a strictly
# monotone 24-level brightness ramp, against the five the block ramp carries.
# That is the whole reason it is here: more levels on the same character grid.
#
# Flat and undithered are not presentation choices, they are the correctness of
# the picture. aalib is built to make PHOTOGRAPHS legible: it samples 2x2
# subpixels to pick glyphs by shape, and its default Floyd-Steinberg dithering
# spreads each cell's quantisation error into its neighbours. Both are wrong
# here. A cell is not a piece of a photograph, it is one price at one minute,
# and either feature makes its glyph a function of the cells AROUND it: measured
# on a real 92-column BTC frame, 21 of 624 distinct notionals drew as more than
# one character depending on what sat next to them. That is the phantom-cluster
# bug wearing a different hat, so the buffer is filled flat and dithering is off,
# and the glyph is a function of that cell alone.
#
# Optional by construction. liqview is an ops tool that must survive a broken
# venv, and a system library is a weaker dependency than that: a box without
# libaa falls back to the block ramp and the frame says so.
AA_SONAMES = ("libaa.so.1", "libaa.so")
AA_DRIVER = "mem_d"                     # aalib exports its drivers unprefixed
AA_NORMAL_MASK = 1                      # no bold, dim or reverse: plain 7-bit out
_AA_LIB: list = []                      # one slot: [] = untried, [None] = absent
_AA_RAMP: list = []


def _load_aa():
    """`(ctypes, lib, hardware_params, render_params)`, or None when absent.

    Looked up once per process. `ctypes` is imported here rather than at the top
    for the same reason `sqlite3` is: this file is a stdlib tool, and an import
    it can do without is an import it should not do at module scope.
    """
    if _AA_LIB:
        return _AA_LIB[0]
    loaded = None
    try:
        import ctypes

        lib = None
        for soname in AA_SONAMES:
            try:
                lib = ctypes.CDLL(soname)
                break
            except OSError:
                continue
        if lib is None:
            raise OSError("no libaa")

        class Hardware(ctypes.Structure):
            _fields_ = ([("font", ctypes.c_void_p), ("supported", ctypes.c_int)]
                        + [(name, ctypes.c_int) for name in (
                            "minwidth", "minheight", "maxwidth", "maxheight",
                            "recwidth", "recheight", "mmwidth", "mmheight",
                            "width", "height")]
                        + [("dimmul", ctypes.c_double), ("boldmul", ctypes.c_double)])

        class Render(ctypes.Structure):
            _fields_ = [("bright", ctypes.c_int), ("contrast", ctypes.c_int),
                        ("gamma", ctypes.c_float), ("dither", ctypes.c_int),
                        ("inversion", ctypes.c_int), ("randomval", ctypes.c_int)]

        lib.aa_init.restype = ctypes.c_void_p
        lib.aa_init.argtypes = [ctypes.c_void_p] * 3
        lib.aa_image.restype = lib.aa_text.restype = ctypes.POINTER(ctypes.c_ubyte)
        lib.aa_image.argtypes = lib.aa_text.argtypes = [ctypes.c_void_p]
        lib.aa_render.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_int] * 4
        lib.aa_close.argtypes = [ctypes.c_void_p]
        # Touched HERE, where the handler is, and not where they are used.
        # Probing the five FUNCTIONS is not enough: `_aa_field` also
        # dereferences two DATA symbols, and `in_dll` raises ValueError from
        # inside the render, outside any try -- so a stripped or stub libaa
        # tracebacked out of the frame instead of falling back to blocks, which
        # is the one thing this tool promised not to do.
        ctypes.c_char.in_dll(lib, AA_DRIVER)
        Hardware.in_dll(lib, "aa_defparams")
        # Spelled out rather than taken from aa_getrenderparams(), which returns
        # dither=2 (Floyd-Steinberg) and would make the frame depend on the
        # library's defaults -- and on a future libaa changing them.
        options = Render(bright=0, contrast=0, gamma=1.0, dither=0,
                         inversion=0, randomval=0)
        loaded = (ctypes, lib, Hardware, options)
    except (ImportError, OSError, AttributeError, ValueError):
        loaded = None
    _AA_LIB.append(loaded)
    return loaded


def overlay_glyphs(ascii_only: bool, used_aa: bool) -> tuple[str, str, str, str]:
    """`(mark, small, large, late)` -- the glyphs that are drawn OVER the tone.

    Every one of them has to be absent from the ramp underneath it. That is
    free with blocks, whose ramp is three shade characters and a full block, and
    it is not free with aalib, whose ramp is most of the ASCII set. Pinned by a
    test rather than by reading, because the ramp comes from the library's font
    tables and a different libaa could quietly reintroduce the collision.
    """
    mark = MARK_ASCII if ascii_only else MARK_GLYPH
    large = LIQ_LARGE
    if used_aa:
        large = LIQ_LARGE_AA
        if ascii_only:
            mark = MARK_ASCII_AA
    return mark, LIQ_SMALL, large, LIQ_LATE


def _aa_field(levels: list[list[float]], width: int, height: int) -> list[str] | None:
    """`levels` (0..1, row 0 at the BOTTOM) as `height` lines of `width` glyphs.

    Row 0 of the result is the TOP of the frame, matching what `render` emits.
    None means libaa is absent and the caller should draw the block ramp.
    """
    loaded = _load_aa()
    if loaded is None:
        return None
    ctypes, lib, Hardware, options = loaded
    # A copy: `aa_defparams` is the library's own global and other callers in
    # this process would inherit whatever we left in it.
    params = Hardware.from_buffer_copy(Hardware.in_dll(lib, "aa_defparams"))
    params.width, params.height, params.supported = width, height, AA_NORMAL_MASK
    # The memory driver renders into a text buffer instead of taking over the
    # terminal, so `frame` still returns a string and the tests still read it.
    context = lib.aa_init(ctypes.addressof(ctypes.c_char.in_dll(lib, AA_DRIVER)),
                          ctypes.addressof(params), None)
    if not context:
        return None
    context = ctypes.c_void_p(context)
    try:
        image, stride = lib.aa_image(context), 2 * width
        for py in range(2 * height):
            row, base = levels[height - 1 - py // 2], py * stride
            for x in range(width):
                shade = int(255 * row[x])
                image[base + 2 * x] = shade
                image[base + 2 * x + 1] = shade        # flat: no shape, no bleed
        lib.aa_render(context, ctypes.addressof(options), 0, 0, width, height)
        text = lib.aa_text(context)
        return [bytes(text[r * width:(r + 1) * width]).decode("latin1")
                for r in range(height)]
    finally:
        lib.aa_close(context)


def aa_ramp() -> str | None:
    """The library's own ramp, sparsest first -- or None when libaa is absent.

    Probed, never hardcoded. The glyphs come from aalib's font tables, so a
    different libaa build would quietly print a legend that is not the ramp on
    the screen above it, and on this file a legend that lies about the scale is
    the whole failure mode.
    """
    if _AA_RAMP:
        return _AA_RAMP[0]
    field = _aa_field([[b / 255 for b in range(256)]], 256, 1)
    ramp = None
    if field:
        line = field[0]
        ramp = "".join(c for i, c in enumerate(line) if i == 0 or c != line[i - 1])
    _AA_RAMP.append(ramp)
    return ramp


def render(rows: list[dict], view: str, span: float, height: int, width: int,
           colour: bool, events: list[dict] | None = None, ascii_only: bool = False,
           window: tuple[int, int] | None = None, aa: bool = False) -> str:
    """The heatmap body. `aa` asks for aalib tone, and gets the ramp without it."""
    return _render(rows, view, span, height, width, colour, events, ascii_only,
                   window, aa)[0]


def _render(rows: list[dict], view: str, span: float, height: int, width: int,
            colour: bool, events: list[dict] | None = None, ascii_only: bool = False,
            window: tuple[int, int] | None = None,
            aa: bool = False) -> tuple[str, bool]:
    """`(body, drawn_with_aalib)`.

    The flag exists so the legend can name the ramp that is actually on screen.
    `--aa` on a box without libaa draws blocks, and a legend claiming 24 levels
    over a five-level picture misstates the scale -- which on this file is the
    whole failure mode.
    """
    ramp = RAMP_ASCII if ascii_only else RAMP
    cells, mark_row, lo, hi = grid(rows, view, span, height, width, window)
    step = (hi - lo) / height if hi > lo else 1.0
    values = [v for line in cells for v in line if v > 0]
    peak = max(values, default=0.0)
    # Normalised across the log RANGE, not from zero. Dividing by log(peak)
    # puts a 1M cell and a 100M cell three ramp steps apart out of ten, so the
    # frame saturates and every cell reads "hot" -- the same failure a linear
    # ramp produces, only inverted.
    # Clipped to percentiles, not to min/max. A long tail of near-empty cells
    # drags the floor to a dollar, which puts every cell that carries real mass
    # into the top two ramp steps -- the frame reads as uniformly hot and shows
    # nothing. p20..p99 is what makes the structure visible; both ends are
    # printed in the legend so the scale is never implicit.
    floor, ceiling = _percentile(values, 0.20), _percentile(values, 0.99)
    lo_log = math.log10(floor) if floor > 0 else 0.0
    hi_log = math.log10(ceiling) if ceiling > floor else lo_log + 1.0
    width_log = (hi_log - lo_log) or 1.0

    # The same clipped log level the ramp is indexed by, handed to aalib as
    # brightness instead. One cell, one flat pixel block, one glyph -- so the two
    # paths draw the same scale and the legend can state it once for both.
    # Sized off the grid, never off `width`: with no window `columns` falls back
    # to index bins and drops the empty ones, so a frame can be narrower than the
    # terminal asked for. The ramp path iterates the row and never noticed.
    field, drawn = None, len(cells[0]) if cells and cells[0] else 0
    if aa and drawn:
        field = _aa_field(
            [[0.0 if value <= 0
              else min(max((math.log10(value) - lo_log) / width_log, 0.0), 1.0)
              for value in row] for row in cells], drawn, height)
    # Resolved once the ramp underneath is known, because which ramp is on the
    # screen decides which characters are still free to draw on top of it.
    mark, liq_small, liq_large, liq_late = overlay_glyphs(ascii_only, field is not None)

    # Realized events, placed by event time and price on the same axes.
    marks: dict[tuple[int, int], str] = {}
    if events:
        binned = columns(rows, width, window)
        # Only the legacy path needs these. Index bins have no shared time axis
        # to measure against -- their columns do not have equal duration -- so
        # there the compacted walk is all there is, and it is right there
        # because `columns` drops its empty bins on that path too.
        edges = [b[0]["t"] for b in binned if b] if window is None else []
        big = _percentile(sorted(e["notional"] for e in events), 0.75)
        # Against the WINDOW, not the column. Nearly every event is late by
        # more than one column -- BTC averages 1,114 s and a column here is
        # ~145 s -- so a per-column test marks all of them and the glyph says
        # nothing. What matters is an event that arrived after the window it
        # belongs to had already closed: that one was invisible to anyone
        # watching live, and is the reason a frame redrawn tomorrow differs.
        window_s = ((rows[-1]["t"] - rows[0]["t"]) / NS) or 1.0
        for event in events:
            x = (time_column(event["t"], window, len(binned)) if window is not None
                 else max(0, min(len([e for e in edges if e <= event["t"]]) - 1,
                                 len(binned) - 1)))
            y = math.floor((event["px"] - lo) / step) if view == "absolute" else None
            if y is None:
                near = [b for b in binned[x:] + binned[:x] if b]
                if not near:
                    continue
                centre = near[0][-1]["mark"]
                y = math.floor(((event["px"] - centre) / centre - lo) / step)
            if not (0 <= y < height):
                continue
            glyph = liq_late if event["late_s"] > window_s else (
                liq_large if event["notional"] >= big else liq_small)
            marks[(y, x)] = glyph

    out = []
    for y in range(height - 1, -1, -1):                     # price increases upward
        line = []
        for x, value in enumerate(cells[y]):
            glyph = " "
            if field is not None:
                glyph = field[height - 1 - y][x]
            elif value > 0:
                level = (math.log10(value) - lo_log) / width_log
                glyph = ramp[min(max(int(level * (len(ramp) - 1)), 0), len(ramp) - 1)]
            if (y, x) in marks:
                glyph = marks[(y, x)]
            elif mark_row[x] == y:
                glyph = f"\033[93m{mark}\033[0m" if colour else mark
            line.append(glyph)
        label = (hi - (hi - lo) * (height - 1 - y) / max(height - 1, 1))
        axis = f"{label:>10,.0f}" if view == "absolute" else f"{label * 100:>+9.1f}%"
        out.append(f"{axis} |{''.join(line)}")
    return "\n".join(out), field is not None


def imb_strip(rows: list[dict], bands: list[str], width: int,
              window: tuple[int, int] | None = None) -> list[tuple[str, str]]:
    """One row per band: which way the map leans, over time.

    `imb` is already on every snapshot, per band, so nothing is derived here --
    it is read and drawn. The point is temporal ordering: whether the
    lopsidedness came before the move or after it, which is the whole
    difference between a magnet and a coincidence.
    """
    binned = columns(rows, width, window)
    out = []
    for band in bands:
        line = []
        for bucket in binned:
            values = [b["imb"].get(band) for b in bucket if b["imb"].get(band) is not None]
            if not values:
                line.append(" ")
                continue
            value = sum(values) / len(values)
            if abs(value) < IMB_MILD:
                line.append(IMB_RAMP[2])
            elif abs(value) < IMB_STRONG:
                line.append(IMB_RAMP[3] if value > 0 else IMB_RAMP[1])
            else:
                line.append(IMB_RAMP[4] if value > 0 else IMB_RAMP[0])
        out.append((band, "".join(line)))
    return out


def liq_strip(events: list[dict] | None, window: tuple[int, int],
              width: int) -> tuple[str, float, float] | None:
    """`(line, floor, peak)` -- realized notional per column -- or None.

    The map cannot carry this and the overlay glyphs only look as though it can.
    A liquidation prints AT the mark, so every `x` lands on the price path: a
    cascade draws as one unreadable column welded to the line, and a print
    outside the price span does not draw at all. Read up, that says "these
    minutes were liquidating" and nothing about how much. The strip is where the
    size lives, on the same time bins as the frame above it.
    """
    if not events:
        return None
    # Through `time_column`, the same call the map's own overlay makes. The
    # strip is only worth anything read against the frame, and two binnings that
    # disagree put a cascade next to a move that did not cause it.
    per = [0.0] * width
    for event in events:
        per[time_column(event["t"], window, width)] += event["notional"]
    seen = [v for v in per if v > 0]
    if not seen:
        return None
    floor, peak = min(seen), max(seen)
    lo_log = math.log10(floor)
    width_log = (math.log10(peak) - lo_log) or 1.0
    top = len(LIQ_RAMP) - 1
    line = "".join(
        " " if v <= 0 else LIQ_RAMP[min(int((math.log10(v) - lo_log) / width_log * top), top)]
        for v in per)
    return line, floor, peak


def liquidations(coin: str, since_ns: int, until_ns: int,
                 db: Path = REGISTRY) -> list[dict]:
    """Realized liquidations in the window, by EVENT time.

    Never by arrival: BTC events land 1,114 s late on average and 3,665 s at
    worst on this box, so drawn by `t_ingest` a cascade appears after the move
    that caused it and the picture invents causality backwards.

    Opened read-only. A live daemon owns this file and this tool renders.
    """
    import sqlite3

    if not db.exists():
        return []
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT t_event, px, sz, method, t_ingest FROM liquidations"
                " WHERE coin = ? AND t_event BETWEEN ? AND ? ORDER BY t_event",
                (coin, since_ns, until_ns)).fetchall()
    except sqlite3.Error:
        return []
    return [{"t": t, "px": px, "notional": (px or 0) * (sz or 0), "method": method,
             "late_s": (ingest - t) / NS} for t, px, sz, method, ingest in rows]


# --- framing ---------------------------------------------------------------

def earliest_ns(stream: str = STREAM, root: Path = RAW) -> int | None:
    """When this stream's history starts. Asking for more than exists should
    clamp and say so, not render an empty frame that reads as 'no clusters'."""
    starts = [e["first_ingest"] for e in read_manifest(root) if e.get("stream") == stream]
    return min(starts) if starts else None


def parse_when(text: str, now_ns: int) -> int:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhdw])", text.strip())
    if match:
        unit = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]
        return now_ns - int(float(match.group(1)) * unit * NS)
    moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * NS)


def iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / NS, timezone.utc).strftime("%m-%d %H:%M:%SZ")


def human_s(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.1f} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} d"


def frame(rows: list[dict], coin: str, view: str, span: float, height: int,
          width: int, colour: bool, bands: list[str] | None = None,
          events: list[dict] | None = None, ascii_only: bool = False,
          window: tuple[int, int] | None = None, aa: bool = False) -> str:
    first, last = rows[0], rows[-1]
    window = window or (first["t"], last["t"])
    cells, _, _, _ = grid(rows, view, span, height, width, window)
    seen = [v for line in cells for v in line if v > 0]
    peak = max(seen, default=0.0)
    floor, ceiling = _percentile(seen, 0.20), _percentile(seen, 0.99)
    per_col = col_seconds(window, width)
    filled = sum(1 for b in columns(rows, width, window) if b)
    head = (f"{coin}  {iso(window[0])} -> {iso(window[1])}  "
            f"{human_s((window[1] - window[0]) / NS)} in {width} cols  "
            f"view={view} span=±{span * 100:.0f}%")
    scale = (f"resolution {human_s(per_col)} per column   {len(rows)} snapshots   "
             f"{filled}/{width} columns carry data"
             + (f"   {width - filled} blank = no snapshot in that slice"
                if filled < width else ""))
    qual = (f"coverage {last['coverage']:.3f}   published {last['published_frac']:.3f}   "
            f"bucket {last['bucket_pct'] * 100:.2f}%   mark {last['mark']:,.0f}")
    body, used_aa = _render(rows, view, span, height, width, colour, events,
                            ascii_only, window, aa)
    realized = liq_strip(events, window, width)
    if realized:
        body += f"\n{'liq $':>10} |{realized[0]}"
    if bands:
        strip = imb_strip(rows, bands, width, window)
        body += "\n" + "\n".join(f"{'imb ' + b:>10} |{line}" for b, line in strip)
    ramp = RAMP_ASCII if ascii_only else RAMP
    if used_aa:
        ramp = aa_ramp() or ramp
    mark, liq_small, liq_large, liq_late = overlay_glyphs(ascii_only, used_aa)
    legend = (f"  '{ramp.strip()}' sparse..dense, log scale: "
              f"${floor:,.0f} .. ${ceiling:,.0f} per cell (peak ${peak:,.0f})   "
              f"'{mark}' = price")
    lines = [head, scale, qual, "", body, "", legend]
    if used_aa:
        lines.append(f"  aalib tone: {len(ramp.strip())} levels where the block ramp has "
                     f"{len(RAMP.strip())}, one flat cell per glyph and no dithering, so a "
                     "glyph reports its own cell and no other")
    elif aa:
        # Two different failures, and the note has to say which. Falling back is
        # fine; blaming a missing library for a library that is sitting right
        # there sends whoever reads it to install something they already have.
        lines.append("  (--aa asked for aalib tone; "
                     + ("libaa is not installed here" if _load_aa() is None
                        else "libaa would not render at this size")
                     + ", so this frame is the block ramp)")
    if bands:
        lines.append(f"  imb '{IMB_RAMP[0]}{IMB_RAMP[1]}{IMB_RAMP[2]}{IMB_RAMP[3]}{IMB_RAMP[4]}'"
                     f" = down .. balanced .. up (|imb| < {IMB_MILD} / < {IMB_STRONG};"
                     " presentation, not a signal)")
    if events is not None:
        window_s = ((rows[-1]["t"] - rows[0]["t"]) / NS) or 1.0
        beyond = sum(1 for e in events if e["late_s"] > window_s)
        median_late = _percentile(sorted(e["late_s"] for e in events), 0.50)
        lines.append(f"  '{liq_small}/{liq_large}' = realized liquidation (small/large), "
                     f"'{liq_late}' arrived after this window closed")
        if realized:
            lines.append(f"  liq $ '{LIQ_RAMP}' = realized notional per column, log scale "
                         f"${realized[1]:,.0f} .. ${realized[2]:,.0f}; a liquidation prints at "
                         "the mark, so the map shows when and this shows how much")
        lines.append(f"  {len(events)} observed, median arrival {median_late / 60:.0f} min "
                     f"after the event, {beyond} later than the window itself")
        lines.append("  observed liquidations, not all liquidations: liqscan reads userFills, "
                     "capped at 2,000 per wallet")
    lines.append("  the map is built from the wallets we can see; "
                 f"coverage {last['coverage']:.0%} of open interest")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="liquidation map over time, in the terminal")
    parser.add_argument("--coin", required=True)
    parser.add_argument("--since", default="max",
                        help="window back from now; default 'max' = everything on disk")
    parser.add_argument("--until", default=None)
    parser.add_argument("--view", choices=("absolute", "relative"), default="absolute")
    parser.add_argument("--span", type=float, default=0.05,
                        help="half-height of the price axis as a fraction of mark")
    parser.add_argument("--rows", type=int, default=0, help="0 = fit the terminal")
    parser.add_argument("--width", type=int, default=0, help="0 = fit the terminal")
    parser.add_argument("--ascii", action="store_true",
                        help="digits instead of blocks, and no colour; for pipes and notes")
    parser.add_argument("--aa", action="store_true",
                        help="shade through aalib: 23 ASCII tone levels instead of 5. Needs "
                             "libaa (Debian libaa1, Arch aalib); without it the frame draws "
                             "the block ramp and says so")
    parser.add_argument("--bands", default="",
                        help="comma-separated imb bands to strip, e.g. 0.01,0.02,0.05")
    parser.add_argument("--liquidations", action="store_true",
                        help="overlay realized liquidations, placed by event time")
    parser.add_argument("--budget", type=float, default=45.0,
                        help="seconds of scanning to spend; 0 = no limit. A window "
                             "that would cost more is trimmed, and the frame says so")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    now = int(datetime.now(timezone.utc).timestamp() * NS)
    until = parse_when(args.until, now) if args.until else now
    floor_ns = earliest_ns()
    if args.since.strip().lower() in ("max", "all"):
        if floor_ns is None:
            print(f"no {STREAM} history in {RAW}", file=sys.stderr)
            return 1
        since, clamped = floor_ns, False
    else:
        since = parse_when(args.since, until)
        # Clamped, and the header says so. A window that opens before the tape
        # does is not an error, but rendering it silently makes the empty rows
        # look like absent clusters rather than absent data.
        clamped = floor_ns is not None and since < floor_ns
        since = max(since, floor_ns) if floor_ns is not None else since
    asked_since = since
    since, full_cost, kept_cost = budget_window(since, until, args.budget)
    trimmed = since > asked_since
    rows = snapshots(args.coin, since, until)
    if not rows:
        print(f"no {STREAM} snapshots for {args.coin} in {iso(since)} -> {iso(until)}",
              file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(rows, separators=(",", ":")))
        return 0

    term = shutil.get_terminal_size((100, 40))
    width = args.width or max(40, min(term.columns - 13, 220))
    colour = not args.ascii and sys.stdout.isatty()
    bands = [b.strip() for b in args.bands.split(",") if b.strip()]
    events = liquidations(args.coin, since, until) if args.liquidations else None
    # Fill the display: the axis, the two header lines, the blanks, the legend
    # and the notes are the fixed cost; everything else is price rows. The strip
    # and the line explaining it are two more rows the map does not get -- but
    # only when there is a strip. `--liquidations` over a quiet window draws
    # none, and reserving for it there spends two price rows on nothing.
    height = args.rows or max(8, term.lines - (
        12 + len(bands) + (2 if events else 0) + (1 if args.aa else 0)))
    if clamped:
        print(f"  (asked for more history than exists; clamped to {iso(asked_since)})")
    if trimmed:
        print(f"  (the full window back to {iso(asked_since)} would take ~{full_cost:.0f} s "
              f"to scan, over the {args.budget:.0f} s budget; showing the last "
              f"{(until - since) / NS / 3600:.1f} h at ~{kept_cost:.0f} s. "
              f"--budget 0 renders all of it)")
    print(frame(rows, args.coin, args.view, args.span, height, width, colour, bands,
                events, ascii_only=args.ascii, window=(since, until), aa=args.aa))
    return 0


if __name__ == "__main__":
    sys.exit(main())
