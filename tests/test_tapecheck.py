"""Golden tests for deploy/tapecheck.py (hetzner_plan task 10).

tapecheck owns `holes()`; gapwatch (task 19) and the clean-day scorer (task 20)
import it rather than growing their own. So the definition is pinned here, not
left to whichever caller ran last.

Loaded by path, like gapwatch: the deploy scripts are outside the nat2 package
on purpose -- tapecheck runs on `/usr/bin/python3` because a dead venv is one of
the things it has to be able to report on.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "tapecheck", Path(__file__).resolve().parent.parent / "deploy" / "tapecheck.py"
)
tapecheck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tapecheck)

NS = 1_000_000_000
T0 = 1_755_000_000 * NS


def _manifest_file(entries, tmp=None):
    import tempfile
    path = Path(tempfile.mkdtemp()) / "_manifest.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def _entry(stream, n, start_s, span_s, seq0, seq1, day="2026-08-08"):
    return {
        "stream": stream,
        "path": f"{stream}/{day}/{stream}-{n:04d}.ndjson.zst",
        "lines": 1, "bytes": 1, "sha256": "0" * 64,
        "first_seq": seq0, "last_seq": seq1,
        "first_ingest": T0 + int(start_s * NS),
        "last_ingest": T0 + int((start_s + span_s) * NS),
        "closed_at": T0 + int((start_s + span_s) * NS) + NS,
    }


# --- the definition this file owns -----------------------------------------

def test_a_hole_is_measured_from_one_part_ending_to_the_next_beginning():
    # Of the nine ways to pair the manifest's three timestamps, only this one is
    # meaningful. closed_at is stamped after the successor has already begun
    # ingesting, so pairing on it yields negative gaps.
    entries = [_entry("hl.trades", 0, 0, 100, 0, 9), _entry("hl.trades", 1, 400, 100, 10, 19)]
    found = tapecheck.holes(entries, T0, T0 + 1000 * NS, min_s=60)
    assert len(found) == 1
    assert found[0]["seconds"] == pytest.approx(300.0)   # 400 - 100, not 400 - 0


def test_a_gap_under_the_floor_is_a_rotation_seam_not_a_hole():
    entries = [_entry("hl.trades", 0, 0, 100, 0, 9), _entry("hl.trades", 1, 140, 100, 10, 19)]
    assert tapecheck.holes(entries, T0, T0 + 1000 * NS, min_s=60) == []
    assert len(tapecheck.holes(entries, T0, T0 + 1000 * NS, min_s=30)) == 1


def test_the_window_edges_are_never_holes():
    # A window opening long before the stream did would otherwise book the wait
    # as data loss. It is real downtime, but it is not something the tape lost,
    # and `no_data_before_s` carries it instead.
    entries = [_entry("hl.trades", 0, 10_000, 100, 0, 9)]
    assert tapecheck.holes(entries, T0, T0 + 20_000 * NS, min_s=60) == []


def test_entries_are_sorted_before_differencing_so_no_gap_is_negative():
    # Two real appends in the live manifest land out of order -- the snapshot
    # streams each carry one, 149.7 s backwards. Differencing in append order
    # would emit a negative "gap".
    late = _entry("nat2.liqmap", 1, 400, 0, 10, 10)
    early = _entry("nat2.liqmap", 0, 0, 0, 0, 0)
    ordered = tapecheck.for_stream([late, early], "nat2.liqmap")
    assert [e["first_ingest"] for e in ordered] == [early["first_ingest"], late["first_ingest"]]
    assert all(h["seconds"] >= 0 for h in tapecheck.holes(ordered, T0, T0 + 1000 * NS, 60))


def test_overlapping_parts_are_not_counted_as_an_absence():
    entries = [_entry("hl.trades", 0, 0, 500, 0, 9), _entry("hl.trades", 1, 100, 100, 10, 19)]
    assert tapecheck.holes(tapecheck.for_stream(entries, "hl.trades"),
                           T0, T0 + 1000 * NS, min_s=60) == []


# --- the manifest ----------------------------------------------------------

def test_a_crash_torn_manifest_line_is_skipped_but_corruption_still_raises(tmp_path):
    path = tmp_path / "_manifest.jsonl"
    good = json.dumps(_entry("hl.trades", 0, 0, 100, 0, 9))
    torn = "\x00" * 999 + '{"stream":"hl.trades","path":"x","lines":1,"first'
    path.write_text("\n".join([good, torn, good]) + "\n")
    assert len(tapecheck.read_manifest(path)) == 2

    path.write_text("\n".join([good, "{not json}", good]) + "\n")
    with pytest.raises(json.JSONDecodeError):
        tapecheck.read_manifest(path)


# --- the other checks ------------------------------------------------------

def test_seq_breaks_tell_a_forward_jump_from_a_rewrite():
    forward = [_entry("hl.trades", 0, 0, 10, 0, 9), _entry("hl.trades", 1, 100, 10, 20, 29)]
    backward = [_entry("hl.trades", 0, 0, 10, 0, 9), _entry("hl.trades", 1, 100, 10, 5, 14)]
    assert tapecheck.seq_breaks(forward) == [
        {"kind": "gap", "missing": 10,
         "after": forward[0]["path"], "before": forward[1]["path"]}]
    assert tapecheck.seq_breaks(backward)[0]["kind"] == "overlap"


def test_an_orphan_covering_a_hole_makes_it_recoverable_without_shrinking_it(tmp_path):
    hole = {"from_ns": T0, "to_ns": T0 + 600 * NS}
    inside = {"hl.trades/2026-08-08/orphan.ndjson.zst": T0 + 300 * NS}
    outside = {"hl.trades/2026-08-08/orphan.ndjson.zst": T0 + 9000 * NS}
    other = {"hl.l2book/2026-08-08/orphan.ndjson.zst": T0 + 300 * NS}
    assert tapecheck.recoverable(hole, "hl.trades", inside)
    assert not tapecheck.recoverable(hole, "hl.trades", outside)
    # An orphan of a different stream explains nothing about this one.
    assert not tapecheck.recoverable(hole, "hl.trades", other)


def test_a_snapshot_stream_reports_presence_and_never_a_hole_count():
    # Every snapshot opens and closes its own writer, so first_ingest ==
    # last_ingest and the "gap" between parts IS the cadence. Counting holes
    # here yields thousands of phantoms.
    entries = [_entry("nat2.liqmap", i, i * 60, 0, i, i) for i in range(10)]
    del entries[5]                                   # one snapshot missing
    report = tapecheck.spacing(entries, T0, T0 + 600 * NS)
    assert report["cadence_s"] == pytest.approx(60.0)
    assert report["present_frac"] < 1.0
    # The discriminating assertion is on `check()`, not on `spacing()`: a
    # snapshot stream must never grow a hole count anywhere in the report.
    full = tapecheck.check(T0, T0 + 600 * NS, min_s=60, classify_causes=False,
                           manifest=_manifest_file(entries), raw_root=Path("/nonexistent"))
    assert "holes" not in full["streams"]["nat2.liqmap"]
    assert "gap_minutes" not in full["streams"]["nat2.liqmap"]
    assert full["streams"]["hl.trades"]["kind"] == "continuous"


# --- policy ----------------------------------------------------------------

def test_the_floor_comes_from_the_ledger_and_the_tool_refuses_without_it(tmp_path, capsys):
    ledger = tmp_path / "ledger.jsonl"
    assert tapecheck.preregistered_floor(ledger) is None

    ledger.write_text("\n".join(json.dumps({
        "seq": i, "kind": kind, "payload": payload,
    }) for i, (kind, payload) in enumerate([
        ("preregistration", {"name": "something_else", "hole_floor_s": 999}),
        ("preregistration", {"name": "tapecheck_v1", "hole_floor_s": 60.0}),
    ])) + "\n")
    assert tapecheck.preregistered_floor(ledger) == 60.0

    # Refusing is the product: this window cannot tell 60 s from 90 s, so a
    # floor chosen in code would look measured without being falsifiable.
    assert tapecheck.main(["--since", "2026-08-20T00:00:00Z",
                           "--until", "2026-08-20T01:00:00Z"]) == 2
    assert "refusing" in capsys.readouterr().err


def test_the_newest_preregistration_wins(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(json.dumps({"seq": i, "kind": "preregistration", "payload": p})
                                for i, p in enumerate([
                                    {"name": "tapecheck_v1", "hole_floor_s": 60.0},
                                    {"name": "tapecheck_v1", "hole_floor_s": 90.0},
                                ])) + "\n")
    assert tapecheck.preregistered_floor(ledger) == 90.0


# --- the failures that read as health ---------------------------------------

def test_a_stream_absent_for_the_whole_window_is_not_reported_as_clean(tmp_path):
    """The worst thing this tool can find is invisible in a hole count.

    Holes are measured *between* parts, so a stream with no part at all has
    zero of them -- and before this was caught, `hl.l2book` rendered as the
    healthiest stream in a window where it had captured nothing for 84 hours,
    while the two streams that were actually alive showed ~1160 gap-minutes.
    """
    alive = [_entry("hl.trades", i, i * 3600, 3599, i * 10, i * 10 + 9) for i in range(4)]
    early = [_entry("hl.l2book", 0, -100_000, 3599, 0, 9)]      # exists, but long before
    report = tapecheck.check(T0, T0 + 4 * 3600 * NS, min_s=60, classify_causes=False,
                             manifest=_manifest_file(alive + early),
                             raw_root=Path("/nonexistent"))
    absent = report["streams"]["hl.l2book"]
    assert absent["absent"] and absent["parts"] == 0
    assert absent["no_data_before_s"] == pytest.approx(4 * 3600, abs=1)
    assert report["absent_streams"] == ["hl.l2book"]
    assert report["bug"] is True
    assert "ABSENT" in tapecheck.render(report)


def test_an_edge_absence_is_never_negative(tmp_path):
    # A part straddling the window bound means there was no absence, not a
    # negative one. Any --until off the rotation boundary used to print one.
    entries = [_entry("hl.trades", 0, -1000, 5000, 0, 9)]
    report = tapecheck.check(T0, T0 + 1000 * NS, min_s=60, classify_causes=False,
                             manifest=_manifest_file(entries), raw_root=Path("/nonexistent"))
    data = report["streams"]["hl.trades"]
    assert data["no_data_before_s"] == 0.0 and data["no_data_after_s"] == 0.0


def test_presence_is_measured_against_the_window_not_the_surviving_samples():
    # A stream that dies a third of the way in must not score near-perfect just
    # because the samples that remain are punctual.
    entries = [_entry("nat2.liqmap", i, i * 60, 0, i, i) for i in range(11)]
    died_early = tapecheck.spacing(entries, T0, T0 + 1800 * NS)
    assert died_early["present_frac"] < 0.45


def test_venue_does_not_swallow_every_hole():
    # `unknown` is the only signal that means "bug", so anything that matches
    # everything destroys it. A bare 5xx regex matched PIDs and byte counts:
    # an ordinary journal hour on this box contains 546, 565, 555, 545, 515.
    assert not tapecheck.HTTP_5XX.search("nat2-capture.service: main process 546 exited")
    assert not tapecheck.HTTP_5XX.search("wrote 515 bytes")
    assert tapecheck.HTTP_5XX.search("poll: HTTPStatusError 503 x4")
    assert "RuntimeError" not in tapecheck.VENUE_MARKS


def test_a_short_pause_does_not_explain_a_long_hole():
    # Two seconds of clock skew is not an explanation for a five-hour absence,
    # and the threshold is relative to the hole so that changing the ledgered
    # floor cannot silently rewrite past diagnoses.
    def rec(real_us, mono_us):
        return {"__REALTIME_TIMESTAMP": str(real_us),
                "__MONOTONIC_TIMESTAMP": str(mono_us), "_BOOT_ID": "b"}
    tiny = [rec(0, 0), rec(2_000_000, 0)]                 # 2 s of skew
    assert not tapecheck._paused(tiny, covers_s=3600.0)
    assert tapecheck._paused(tiny, covers_s=3.0)
