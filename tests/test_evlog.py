"""evlog: parsers against recorded responses, dedup, and the daily close."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy" / "evlog"))
import evlog  # noqa: E402
import sources  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "evlog"
FILES = {sources.FOMC_URL: "fomc.html", sources.BLS_SCHEDULE_URL: "bls_cpi.html",
         sources.BLS_API_URL: "bls_api.json", sources.TRUTH_URL: "truth.json"}


def fetch(url: str) -> str:
    return (FIX / FILES[url]).read_text()


def test_fomc_parses_meetings_and_released_statements():
    recs = {r["event_id"]: r for r in sources.fomc(fetch)}
    jan = recs["fomc:2026-01-28"]
    assert jan["class"] == "scheduled" and jan["payload"]["meeting_days"] == "27-28"
    assert jan["source_ts"] == sources.et_ns(2026, 1, 28, 14, 0)  # statement at 14:00 ET
    assert recs["fomc:2026-01-28:released"]["payload"]["statement_url"].endswith("monetary20260128a.htm")
    assert "fomc:2027-10-27" in recs and "fomc:2027-10-27:released" not in recs  # future: scheduled only


def test_bls_cpi_schedule_and_latest_release():
    recs = {r["event_id"]: r for r in sources.bls_cpi(fetch)}
    sep = recs["cpi:2026-09-11"]
    assert sep["payload"]["reference_month"] == "August 2026"
    assert sep["source_ts"] == sources.et_ns(2026, 9, 11, 8, 30)
    rel = recs["cpi:2026-M07:released"]
    assert rel["payload"]["value"] == "333.918"
    assert rel["source_ts"] == recs["cpi:2026-08-12"]["source_ts"]  # July CPI was scheduled for Aug 12


def test_truth_social_statuses():
    recs = sources.truth_social(fetch)
    assert len(recs) == 20 and all(r["class"] == "unscheduled" for r in recs)
    assert recs[0]["event_id"] == "truth:117125950821111248"
    assert recs[0]["source_ts"] == 1787200177326000000  # exact ns, no float drift
    assert "<" not in recs[0]["payload"]["text"]


def test_poll_all_dedups_and_isolates_source_failures():
    def boom(_):
        raise RuntimeError("403")
    srcs = [("ok", lambda f: [{"class": "unscheduled", "event_id": "x:1", "source_ts": 1, "payload": {}}]),
            ("bad", boom)]
    state = {"seen": [], "last_poll_ns": 0, "errors": {}}
    first = evlog.poll_all(state, fetch_fn=None, sources=srcs, now_ns=lambda: 42)
    second = evlog.poll_all(state, fetch_fn=None, sources=srcs, now_ns=lambda: 43)
    assert [r["event_id"] for r in first] == ["x:1"] and first[0]["receipt_ts"] == 42
    assert second == []
    assert state["errors"]["bad"].startswith("RuntimeError")


def test_close_finished_days_writes_manifest_once(tmp_path, monkeypatch):
    monkeypatch.setattr(evlog, "ROOT", tmp_path)
    monkeypatch.setattr(evlog, "EVENTS", tmp_path / "data" / "events")
    monkeypatch.setattr(evlog, "MANIFEST", tmp_path / "data" / "events" / "_manifest.jsonl")
    evlog.append_records([
        {"schema": 1, "class": "unscheduled", "source": "t", "event_id": "a", "source_ts": 0,
         "receipt_ts": 1_700_000_000_000_000_000, "payload": {}},
        {"schema": 1, "class": "unscheduled", "source": "t", "event_id": "b", "source_ts": 0,
         "receipt_ts": 1_800_000_000_000_000_000, "payload": {}},
    ])
    closed = evlog.close_finished_days(today="2027-01-15")  # the second record's day is still open
    assert [c["path"] for c in closed] == ["data/events/2023-11-14.ndjson"]
    assert closed[0]["lines"] == 1 and len(closed[0]["sha256"]) == 64
    assert evlog.close_finished_days(today="2027-01-15") == []  # idempotent
    assert len(evlog.MANIFEST.read_text().splitlines()) == 1


def test_golden_record_shape():
    rec = evlog.poll_all({"seen": [], "last_poll_ns": 0, "errors": {}}, fetch_fn=fetch,
                         sources=[("truth_social", sources.truth_social)], now_ns=lambda: 7)[0]
    assert set(rec) == {"schema", "class", "source", "event_id", "source_ts", "receipt_ts", "payload"}
    assert rec["schema"] == 1 and json.dumps(rec)  # serialisable
