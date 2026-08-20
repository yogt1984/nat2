"""Golden tests for deploy/gapwatch.py (TASK_2/TASKS/01).

gapwatch is deliberately outside the nat2 package (no cross-imports rule);
load it by path.
"""

import importlib.util
import json
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
