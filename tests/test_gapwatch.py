"""Golden tests for deploy/gapwatch.py (TASK_2/TASKS/01).

gapwatch is deliberately outside the nat2 package (no cross-imports rule);
load it by path.
"""

import importlib.util
import json

import pytest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "gapwatch", Path(__file__).resolve().parent.parent / "deploy" / "gapwatch.py"
)
gapwatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gapwatch)

NOW = 1_755_000_000.0


def _manifest(tmp_path):
    path = tmp_path / "_manifest.jsonl"
    fresh_ns = int((NOW - 600) * 1e9)  # 10 min old — fresh
    stale_ns = int((NOW - 8000) * 1e9)  # 133 min old — beyond 2x 3600 s
    lines = [
        {"stream": "hl.trades", "last_ingest": fresh_ns},
        {"stream": "hl.trades", "last_ingest": stale_ns},  # older entry after newer: max wins
        {"stream": "hl.l2book", "last_ingest": stale_ns},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def test_newest_ingest_takes_max_per_stream(tmp_path):
    ages = gapwatch.newest_ingest(_manifest(tmp_path))
    assert ages == {"hl.trades": NOW - 600, "hl.l2book": NOW - 8000}


def test_tick_golden_open_accrue_recover():
    conds_bad = {"stream:hl.l2book": (True, "age 133m"), "stream:hl.trades": (False, "age 10m")}
    state: dict = {}

    events = gapwatch.tick(state, conds_bad, NOW)
    assert events == [("open", "stream:hl.l2book GAP OPEN (age 133m)")]
    assert state["open"] == {"stream:hl.l2book": NOW}
    assert state["gap_minutes"] == {}  # opened this tick; accrual starts next tick

    events = gapwatch.tick(state, conds_bad, NOW + 300)
    assert events == []  # edge-triggered: no re-alert while open
    assert state["gap_minutes"] == {"stream:hl.l2book": 5.0}

    conds_ok = {k: (False, "age 1m") for k in conds_bad}
    events = gapwatch.tick(state, conds_ok, NOW + 600)
    assert events == [("recovery", "stream:hl.l2book recovered after 10m")]
    assert state["open"] == {}
    assert state["gap_minutes"] == {"stream:hl.l2book": 10.0}  # 5 + 5 more before recovery


def test_week_roll_resets_counter():
    state = {"week": "2026-W01", "gap_minutes": {"stream:hl.trades": 59.0}, "open": {}}
    gapwatch.tick(state, {}, NOW)
    assert state["gap_minutes"] == {} and state["week"] != "2026-W01"


def test_check_records_unit_states_and_report_units_never_alert(tmp_path, monkeypatch):
    """TASK_2/06: the status page reads unit states from this file, never systemd."""
    monkeypatch.setattr(gapwatch, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(gapwatch, "unit_active", lambda unit: unit != "nat-ingestor.service")
    monkeypatch.setattr(gapwatch, "newest_ingest", lambda: {s: NOW for s in gapwatch.CADENCE_S})
    monkeypatch.setattr(gapwatch, "last_observation_s", lambda: NOW)
    monkeypatch.setattr(gapwatch, "notify", lambda *a, **k: True)
    monkeypatch.setattr(gapwatch.time, "time", lambda: NOW)
    monkeypatch.setattr(gapwatch, "EVLOG_STATE", tmp_path / "missing.json")
    gapwatch.cmd_check()
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["units"]["nat-ingestor.service"] == "inactive"
    assert state["units"]["nat2-statuspage.timer"] == "active"
    assert set(state["units"]) == set(gapwatch.UNITS) | set(gapwatch.REPORT_UNITS)
    assert not any(k.startswith("report:") for k in state["open"])  # report units never open a gap


def test_tape_heartbeat_sees_intra_file_silence(tmp_path, monkeypatch):
    """TASK_2/07 follow-up: a hole inside a normally rotated file must still open a gap."""
    import os
    day = tmp_path / "2026-08-20"
    day.mkdir()
    f = day / "hl.trades-20260820T17-00.ndjson.zst"
    f.write_bytes(b"x")
    os.utime(f, (NOW - 1965, NOW - 1965))
    assert gapwatch.tape_heartbeat_s(tmp_path) == NOW - 1965
    assert gapwatch.tape_heartbeat_s(tmp_path / "missing") == 0.0
    monkeypatch.setattr(gapwatch, "TAPE_DIR", tmp_path)
    monkeypatch.setattr(gapwatch, "newest_ingest", lambda: {s: NOW for s in gapwatch.CADENCE_S})
    monkeypatch.setattr(gapwatch, "unit_active", lambda unit: True)
    monkeypatch.setattr(gapwatch, "last_observation_s", lambda: NOW)
    monkeypatch.setattr(gapwatch, "EVLOG_STATE", tmp_path / "missing.json")
    conds = gapwatch.conditions(NOW)
    assert conds["stream:hl.trades:heartbeat"][0] and not conds["stream:hl.trades"][0]
    os.utime(f, (NOW - 30, NOW - 30))
    assert not gapwatch.conditions(NOW)["stream:hl.trades:heartbeat"][0]


def _manifest_with_hole(tmp_path, now_s, gap_s=3400.0, stream="hl.trades"):
    """A two-part manifest for `stream` with one gap, inside now_s's ISO week."""
    import json as _json

    week = gapwatch.week_start_s(now_s)
    a_end = week + 3600
    b_start = a_end + gap_s
    rows = [
        {"stream": stream, "path": f"{stream}/d/a.zst", "lines": 1, "bytes": 1,
         "sha256": "0" * 64, "first_seq": 0, "last_seq": 9,
         "first_ingest": int(week * 1e9), "last_ingest": int(a_end * 1e9),
         "closed_at": int(a_end * 1e9)},
        {"stream": stream, "path": f"{stream}/d/b.zst", "lines": 1, "bytes": 1,
         "sha256": "0" * 64, "first_seq": 10, "last_seq": 19,
         "first_ingest": int(b_start * 1e9), "last_ingest": int((b_start + 60) * 1e9),
         "closed_at": int((b_start + 60) * 1e9)},
    ]
    manifest = tmp_path / "_manifest.jsonl"
    manifest.write_text("".join(_json.dumps(r) + "\n" for r in rows))
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(_json.dumps({"seq": 0, "kind": "preregistration",
                                   "payload": {"name": "tapecheck_v1", "hole_floor_s": 60.0}}) + "\n")
    return manifest, ledger


def test_every_hole_is_booked_not_only_the_ones_the_watchdog_slept_through(tmp_path):
    """The old reconstruction ran only when this watchdog's own tick was >= 900 s
    late, so every outage it survived was invisible -- the state booked 62.3 min
    for ISO-W35 against 758.4 min sitting in the manifest. Against the live
    store this booker reports 1,817 min for that week."""
    manifest, ledger = _manifest_with_hole(tmp_path, NOW)

    on_time = {"last_tick": NOW - 300}                    # the watchdog was never late
    events = gapwatch.book_holes(on_time, NOW, manifest=manifest, ledger=ledger)
    assert events and "TAPE HOLE" in events[0][1]
    assert on_time["gap_minutes"]["stream:hl.trades:hole"] == pytest.approx(3400 / 60)


def test_a_hole_is_booked_once_however_many_ticks_see_it(tmp_path):
    # Keyed on the hole's absolute start. An offset from "now" is a different
    # key every tick, and books one outage repeatedly.
    manifest, ledger = _manifest_with_hole(tmp_path, NOW)
    state = {}
    first = gapwatch.book_holes(state, NOW, manifest=manifest, ledger=ledger)
    booked = dict(state["gap_minutes"])
    second = gapwatch.book_holes(state, NOW + 300, manifest=manifest, ledger=ledger)
    assert first and not second
    assert state["gap_minutes"] == booked


def test_snapshot_streams_are_never_booked_as_holes(tmp_path):
    """nat2.liqmap* open a writer per snapshot, so the gap between parts IS the
    cadence -- 64.5 s, over the 60 s floor. Booking them yielded 7,512 phantom
    holes and 10,077 phantom minutes for one week, burying the 1,817 real ones."""
    manifest, ledger = _manifest_with_hole(tmp_path, NOW, gap_s=3400.0, stream="nat2.liqmap2")
    state = {}
    assert gapwatch.book_holes(state, NOW, manifest=manifest, ledger=ledger) == []
    assert "gap_minutes" not in state or not state["gap_minutes"]
    assert "nat2.liqmap2" in gapwatch.CADENCE_S       # watched for staleness, though


def test_without_the_pre_registered_floor_nothing_is_booked_but_the_watch_goes_on(tmp_path):
    manifest, _ = _manifest_with_hole(tmp_path, NOW)
    bare = tmp_path / "empty-ledger.jsonl"
    bare.write_text("")
    state = {}
    assert gapwatch.book_holes(state, NOW, manifest=manifest, ledger=bare) == []


def test_the_restart_time_is_wall_clock_not_monotonic(monkeypatch):
    """CLOCK_MONOTONIC does not advance across suspend and /proc/uptime's
    CLOCK_BOOTTIME does, so the old arithmetic drifted by however long the box
    had slept: 908 minutes for nat2-capture on this host, which put every
    reconstructed restart fifteen hours before the truth."""
    class Done:
        stdout = "@1788085325\n"

    calls = []
    monkeypatch.setattr(gapwatch.subprocess, "run",
                        lambda argv, **k: calls.append(argv) or Done())
    assert gapwatch.unit_active_since_s("nat2-cycle.service") == 1788085325.0
    assert "--timestamp=unix" in calls[0]
    assert not any("Monotonic" in a for a in calls[0])


def test_hole_is_alerted_once_counted_and_ledgered_with_retry(tmp_path, monkeypatch):
    sent, ledgered, ok = [], [], [False, True]           # first ledger write fails, second succeeds
    monkeypatch.setattr(gapwatch, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(gapwatch, "unit_active", lambda unit: True)
    monkeypatch.setattr(gapwatch, "newest_ingest", lambda: {s: NOW for s in gapwatch.CADENCE_S})
    monkeypatch.setattr(gapwatch, "last_observation_s", lambda: NOW)
    monkeypatch.setattr(gapwatch, "tape_heartbeat_s", lambda *a: NOW)
    (tmp_path / "evlog.json").write_text(json.dumps({"last_poll_ns": int(NOW * 1e9)}))
    monkeypatch.setattr(gapwatch, "EVLOG_STATE", tmp_path / "evlog.json")
    monkeypatch.setattr(gapwatch, "notify", lambda m, **k: sent.append(m) or True)
    monkeypatch.setattr(gapwatch, "ledger_incident", lambda p: ledgered.append(p) or ok.pop(0))
    def _one_hole(state, now, **kw):
        if now != NOW or "booked" in state:
            return []
        state["booked"] = True
        state.setdefault("gap_minutes", {})["stream:hl.trades:hole"] = (3400 - 60) / 60
        state["holes"] = [{"stream": "hl.trades", "from_s": NOW - 3400, "to_s": NOW - 60,
                           "minutes": (3400 - 60) / 60}]
        state.setdefault("pending_incidents", []).append({"name": "tape_hole", "minutes": 55.7})
        return [("open", "TAPE HOLE hl.trades 56m (08-21 13:00:00Z -> 08-21 13:56:00Z); "
                         "`deploy/tapecheck.py` has the cause")]

    monkeypatch.setattr(gapwatch, "book_holes", _one_hole)
    (tmp_path / "state.json").write_text(json.dumps({"last_tick": NOW - 3600}))
    monkeypatch.setattr(gapwatch.time, "time", lambda: NOW)
    gapwatch.cmd_check()
    state = json.loads((tmp_path / "state.json").read_text())
    assert sent and sent[0].startswith("TAPE HOLE hl.trades 56m") and "tapecheck" in sent[0]
    assert state["gap_minutes"]["stream:hl.trades:hole"] == (3400 - 60) / 60 and state["holes"][0]["minutes"] == (3400 - 60) / 60
    assert len(state["pending_incidents"]) == 1 and ledgered[0]["name"] == "tape_hole"       # kept for retry
    monkeypatch.setattr(gapwatch.time, "time", lambda: NOW + 300)
    gapwatch.cmd_check()
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["pending_incidents"] == [] and len(ledgered) == 2 and len(sent) == 1       # retried once, no second alert
