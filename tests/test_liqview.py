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
    median = statistics.median(idx)
    assert median <= 6.0, (
        f"the ramp saturated: median glyph index {median} of {len(liqview.RAMP) - 1}")
    assert len(set(idx)) >= 8, f"the ramp must use its range, got {len(set(idx))} levels"


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
