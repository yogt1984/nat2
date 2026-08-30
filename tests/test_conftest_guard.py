"""The blast shield is itself under test.

A guard nobody checks is a guard that quietly stops working. These tests assert
the mechanism rather than the symptom: that the store constants really are
redirected, that the write really does land in the scratch copy, and -- the part
a naive fix gets wrong -- that a helper whose default argument was rebound still
honours an argument the caller passes positionally.
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

from conftest import REPO

spec = importlib.util.spec_from_file_location(
    "gapwatch", Path(__file__).resolve().parent.parent / "deploy" / "gapwatch.py"
)
gapwatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gapwatch)

NOW = 1_755_000_000.0
STORE_CONSTANTS = ("MANIFEST", "LEDGER", "STATE_PATH", "EVLOG_STATE", "TAPE_DIR", "ACTIONS")


def test_every_store_path_gapwatch_knows_lands_under_tmp_path(tmp_path):
    for name in STORE_CONSTANTS:
        value = getattr(gapwatch, name)
        assert isinstance(value, Path), name
        assert str(value).startswith(str(tmp_path)), f"{name} still points at {value}"


def test_the_paths_that_stay_real_are_the_ones_that_point_at_code():
    """ROOT is only ever stat-ed for free disk, and redirecting a code path would
    break tests that read the source they are testing."""
    assert gapwatch.ROOT == REPO


def test_the_cli_that_writes_the_ledger_is_not_reachable():
    """gapwatch appends incidents by shelling out to .venv/bin/nat2. Redirecting the
    store is not enough; the binary has to be one that is not there, which is the
    case ledger_incident already handles."""
    assert not gapwatch.NAT2.exists()
    assert gapwatch.ledger_incident({"name": "guard_probe"}) is False


def test_the_public_alert_topic_can_never_be_paged_from_a_test(monkeypatch):
    import os
    assert "NAT2_NTFY_TOPIC" not in os.environ
    assert gapwatch.notify("guard probe") is False   # no topic, no request


def test_a_rebound_helper_still_honours_an_explicit_argument(tmp_path):
    """The trap a functools.partial would fall into: tests/test_gapwatch.py:34 calls
    newest_ingest(path) positionally, which a naive rebinding turns into
    'got multiple values for argument manifest'."""
    manifest = tmp_path / "explicit.jsonl"
    manifest.write_text(json.dumps({"stream": "hl.trades", "last_ingest": int(NOW * 1e9)}) + "\n")

    assert gapwatch.newest_ingest(manifest) == {"hl.trades": NOW}          # positional
    assert gapwatch.newest_ingest(manifest=manifest) == {"hl.trades": NOW}  # keyword
    assert gapwatch.newest_ingest() == {}                                   # rebound default

    assert gapwatch.tape_heartbeat_s(tmp_path / "absent") == 0.0
    assert gapwatch.last_observation_s() is None


def test_the_helpers_that_were_rebound_are_exactly_the_ones_with_store_defaults():
    """Five, not the four a hand-written list would have named: capture_hole also
    defaults to TAPE_DIR (gapwatch.py:119), so without the rebinding it globs the
    real 17k-file tape whenever a test calls it without a third argument. Deriving
    the set from the signatures rather than listing it is what caught that."""
    rebound = {name for name in dir(gapwatch)
               if hasattr(getattr(gapwatch, name), "__wrapped_defaults__")}
    assert rebound == {"newest_ingest", "tape_heartbeat_s", "last_observation_s",
                       "record_action", "capture_hole"}
    assert gapwatch.capture_hole({}, NOW) is None      # rebound default, no real tape reached


def test_a_watchdog_event_is_written_to_the_scratch_log_not_the_real_one(tmp_path, monkeypatch):
    """The exact shape of the historical leak: an open condition makes cmd_check call
    record_action, whose ACTIONS default was bound at import."""
    before = (REPO / "data" / "actions.jsonl").stat().st_size

    monkeypatch.setattr(gapwatch, "unit_active", lambda unit: True)
    monkeypatch.setattr(gapwatch, "newest_ingest", lambda: {s: NOW for s in gapwatch.CADENCE_S})
    monkeypatch.setattr(gapwatch, "last_observation_s", lambda: NOW)
    monkeypatch.setattr(gapwatch, "tape_heartbeat_s", lambda *a: NOW)
    monkeypatch.setattr(gapwatch, "notify", lambda *a, **k: True)
    monkeypatch.setattr(gapwatch.time, "time", lambda: NOW)
    # EVLOG_STATE is already redirected to a path that does not exist, so
    # `stream:events` opens -- which is precisely what test_gapwatch.py:72 does.
    gapwatch.cmd_check()

    written = gapwatch.ACTIONS.read_text()
    assert '"kind":"gapwatch:open"' in written
    assert "stream:events" in written
    assert (REPO / "data" / "actions.jsonl").stat().st_size == before


def test_the_session_backstop_reads_timestamps_not_sizes(tmp_path):
    """The live daemons append to these files while the suite runs, so the backstop
    has to tell a daemon's record from a test's. The clock is what separates them."""
    from conftest import BACKDATE_SLACK_NS, WITNESSES, appended_records

    assert set(WITNESSES) == {"data/actions.jsonl", "data/ledger.jsonl"}
    for name in WITNESSES:
        assert (REPO / name).exists(), f"{name} is the file the backstop reads"

    log = tmp_path / "actions.jsonl"
    daemon = {"t_ingest": time.time_ns(), "level": "L1", "kind": "cycle:scan"}
    planted = {"t_ingest": int(NOW * 1e9), "level": "L0", "kind": "gapwatch:open"}
    log.write_text("".join(json.dumps(r) + "\n" for r in (daemon, planted)))

    stamps = appended_records(log, 0, "t_ingest")
    floor = time.time_ns() - BACKDATE_SLACK_NS
    assert [s < floor for s, _ in stamps] == [False, True]   # daemon kept, test caught
