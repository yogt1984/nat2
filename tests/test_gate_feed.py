"""The feed audit must fail loudly on each way the store can be wrong."""

from __future__ import annotations

import pytest

from nat2.core.clock import NS, now_ns, parse_window
from nat2.core.guard import GateRefusal, require
from nat2.gates import feed as gate_feed
from nat2.hl.schemas import STREAMS, asset_contexts
from nat2.io.worm import WormWriter
from nat2.ledger.chain import Ledger

WINDOW = parse_window("1h")


def _write(tmp_path, count=10, event_offset_ns=-NS, seq_jump_at=None):
    with WormWriter(tmp_path, "hl.trades") as writer:
        for i in range(count):
            if seq_jump_at is not None and i == seq_jump_at:
                writer.seq += 50  # simulate records lost between two writes
            t_ingest = now_ns()
            writer.write([{"time": (t_ingest + event_offset_ns) // 1_000_000}],
                         t_event=t_ingest + event_offset_ns, t_ingest=t_ingest)


def _checks(result):
    return {c.name: c for c in result.checks}


def test_clean_store_passes(tmp_path):
    _write(tmp_path)
    from nat2.validate.audit_feed import audit

    result = audit(tmp_path, ["hl.trades"], WINDOW)
    assert result.passed, [c.detail for c in result.failures]


def test_sequence_hole_fails(tmp_path):
    _write(tmp_path, seq_jump_at=5)
    from nat2.validate.audit_feed import audit

    result = audit(tmp_path, ["hl.trades"], WINDOW)
    assert not result.passed
    hole = _checks(result)["seq_continuous"]
    assert not hole.passed and hole.stats["lost"] == 50


def test_ingest_preceding_event_fails(tmp_path):
    # Our clock behind HL's: t_ingest stops being an upper bound on what we
    # could have known, so every feature built on the store inherits lookahead.
    _write(tmp_path, event_offset_ns=+5 * NS)
    from nat2.validate.audit_feed import audit

    result = audit(tmp_path, ["hl.trades"], WINDOW)
    assert not result.passed
    assert not _checks(result)["clock_causal"].passed


def test_empty_stream_fails(tmp_path):
    from nat2.validate.audit_feed import audit

    result = audit(tmp_path, ["hl.trades"], WINDOW)
    assert not result.passed
    assert not _checks(result)["has_data"].passed


def test_gate_records_verdict_and_guards_downstream(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    verdict, _ = gate_feed.run(tmp_path / "raw", ["hl.trades"], WINDOW, ledger)
    assert not verdict.passed
    with pytest.raises(GateRefusal, match="FAILED"):
        require(ledger, "feed")

    _write(tmp_path / "raw")
    verdict, _ = gate_feed.run(tmp_path / "raw", ["hl.trades"], WINDOW, ledger)
    assert verdict.passed
    assert require(ledger, "feed").passed
    # Both verdicts stay on the record; the failure cannot be re-run away.
    assert len([e for e in ledger.entries() if e.kind == "gate"]) == 2


def test_require_refuses_when_never_run(tmp_path):
    with pytest.raises(GateRefusal, match="never run"):
        require(Ledger(tmp_path / "ledger.jsonl"), "map")


def test_require_refuses_stale_verdict(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    _write(tmp_path / "raw")
    gate_feed.run(tmp_path / "raw", ["hl.trades"], WINDOW, ledger)
    with pytest.raises(GateRefusal, match="freshness"):
        require(ledger, "feed", max_age_ns=0)


def test_event_time_extractors():
    trades = STREAMS["hl.trades"]
    assert trades.event_time([{"time": 1700}, {"time": 1800}]) == 1800 * 1_000_000
    assert trades.event_time([]) is None
    assert STREAMS["hl.l2book"].event_time({"time": 5}) == 5_000_000
    assert STREAMS["hl.assetctxs"].event_time({"time": 5}) is None


def test_asset_contexts_pairs_universe_with_ctxs():
    payload = [{"universe": [{"name": "BTC"}, {"name": "ETH"}]},
               [{"markPx": "1"}, {"markPx": "2"}]]
    assert asset_contexts(payload) == [
        {"coin": "BTC", "markPx": "1"},
        {"coin": "ETH", "markPx": "2"},
    ]
    assert asset_contexts({"not": "a pair"}) == []
