"""WORM store and ledger: the two things everything else trusts."""

from __future__ import annotations

import json

import pytest

from nat2.core.clock import NS, ms_to_ns, now_ns, parse_window
from nat2.io.worm import WormWriter, read_manifest, read_records
from nat2.ledger.chain import Ledger


def test_parse_window():
    assert parse_window("30m") == 30 * 60 * NS
    assert parse_window("24h") == 24 * 3600 * NS
    assert parse_window("7d") == 7 * 86400 * NS
    with pytest.raises(ValueError):
        parse_window("7 weeks")


def test_ms_to_ns():
    assert ms_to_ns(1) == 1_000_000
    assert ms_to_ns(None) is None


def test_roundtrip_and_manifest(tmp_path):
    with WormWriter(tmp_path, "hl.trades") as writer:
        for i in range(5):
            writer.write({"i": i}, t_event=now_ns() - NS)

    records = list(read_records(tmp_path, "hl.trades"))
    assert [r["seq"] for r in records] == [0, 1, 2, 3, 4]
    assert [r["payload"]["i"] for r in records] == list(range(5))
    assert all(r["t_ingest"] > r["t_event"] for r in records)

    entries = read_manifest(tmp_path, "hl.trades")
    assert len(entries) == 1
    assert entries[0].lines == 5
    assert (entries[0].first_seq, entries[0].last_seq) == (0, 4)


def test_seq_continues_across_restarts(tmp_path):
    with WormWriter(tmp_path, "hl.trades") as writer:
        writer.write({"i": 0}, None)
    with WormWriter(tmp_path, "hl.trades") as writer:
        # A restart must not restart the counter: a hole in seq has to mean
        # lost records, never merely a bounced daemon.
        assert writer.seq == 1
        writer.write({"i": 1}, None)
    assert [r["seq"] for r in read_records(tmp_path, "hl.trades")] == [0, 1]


def test_manifest_detects_modified_file(tmp_path):
    from nat2.validate.audit_feed import audit

    with WormWriter(tmp_path, "hl.trades") as writer:
        for i in range(3):
            writer.write({"i": i}, now_ns())

    entry = read_manifest(tmp_path)[0]
    path = tmp_path / entry.path
    path.write_bytes(path.read_bytes() + b"tampered")

    result = audit(tmp_path, ["hl.trades"], parse_window("1h"))
    assert not result.passed
    assert any(c.name == "manifest_intact" for c in result.failures)


def test_ledger_chain_detects_edit(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append("gate", {"gate": "feed", "passed": False, "detail": {}})
    ledger.append("gate", {"gate": "feed", "passed": True, "detail": {}})
    assert ledger.verify()[0]

    lines = (tmp_path / "ledger.jsonl").read_text().splitlines()
    first = json.loads(lines[0])
    first["payload"]["passed"] = True  # bury the failed test
    lines[0] = json.dumps(first, separators=(",", ":"))
    (tmp_path / "ledger.jsonl").write_text("\n".join(lines) + "\n")

    ok, message = ledger.verify()
    assert not ok and "entry 0" in message


def test_ledger_latest_matches_on_payload(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append("gate", {"gate": "feed", "passed": True, "detail": {}})
    ledger.append("gate", {"gate": "map", "passed": False, "detail": {}})
    assert ledger.latest("gate", gate="feed").payload["passed"] is True
    assert ledger.latest("gate", gate="magnet") is None
