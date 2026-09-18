"""The liquidation-map renderer, and the two ways it lied before these existed.

`deploy/liqview.py` draws a picture that a human then reasons from, which makes
a wrong picture more dangerous than a wrong number: nobody sanity-checks a
heatmap against arithmetic. Both bugs these tests pin were found by staring at
real output, not by testing -- and one of them, a phantom cluster welded to the
bottom of every frame, would have survived a week of daily use and become
something you could tell a story about.

Loaded by path, like tests/test_gapwatch.py and tests/test_tapecheck.py:
`deploy/` is outside the package on purpose, because these tools must run when
the venv does not.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from nat2.io.worm import WormWriter

spec = importlib.util.spec_from_file_location(
    "liqview", Path(__file__).resolve().parent.parent / "deploy" / "liqview.py"
)
liqview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(liqview)

NS = 1_000_000_000
T0 = 1_755_000_000 * NS
STREAM = "nat2.liqmap2"


def _snapshot(mark: float, buckets: list[list[float]], coin: str = "BTC") -> dict:
    return {"coins": [{
        "coin": coin, "mark": mark, "coverage": 0.38, "published_frac": 0.64,
        "span": 0.30, "bucket_pct": 0.0025, "imb": {"0.01": 0.0},
        "buckets": buckets, "near": {},
    }]}


def _store(tmp_path: Path, frames: list[tuple[float, list[list[float]]]]) -> Path:
    """A real WORM store: real parts, a real manifest, one snapshot per part —
    exactly how mapsnap writes it."""
    for i, (mark, buckets) in enumerate(frames):
        with WormWriter(tmp_path, STREAM) as writer:
            writer.write(_snapshot(mark, buckets), t_event=None, t_ingest=T0 + i * 60 * NS)
    return tmp_path


def _ramp(n: int = 20, cluster_price: float = 105.0):
    """A cluster pinned at one ABSOLUTE price, and a mark that walks up past it.

    In absolute space the cluster is a horizontal line the mark crosses. In
    mark-relative space it is a diagonal converging on zero. Same input, and
    reading one as the other is how you see a magnet that is not there.
    """
    frames = []
    for i in range(n):
        mark = 100.0 + i * 0.5
        frames.append((mark, [[(cluster_price - mark) / mark, 1_000_000.0, 0.0, 3]]))
    return frames


def _window(rows):
    return rows[0]["t"], rows[-1]["t"]


# --- the slice --------------------------------------------------------------

def test_the_stdlib_reader_finds_every_snapshot(tmp_path):
    _store(tmp_path, _ramp(12))
    rows = liqview.snapshots("BTC", T0, T0 + 12 * 60 * NS, root=tmp_path)
    assert len(rows) == 12
    assert [r["mark"] for r in rows] == [100.0 + i * 0.5 for i in range(12)]
    assert all(r["buckets"] for r in rows)


def test_both_decompression_paths_agree(tmp_path, monkeypatch):
    """`zstandard` is a venv package and is NOT importable from /usr/bin/python3,
    so the `zstd` CLI is the path production actually takes. The tests run in
    the venv, so without this the production path is never exercised."""
    _store(tmp_path, _ramp(6))
    fast = liqview.snapshots("BTC", T0, T0 + 6 * 60 * NS, root=tmp_path)

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def no_zstandard(name, *a, **k):
        if name == "zstandard":
            raise ImportError("forced: exercise the /usr/bin/zstd path")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", no_zstandard)
    slow = liqview.snapshots("BTC", T0, T0 + 6 * 60 * NS, root=tmp_path)
    assert fast == slow and len(slow) == 6


def test_a_crash_torn_manifest_line_does_not_hide_the_stream(tmp_path):
    _store(tmp_path, _ramp(4))
    manifest = tmp_path / "_manifest.jsonl"
    lines = manifest.read_text().splitlines()
    manifest.write_text("\n".join([lines[0], "\x00" * 200, *lines[1:]]) + "\n")
    assert len(liqview.snapshots("BTC", T0, T0 + 4 * 60 * NS, root=tmp_path)) >= 3


# --- the two views ----------------------------------------------------------

def test_absolute_holds_the_cluster_still_and_relative_makes_it_a_diagonal(tmp_path):
    _store(tmp_path, _ramp(20, cluster_price=105.0))
    rows = liqview.snapshots("BTC", *_window(liqview.snapshots("BTC", T0, T0 + 3600 * NS,
                                                               root=tmp_path)), root=tmp_path)

    absolute, _, _, _ = liqview.grid(rows, "absolute", 0.10, 20, 20)
    hot_abs = [max(range(20), key=lambda y: absolute[y][x]) for x in range(20)]
    assert len(set(hot_abs)) == 1, f"absolute: the cluster must not move, got {set(hot_abs)}"

    relative, _, _, _ = liqview.grid(rows, "relative", 0.10, 20, 20)
    hot_rel = [max(range(20), key=lambda y: relative[y][x]) for x in range(20)]
    assert len(set(hot_rel)) > 1, "relative: the cluster must drift as the mark moves"
    assert hot_rel == sorted(hot_rel, reverse=True) or hot_rel == sorted(hot_rel)


# --- the bug that welded a cluster to the floor -----------------------------

def test_a_bucket_below_the_axis_is_dropped_not_clamped_into_the_bottom_row(tmp_path):
    """`int()` truncates toward zero, so every bucket in the half-row BELOW the
    floor landed in row 0. On a real 6 h BTC frame that invented about $58M of
    mass at the bottom of every picture — a permanent cluster that was not
    there, and one no test would have caught."""
    # Just below the floor, not far below it. With mark 100 and span 2% the
    # axis starts at 98 and a row is 0.25 wide, so a bucket at 97.9 gives
    # y = -0.4: int() truncates that to 0 and welds it to the bottom row, while
    # floor() gives -1 and drops it. Far-below buckets are dropped by BOTH, so
    # a fixture that uses them proves nothing -- as the first version of this
    # test did, passing happily against the bug it was written for.
    just_below = [[-0.021, 500_000_000.0, 0.0, 99]]     # -> price 97.9, axis floor 98.0
    _store(tmp_path, [(100.0, just_below) for _ in range(6)])
    rows = liqview.snapshots("BTC", T0, T0 + 6 * 60 * NS, root=tmp_path)

    cells, _, _, _ = liqview.grid(rows, "absolute", 0.02, 16, 6)
    assert sum(cells[0]) == 0.0, "a bucket outside the axis must vanish, not sink to row 0"
    assert sum(v for line in cells for v in line) == 0.0


def test_a_bucket_inside_the_axis_still_lands(tmp_path):
    # The guard above must not have been achieved by dropping everything.
    _store(tmp_path, [(100.0, [[0.0, 7_000_000.0, 0.0, 2]]) for _ in range(4)])
    rows = liqview.snapshots("BTC", T0, T0 + 4 * 60 * NS, root=tmp_path)
    cells, _, _, _ = liqview.grid(rows, "absolute", 0.02, 16, 4)
    assert sum(v for line in cells for v in line) == pytest.approx(4 * 7_000_000.0)


# --- the scale --------------------------------------------------------------

def test_the_ramp_does_not_saturate_on_a_decade_spanning_bulk(tmp_path):
    """The fixture is the test. Three earlier versions of it passed against the
    very bug it was written for.

    Saturation needs a bulk that spans decades AND a floor dragged to nothing by
    a low tail: `log(v)/log(peak)` then maps the whole bulk into the top of the
    ramp. An evenly-spread fixture does not reproduce it, and neither does a
    narrow bulk -- a narrow bulk genuinely has little dynamic range, and
    compressing it is correct. Measured on this shape: the saturating scale puts
    the median glyph at index 7.0 of 9 with two glyphs carrying 60% of the ink;
    percentile clipping puts it at 4.5 and uses all nine.
    """
    import statistics

    bulk = [[i * 0.0015, 1_000_000.0 * (10 ** (2 * i / 45)), 0.0, 2] for i in range(45)]
    tail = [[-0.0015 * (i + 1), 3.0, 0.0, 1] for i in range(5)]
    _store(tmp_path, [(100.0, bulk + tail) for _ in range(10)])
    rows = liqview.snapshots("BTC", T0, T0 + 10 * 60 * NS, root=tmp_path)

    art = liqview.render(rows, "relative", 0.08, 24, 30, colour=False)
    idx = [liqview.RAMP.index(c) for c in art if c in liqview.RAMP.strip()]
    assert idx, "nothing rendered"
    # Re-measured when the ramp went from nine steps to five: the fixed scale
    # uses all five with a median index of 2.5, the saturating one collapses to
    # two with a median of 4.0. Both bounds sit between those, so each still
    # discriminates -- checked by reverting the scale, not by assuming.
    median = statistics.median(idx)
    assert median <= 3.0, (
        f"the ramp saturated: median glyph index {median} of {len(liqview.RAMP) - 1}")
    assert len(set(idx)) >= 4, f"the ramp must use its range, got {len(set(idx))} levels"


# --- the frame --------------------------------------------------------------

def test_no_data_is_not_no_clusters(tmp_path, monkeypatch, capsys):
    """An empty grid reads as 'there are no clusters here', which is a far
    stronger claim than 'there is no data here'."""
    monkeypatch.setattr(liqview, "RAW", tmp_path)
    assert liqview.main(["--coin", "BTC", "--since", "1h"]) == 1
    assert "no" in capsys.readouterr().err.lower()


def test_the_header_always_names_the_view_and_the_coverage(tmp_path):
    _store(tmp_path, _ramp(10))
    rows = liqview.snapshots("BTC", T0, T0 + 10 * 60 * NS, root=tmp_path)
    for view in ("absolute", "relative"):
        frame = liqview.frame(rows, "BTC", view, 0.05, 12, 40, colour=False)
        assert f"view={view}" in frame
        assert "coverage 0.380" in frame
        assert "wallets we can see" in frame


@pytest.mark.parametrize("width,rows_n", [(20, 1), (80, 12), (200, 40)])
def test_it_renders_at_any_terminal_size(tmp_path, width, rows_n):
    _store(tmp_path, _ramp(15))
    rows = liqview.snapshots("BTC", T0, T0 + 15 * 60 * NS, root=tmp_path)
    art = liqview.render(rows, "absolute", 0.05, rows_n, width, colour=False)
    lines = art.splitlines()
    assert len(lines) == rows_n
    assert all(len(line) <= width + 13 for line in lines), "must not wrap"


# --- the contract -----------------------------------------------------------

def test_liqview_never_writes():
    """It renders. The moment it can write, it can be the thing that broke the
    record it was built to look at."""
    import re

    source = (Path(__file__).resolve().parent.parent / "deploy" / "liqview.py").read_text()
    for forbidden in ("write_text(", "write_bytes(", "mkdir(", "unlink(", "shutil.rmtree",
                      "os.replace", "os.remove"):
        assert forbidden not in source, f"liqview must not {forbidden}"
    # A mode string inside an open() call, not any 'w' anywhere -- the naive
    # version matched `{"w": 604800}` in the window parser.
    opened = re.findall(r"""\.open\(\s*["']([^"']*)["']""", source)
    assert opened, "expected at least one open() to check"
    assert all(set(mode) <= {"r", "b", "t"} for mode in opened), f"write mode in {opened}"


# --- the asymmetry strip (task 04) ------------------------------------------

def _imb_store(tmp_path: Path, values: list[float]) -> Path:
    for i, value in enumerate(values):
        payload = {"coins": [{
            "coin": "BTC", "mark": 100.0, "coverage": 0.38, "published_frac": 0.64,
            "span": 0.30, "bucket_pct": 0.0025, "imb": {"0.01": value},
            "buckets": [[0.0, 1_000_000.0, 0.0, 1]], "near": {},
        }]}
        with WormWriter(tmp_path, STREAM) as writer:
            writer.write(payload, t_event=None, t_ingest=T0 + i * 60 * NS)
    return tmp_path


def test_the_strip_shows_which_side_the_mass_is_on(tmp_path):
    _imb_store(tmp_path, [-0.9, -0.4, 0.0, 0.4, 0.9])
    rows = liqview.snapshots("BTC", T0, T0 + 5 * 60 * NS, root=tmp_path)
    (band, line), = liqview.imb_strip(rows, ["0.01"], width=5)
    assert band == "0.01"
    assert line == "Vv-^A", f"got {line!r}"


def test_the_strip_is_column_aligned_with_the_heatmap(tmp_path):
    """The whole value of the strip is reading it against the frame above it:
    whether the lopsidedness came before the move or after."""
    _imb_store(tmp_path, [0.9] * 30)
    rows = liqview.snapshots("BTC", T0, T0 + 30 * 60 * NS, root=tmp_path)
    art = liqview.render(rows, "absolute", 0.05, 8, 17, colour=False)
    (_, line), = liqview.imb_strip(rows, ["0.01"], width=17)
    assert len(line) == len(art.splitlines()[0].split("|", 1)[1])


# --- realized liquidations (task 05) ----------------------------------------

def _registry(tmp_path: Path, events: list[tuple[int, float, float, int]]) -> Path:
    import sqlite3

    db = tmp_path / "registry.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE liquidations (tid INTEGER PRIMARY KEY, t_event INTEGER,"
                     " coin TEXT, liquidated_user TEXT, mark_px REAL, method TEXT,"
                     " px REAL, sz REAL, observer TEXT, source TEXT, t_ingest INTEGER)")
        conn.executemany(
            "INSERT INTO liquidations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(i, t, "BTC", "0xa", px, "market", px, sz, "0xo", "counterparty", ingest)
             for i, (t, px, sz, ingest) in enumerate(events)])
    return db


def test_liquidations_are_selected_by_event_time_not_arrival(tmp_path):
    """The one that matters. BTC events land a median of 7 minutes after they
    happen and up to an hour late; selected or placed by `t_ingest`, a cascade
    appears AFTER the move that caused it and the frame invents causality
    backwards."""
    inside_late = (T0 + 60 * NS, 100.0, 1.0, T0 + 3600 * NS)     # happened in, arrived after
    outside_early = (T0 - 3600 * NS, 100.0, 1.0, T0 + 120 * NS)  # happened before, arrived in
    db = _registry(tmp_path, [inside_late, outside_early])

    found = liqview.liquidations("BTC", T0, T0 + 600 * NS, db=db)
    assert len(found) == 1, "selection must be on t_event"
    assert found[0]["t"] == inside_late[0]
    assert found[0]["late_s"] == pytest.approx(3540.0)


def test_a_liquidation_is_drawn_at_the_price_it_happened(tmp_path):
    _store(tmp_path, [(100.0, [[0.0, 1_000_000.0, 0.0, 1]]) for _ in range(8)])
    rows = liqview.snapshots("BTC", T0, T0 + 8 * 60 * NS, root=tmp_path)
    events = [{"t": rows[4]["t"], "px": 101.0, "notional": 5000.0, "method": "market",
               "late_s": 10.0}]
    art = liqview.render(rows, "absolute", 0.05, 20, 8, colour=False, events=events)
    assert liqview.LIQ_SMALL in art or liqview.LIQ_LARGE in art
    # the row it lands on must be above the mark row, since 101 > 100
    lines = art.splitlines()
    liq_row = next(i for i, l in enumerate(lines)
                   if liqview.LIQ_SMALL in l or liqview.LIQ_LARGE in l)
    mark_row = next(i for i, l in enumerate(lines) if liqview.MARK_GLYPH in l)
    assert liq_row < mark_row, "a higher price must draw higher on the frame"


def test_the_late_glyph_marks_the_window_not_the_column(tmp_path):
    """Nearly every event is late by more than one column -- BTC averages 1,114 s
    against ~145 s columns -- so a per-column test marks all of them and the
    glyph says nothing. It marks events that arrived after the window closed."""
    _store(tmp_path, [(100.0, [[0.0, 1_000_000.0, 0.0, 1]]) for _ in range(10)])
    rows = liqview.snapshots("BTC", T0, T0 + 10 * 60 * NS, root=tmp_path)
    window_s = (rows[-1]["t"] - rows[0]["t"]) / NS

    ordinary = [{"t": rows[5]["t"], "px": 100.0, "notional": 1.0, "method": "m",
                 "late_s": window_s / 2}]
    beyond = [{"t": rows[5]["t"], "px": 100.0, "notional": 1.0, "method": "m",
               "late_s": window_s * 2}]
    assert liqview.LIQ_LATE not in liqview.render(rows, "absolute", 0.05, 12, 10,
                                                  colour=False, events=ordinary)
    assert liqview.LIQ_LATE in liqview.render(rows, "absolute", 0.05, 12, 10,
                                              colour=False, events=beyond)


def test_the_registry_is_opened_read_only(tmp_path):
    db = _registry(tmp_path, [(T0 + 60 * NS, 100.0, 1.0, T0 + 90 * NS)])
    liqview.liquidations("BTC", T0, T0 + 600 * NS, db=db)
    source = (Path(__file__).resolve().parent.parent / "deploy" / "liqview.py").read_text()
    assert "mode=ro" in source, "a live daemon owns the registry; this tool renders"


def test_a_missing_registry_is_not_an_error(tmp_path):
    # The tool must render on a box that has a tape but no registry yet.
    assert liqview.liquidations("BTC", T0, T0 + 600 * NS, db=tmp_path / "absent.sqlite") == []


# --- the x axis has a scale --------------------------------------------------

def _gapped(tmp_path, before: int = 5, gap_min: int = 240, after: int = 5) -> tuple[Path, int, int]:
    """Snapshots, a long hole, then snapshots again — the shape the tape
    actually has: liqmap2 lost 23.7 h on 2026-09-06 and the frame drew it as a
    seam one character wide between two dense blocks."""
    t = T0
    stamps = [T0 + i * 60 * NS for i in range(before)]
    resume = stamps[-1] + gap_min * 60 * NS
    stamps += [resume + i * 60 * NS for i in range(after)]
    for stamp in stamps:
        with WormWriter(tmp_path, STREAM) as writer:
            writer.write(_snapshot(100.0, [[0.02, 1_000_000.0, 0.0, 3]]),
                         t_event=None, t_ingest=stamp)
    return tmp_path, stamps[0], stamps[-1]


def test_a_hole_in_the_tape_is_blank_columns_not_a_closed_seam(tmp_path):
    """Index binning gave every column the same snapshot COUNT, so a four-hour
    hole between ten snapshots vanished: the frame showed ten adjacent columns
    and claimed to cover four hours. Time binning leaves the hole where it is."""
    root, first, last = _gapped(tmp_path)
    rows = liqview.snapshots("BTC", first, last, root=root)
    assert len(rows) == 10
    binned = liqview.columns(rows, 40, (first, last))
    assert len(binned) == 40                                # the width is filled
    blank = [i for i, b in enumerate(binned) if not b]
    assert len(blank) > 25, "the hole must occupy most of the frame"
    assert binned[0] and binned[-1], "both ends carry data"
    # And the failure this replaced, pinned: index binning packs the same ten
    # snapshots into ten adjacent columns and the hole is gone from the picture.
    legacy = liqview.columns(rows, 40)
    assert len(legacy) == 10 and all(legacy), "index binning closes the seam"


def test_the_header_states_seconds_per_column_and_how_many_are_blank(tmp_path):
    root, first, last = _gapped(tmp_path)
    rows = liqview.snapshots("BTC", first, last, root=root)
    frame = liqview.frame(rows, "BTC", "absolute", 0.05, 10, 40, colour=False,
                          window=(first, last))
    assert "resolution" in frame and "per column" in frame
    assert "/40 columns carry data" in frame
    assert "blank = no snapshot" in frame
    # 4.15 h over 40 columns is 6.2 min each; the number must be the real one.
    assert f"{liqview.col_seconds((first, last), 40) / 60:.1f} min" in frame


def test_the_mark_row_does_not_carry_across_a_hole(tmp_path):
    """A gap column has no mark. Repeating the last one would draw a flat price
    line through hours nobody observed."""
    root, first, last = _gapped(tmp_path)
    rows = liqview.snapshots("BTC", first, last, root=root)
    _, mark_row, _, _ = liqview.grid(rows, "absolute", 0.05, 10, 40, (first, last))
    assert mark_row.count(None) > 25


def test_the_strip_stays_aligned_when_columns_are_time_binned(tmp_path):
    root, first, last = _gapped(tmp_path)
    rows = liqview.snapshots("BTC", first, last, root=root)
    (band, line), = liqview.imb_strip(rows, ["0.01"], 40, (first, last))
    assert len(line) == 40
    body = liqview.render(rows, "absolute", 0.05, 10, 40, colour=False,
                          window=(first, last))
    for y, text in enumerate(body.splitlines()):
        assert len(text.split("|", 1)[1]) == 40


def test_the_budget_trims_by_ingest_time_not_by_part_count(tmp_path):
    """The first cut computed the floor as `until - kept_parts * 1 h`, which
    assumed one part per hour. The live store writes ~34, so the estimate put
    the floor days before the tape and trimmed nothing at all — the frame took
    three minutes with no warning."""
    root, first, last = _gapped(tmp_path, before=30, gap_min=0, after=0)
    parts = liqview.part_span(STREAM, first, last, root=root)
    assert len(parts) > 10
    total_mb = sum(n for _, n in parts) / 1e6
    budget = (total_mb / liqview.SCAN_MB_S) / 3                # room for a third
    floor, full, kept = liqview.budget_window(first, last, budget, root=root)
    assert floor > first, "the window must actually be trimmed"
    assert floor <= last
    assert kept <= budget and kept < full
    assert floor in {t for t, _ in parts}, "the floor is a real part's first ingest"


def test_a_window_inside_the_budget_is_left_alone(tmp_path):
    root, first, last = _gapped(tmp_path, before=5, gap_min=0, after=0)
    floor, full, kept = liqview.budget_window(first, last, 1e6, root=root)
    assert floor == first and kept == full
    floor, _, _ = liqview.budget_window(first, last, 0.0, root=root)
    assert floor == first, "--budget 0 means no limit"


# --- aalib tone (task 06) ---------------------------------------------------

needs_aa = pytest.mark.skipif(liqview._load_aa() is None,
                              reason="libaa is not installed on this box")


def _blind(monkeypatch):
    """Make the box look like one without libaa, cache and all."""
    monkeypatch.setattr(liqview, "_AA_LIB", [None])
    monkeypatch.setattr(liqview, "_AA_RAMP", [None])


@needs_aa
def test_the_aa_ramp_is_monotone_in_brightness():
    """The reason aalib is usable for a heatmap at all.

    aalib picks glyphs by SHAPE, which for a photograph is the point and here
    would be a disaster: a ramp whose glyphs do not order by ink is not a scale,
    it is decoration. With a flat cell and no dithering the mapping collapses to
    pure brightness, and this pins that -- if a future libaa or font broke it,
    every frame would still render and quietly stop meaning anything.
    """
    ramp = liqview.aa_ramp()
    assert ramp and len(ramp) > len(liqview.RAMP), "aalib must beat the block ramp"
    assert ramp[0] == " ", "the empty cell must be blank"
    assert ramp == "".join(dict.fromkeys(ramp)), f"a glyph recurs: {ramp!r}"

    # Walk the whole brightness range: the glyph must never go backwards.
    field = liqview._aa_field([[b / 255 for b in range(256)]], 256, 1)
    index = [ramp.index(c) for c in field[0]]
    assert index == sorted(index), "brightness must not map back down the ramp"
    assert index[0] == 0 and index[-1] == len(ramp) - 1


@needs_aa
def test_no_overlay_glyph_is_also_a_shade():
    """An overlay you cannot tell from a shade is worse than no overlay.

    The block ramp is three shade characters and a full block, so anything is
    free to sit on it. aalib's ramp is most of the printable ASCII set, and it
    swallowed two overlays outright: 'X', a large realized liquidation, drew
    exactly like a dense cell -- on a picture whose whole subject is dense cells
    -- and the ASCII mark 'o' like a middling one. Pinned for every combination,
    because the ramp is the library's font table and not ours.
    """
    for ascii_only in (False, True):
        for used_aa in (False, True):
            ramp = (liqview.aa_ramp() if used_aa
                    else (liqview.RAMP_ASCII if ascii_only else liqview.RAMP))
            overlays = liqview.overlay_glyphs(ascii_only, used_aa)
            assert len(set(overlays)) == len(overlays), f"two overlays collide: {overlays}"
            for glyph in overlays:
                assert glyph not in ramp.strip(), (
                    f"{glyph!r} is both an overlay and a shade "
                    f"(ascii={ascii_only}, aa={used_aa})")


@needs_aa
def test_the_substituted_glyphs_reach_the_frame_and_its_legend(tmp_path):
    """The substitution is worth nothing if the legend still names 'X'."""
    _store(tmp_path, [(100.0, [[0.0, 1_000_000.0, 0.0, 1]]) for _ in range(8)])
    rows = liqview.snapshots("BTC", T0, T0 + 8 * 60 * NS, root=tmp_path)
    events = [{"t": rows[4]["t"], "px": 100.0, "notional": 5_000.0, "method": "m",
               "late_s": 1.0}]
    text = liqview.frame(rows, "BTC", "absolute", 0.05, 16, 8, colour=False,
                         events=events, window=_window(rows), aa=True)
    body = "\n".join(l.split("|", 1)[1] for l in text.splitlines() if "|" in l)
    assert liqview.LIQ_LARGE_AA in body, "the substitute must actually be drawn"
    assert f"'{liqview.LIQ_SMALL}/{liqview.LIQ_LARGE_AA}'" in text
    assert f"'{liqview.LIQ_SMALL}/{liqview.LIQ_LARGE}'" not in text


@needs_aa
def test_a_glyph_reports_its_own_cell_and_no_other(tmp_path):
    """The bug this design avoids, pinned as a property.

    aalib's defaults -- 2x2 shape sampling over an interpolated buffer, plus
    Floyd-Steinberg dithering -- make a cell's glyph depend on its NEIGHBOURS.
    Measured on a 92-column BTC frame, that drew 21 of 624 distinct notionals as
    more than one character. On a picture someone reads cluster sizes off, that
    is the phantom-cluster bug again: mass that is not there, invented by the
    renderer. Equal cells must draw equal, whatever surrounds them.
    """
    # A checkerboard: every cell's neighbourhood is the opposite of its value,
    # so any bleed between cells shows up immediately.
    buckets = [[(i - 12) * 0.002, 1_000_000.0 * (10 ** (i % 2)), 0.0, 2] for i in range(24)]
    _store(tmp_path, [(100.0, buckets) for _ in range(12)])
    rows = liqview.snapshots("BTC", T0, T0 + 12 * 60 * NS, root=tmp_path)

    cells, _, _, _ = liqview.grid(rows, "absolute", 0.05, 20, 12)
    art = liqview.render(rows, "absolute", 0.05, 20, 12, colour=False, aa=True)
    body = [line.split("|", 1)[1] for line in art.splitlines()]

    glyphs = {}
    for y in range(20):
        for x in range(12):
            value = cells[y][x]
            glyph = body[20 - 1 - y][x]
            if glyph in (liqview.MARK_GLYPH, liqview.MARK_ASCII):
                continue                                # the price line, not tone
            glyphs.setdefault(round(value, 6), set()).add(glyph)
    smeared = {v: g for v, g in glyphs.items() if len(g) > 1}
    assert not smeared, f"equal notionals drew as different glyphs: {smeared}"


@needs_aa
def test_aa_tone_carries_more_levels_than_the_block_ramp(tmp_path):
    """The whole point of the flag. Same data, same log scale, more of it
    readable -- on the fixture that pins ramp saturation, so the comparison is
    against the block ramp at its best rather than on a shape that flatters."""
    bulk = [[i * 0.0015, 1_000_000.0 * (10 ** (2 * i / 45)), 0.0, 2] for i in range(45)]
    tail = [[-0.0015 * (i + 1), 3.0, 0.0, 1] for i in range(5)]
    _store(tmp_path, [(100.0, bulk + tail) for _ in range(10)])
    rows = liqview.snapshots("BTC", T0, T0 + 10 * 60 * NS, root=tmp_path)

    blocks = liqview.render(rows, "relative", 0.08, 24, 30, colour=False)
    tone = liqview.render(rows, "relative", 0.08, 24, 30, colour=False, aa=True)
    ramp = liqview.aa_ramp()
    assert len(set(c for c in tone if c in ramp.strip())) > \
        len(set(c for c in blocks if c in liqview.RAMP.strip()))


@needs_aa
@pytest.mark.parametrize("windowed", [True, False])
def test_aa_does_not_change_the_shape_of_the_frame(tmp_path, windowed):
    """Same grid, same axis, same width -- only the glyphs differ.

    Both binnings, because they disagree about width and the first cut of the
    aalib path sized its buffer off `width` instead of off the grid. Time bins
    fill the frame; index bins drop empty columns, so 15 snapshots asked to fill
    40 columns produce 15 -- and the buffer ran off the end of the row. The
    invariant is that the two paths agree, not that either equals `width`.
    """
    _store(tmp_path, _ramp(15))
    rows = liqview.snapshots("BTC", T0, T0 + 15 * 60 * NS, root=tmp_path)
    window = _window(rows) if windowed else None
    blocks = liqview.render(rows, "absolute", 0.05, 12, 40, colour=False,
                            window=window).splitlines()
    tone = liqview.render(rows, "absolute", 0.05, 12, 40, colour=False,
                          window=window, aa=True).splitlines()
    assert len(blocks) == len(tone) == 12
    for a, b in zip(blocks, tone):
        assert a.split("|", 1)[0] == b.split("|", 1)[0], "the price axis must not move"
        assert len(a.split("|", 1)[1]) == len(b.split("|", 1)[1])
    drawn = len(blocks[0].split("|", 1)[1])
    assert drawn == (40 if windowed else 15)


@needs_aa
def test_a_hole_in_the_tape_stays_blank_under_aa(tmp_path):
    """The failure that made this renderer flat-fill rather than interpolate: an
    upsampled buffer bleeds ink sideways, and ink in a gap column is the frame
    claiming data for hours nobody observed."""
    root, first, last = _gapped(tmp_path)
    rows = liqview.snapshots("BTC", first, last, root=root)
    _, mark_row, _, _ = liqview.grid(rows, "absolute", 0.05, 10, 40, (first, last))
    body = liqview.render(rows, "absolute", 0.05, 10, 40, colour=False,
                          window=(first, last), aa=True).splitlines()
    blank = [x for x, m in enumerate(mark_row) if m is None]
    assert len(blank) > 25
    for line in body:
        drawn = line.split("|", 1)[1]
        assert all(drawn[x] == " " for x in blank), "the hole must carry no ink"


def test_without_libaa_the_frame_draws_blocks_and_says_so(tmp_path, monkeypatch):
    """liqview is the tool you reach for when the box is already unhappy, so a
    missing system library is a fallback and never a traceback -- and the frame
    has to admit which ramp it drew, or the legend misstates the scale."""
    _blind(monkeypatch)
    _store(tmp_path, _ramp(10))
    rows = liqview.snapshots("BTC", T0, T0 + 10 * 60 * NS, root=tmp_path)
    assert liqview.aa_ramp() is None
    assert liqview._aa_field([[0.5] * 8], 8, 1) is None

    asked = liqview.render(rows, "absolute", 0.05, 12, 40, colour=False, aa=True)
    assert asked == liqview.render(rows, "absolute", 0.05, 12, 40, colour=False)
    text = liqview.frame(rows, "BTC", "absolute", 0.05, 12, 40, colour=False, aa=True)
    assert "libaa is not installed" in text
    assert liqview.RAMP.strip() in text


@needs_aa
def test_a_library_that_will_not_render_is_not_reported_as_a_missing_one(tmp_path,
                                                                        monkeypatch):
    """Falling back is fine. Blaming a library that is sitting right there is
    not: it sends whoever reads the frame off to install what they already
    have."""
    _store(tmp_path, _ramp(10))
    rows = liqview.snapshots("BTC", T0, T0 + 10 * 60 * NS, root=tmp_path)
    monkeypatch.setattr(liqview, "_aa_field", lambda *a, **k: None)   # installed, refuses
    text = liqview.frame(rows, "BTC", "absolute", 0.05, 12, 40, colour=False, aa=True)
    assert "would not render at this size" in text
    assert "not installed" not in text
    assert liqview.RAMP.strip() in text


def test_aa_off_is_byte_for_byte_what_it_always_was(tmp_path):
    """The default path is what everyone's eye is calibrated to; a new flag must
    not move it. Pinned against `RAMP` directly rather than a golden string, so
    this still means something when the ramp is next retuned."""
    _store(tmp_path, _ramp(15))
    rows = liqview.snapshots("BTC", T0, T0 + 15 * 60 * NS, root=tmp_path)
    art = liqview.render(rows, "absolute", 0.05, 12, 40, colour=False)
    assert set(art) <= set(liqview.RAMP + liqview.MARK_GLYPH + " |,.0123456789\n")
    text = liqview.frame(rows, "BTC", "absolute", 0.05, 12, 40, colour=False)
    assert "aalib" not in text and "libaa" not in text


# --- the realized-notional strip (task 06) ----------------------------------

def _events(window, rows_n: int, at: dict[int, float]) -> list[dict]:
    """`at` maps a column index in a `rows_n`-wide frame to a notional."""
    since, until = window
    span = until - since
    return [{"t": since + (x * span) // rows_n + span // (2 * rows_n), "px": 100.0,
             "notional": n, "method": "market", "late_s": 1.0}
            for x, n in at.items()]


def test_the_liq_strip_bins_on_the_same_columns_as_the_map(tmp_path):
    """Its only value is being read against the frame above it. Off by one
    column and it puts a cascade next to the move that caused it."""
    _store(tmp_path, [(100.0, [[0.0, 1_000_000.0, 0.0, 1]]) for _ in range(20)])
    rows = liqview.snapshots("BTC", T0, T0 + 20 * 60 * NS, root=tmp_path)
    window = _window(rows)
    events = _events(window, 20, {0: 1000.0, 7: 1000.0, 19: 1000.0})

    line, floor, peak = liqview.liq_strip(events, window, 20)
    assert len(line) == 20
    assert [x for x, c in enumerate(line) if c != " "] == [0, 7, 19]
    body = liqview.render(rows, "absolute", 0.05, 8, 20, colour=False, window=window)
    assert len(body.splitlines()[0].split("|", 1)[1]) == len(line)


def test_the_strip_ranks_notional_not_count(tmp_path):
    """Ten small prints are not a cascade and one big one is. Counting would say
    the opposite, and the strip exists precisely because the map cannot show
    size at all."""
    _store(tmp_path, [(100.0, [[0.0, 1_000_000.0, 0.0, 1]]) for _ in range(10)])
    rows = liqview.snapshots("BTC", T0, T0 + 10 * 60 * NS, root=tmp_path)
    window = _window(rows)
    events = _events(window, 10, {2: 100.0}) + [
        e for _ in range(10) for e in _events(window, 10, {8: 1_000_000.0})]

    line, floor, peak = liqview.liq_strip(events, window, 10)
    assert line[2] == liqview.LIQ_RAMP[0], f"the small print must sit at the floor: {line!r}"
    assert line[8] == liqview.LIQ_RAMP[-1], f"the cascade must sit at the top: {line!r}"
    assert floor == pytest.approx(100.0) and peak == pytest.approx(10_000_000.0)


def test_the_strip_is_absent_rather_than_empty_when_nothing_was_liquidated(tmp_path):
    _store(tmp_path, _ramp(6))
    rows = liqview.snapshots("BTC", T0, T0 + 6 * 60 * NS, root=tmp_path)
    window = _window(rows)
    assert liqview.liq_strip(None, window, 20) is None
    assert liqview.liq_strip([], window, 20) is None
    text = liqview.frame(rows, "BTC", "absolute", 0.05, 10, 20, colour=False,
                         events=[], window=window)
    assert "liq $" not in text


def test_the_frame_prints_the_strip_and_the_dollars_it_stands_for(tmp_path):
    """A ramp without its bounds is a picture of a number nobody can read."""
    _store(tmp_path, [(100.0, [[0.0, 1_000_000.0, 0.0, 1]]) for _ in range(12)])
    rows = liqview.snapshots("BTC", T0, T0 + 12 * 60 * NS, root=tmp_path)
    window = _window(rows)
    events = _events(window, 12, {1: 500.0, 6: 250_000.0})
    text = liqview.frame(rows, "BTC", "absolute", 0.05, 10, 12, colour=False,
                         events=events, window=window)
    assert "liq $" in text
    assert "$500" in text and "$250,000" in text
    strip = next(l for l in text.splitlines() if l.startswith(f"{'liq $':>10} |"))
    body = next(l for l in text.splitlines() if "|" in l and l[:10].strip().isdigit())
    assert len(strip.split("|", 1)[1]) == len(body.split("|", 1)[1])
