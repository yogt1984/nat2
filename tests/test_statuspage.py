"""Golden test for deploy/statuspage.py (TASK_2/TASKS/06).

Fixture ledger + gapwatch state + manifest + mtime-stamped dirs -> HTML compared
byte-for-byte against tests/golden/status.html. ``now`` is injected; mtimes are
set explicitly so the page is deterministic. Invariants the task pins: every
metric carries an as-of stamp, no <script>, generator <= 400 lines.
Regenerate the golden with: UPDATE_GOLDEN=1 pytest tests/test_statuspage.py
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "deploy" / "statuspage.py"
GOLDEN = HERE / "golden" / "status.html"
spec = importlib.util.spec_from_file_location("statuspage", SRC)
sp = importlib.util.module_from_spec(spec)
sys.modules["statuspage"] = sp  # dataclass needs it importable to resolve annotations
spec.loader.exec_module(sp)

NOW = 1_755_000_000.0  # 2025-08-12 12:26:40Z
NS = int(1e9)


def _fixture(tmp_path: Path) -> "sp.Paths":
    ledger = tmp_path / "ledger.jsonl"
    rows = [
        {"seq": 1, "ts": int((NOW - 86400) * NS), "kind": "preregistration",
         "payload": {"name": "map_threshold", "pass_if": ">= 0.60"}},
        {"seq": 2, "ts": int((NOW - 80000) * NS), "kind": "gate",
         "payload": {"gate": "feed", "passed": True, "detail": {"failed": []}}},
        {"seq": 3, "ts": int((NOW - 70000) * NS), "kind": "gate",
         "payload": {"gate": "map", "passed": False, "detail": {"failed": ["-:window"], "judged_against": [1]}}},
    ] + [
        {"seq": 10 + i, "ts": int((NOW - 60000 + i * 3600) * NS), "kind": "observation",
         "payload": {"name": "liq_population", "notional_frac": 0.8 + i / 100, "mapped_notional_frac": 0.3,
                     "wallet_frac": 0.05, "mapped_wallet_frac": 0.01}}
        for i in range(5)
    ] + [{"seq": 99, "ts": int(NOW * NS), "kind": "observation", "payload": {"name": "other", "x": 1}}]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    state = tmp_path / "gapwatch_state.json"
    state.write_text(json.dumps({
        "week": "2025-W33", "open": {"stream:hl.l2book": NOW - 900},
        "gap_minutes": {"stream:hl.l2book": 75.0, "stream:hl.trades": 2.5},
        "units": {"nat2-capture.service": "active", "nat2-cycle.service": "inactive",
                  "nat2-statuspage.timer": "active", "nat-ingestor.service": "active"},
    }))
    os.utime(state, (NOW - 120, NOW - 120))

    manifest = tmp_path / "_manifest.jsonl"
    manifest.write_text("\n".join(json.dumps(x) for x in [
        {"stream": "hl.trades", "last_ingest": int((NOW - 600) * NS)},
        {"stream": "hl.l2book", "last_ingest": int((NOW - 9000) * NS)},
        {"stream": "hl.trades", "last_ingest": int((NOW - 7200) * NS)},  # older after newer: max wins
    ]) + "\n")

    events = tmp_path / "events"
    events.mkdir()
    (events / "2025-08-12.ndjson").write_text("{}\n")
    os.utime(events / "2025-08-12.ndjson", (NOW - 300, NOW - 300))

    nat_data = tmp_path / "nat_data" / "features"
    nat_data.mkdir(parents=True)
    (nat_data / "a.parquet").write_text("")
    os.utime(nat_data / "a.parquet", (NOW - 5000, NOW - 5000))
    (nat_data / "b.parquet.tmp").write_text("")
    os.utime(nat_data / "b.parquet.tmp", (NOW - 200, NOW - 200))  # live tmp is the freshness signal

    results = tmp_path / "experiment_results"
    results.mkdir()
    (results / "result__2025-07-30__1000_old.md").write_text("")
    return sp.Paths(ledger=ledger, gapwatch_state=state, manifest=manifest, events_dir=events,
                    nat_data=nat_data.parent, nat_results=results)


def test_golden(tmp_path):
    out = sp.render(sp.collect(NOW, _fixture(tmp_path)))
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(out)
    assert out == GOLDEN.read_text()


def test_invariants(tmp_path):
    out = sp.render(sp.collect(NOW, _fixture(tmp_path)))
    assert out.lower().count("<script") == 0
    assert out.count('class="m ') == out.count('class="asof"') > 10
    assert sum(1 for _ in SRC.open()) <= 400
    # facts the fixture plants
    assert "2025-W33" in out and "PASS" in out and "FAIL" in out
    assert "75.0 / 60" in out and "results this month" in out
    assert '<span class="m bad">0</span>' in out  # B4 cadence: month at 0 is red
    assert "3m" in out  # live .parquet.tmp beats the older closed parquet


def test_missing_inputs_render(tmp_path):
    """Empty world: the page must still render, with refusals visible, never crash."""
    empty = sp.Paths(**{f: tmp_path / f for f in sp.Paths.__dataclass_fields__})
    out = sp.render(sp.collect(NOW, empty))
    assert "no observations" in out and "fewer than 2 points" in out
    assert out.count('class="m ') == out.count('class="asof"')


def test_write_atomic(tmp_path):
    target = tmp_path / "www" / "status.html"
    sp.write_atomic(target, "<p>x</p>")
    assert target.read_text() == "<p>x</p>" and not target.with_suffix(".tmp").exists()


def test_malformed_ledger_lines_are_skipped(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join([
        "not json", "[1,2]", json.dumps({"kind": "gate", "payload": None}),  # no ts
        json.dumps({"seq": 1, "ts": int(NOW * NS), "kind": "gate", "payload": {"gate": "x", "passed": True, "detail": None}}),
        json.dumps({"seq": 2, "ts": int(NOW * NS), "kind": "observation", "payload": "string"}),
    ]) + "\n")
    paths = sp.Paths(**{f: tmp_path / f for f in sp.Paths.__dataclass_fields__ if f != "ledger"}, ledger=ledger)
    out = sp.render(sp.collect(NOW, paths))
    assert "<td>x</td>" in out and "PASS" in out
    assert out.count('class="m ') == out.count('class="asof"')
