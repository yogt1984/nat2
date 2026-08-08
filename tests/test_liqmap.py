"""Liquidation map, coverage arithmetic, and the derivation's honesty."""

from __future__ import annotations

import pytest

from nat2.core.registry import Registry
from nat2.features.liqmap import OI_SIDES, build
from nat2.features.liqmath import Position, derive, effective, validate
from nat2.gates import map as gate_map
from nat2.hl.leaderboard import LeaderboardRow, seed
from nat2.io.snapshot import parse_state
from nat2.ledger.chain import Ledger


def _position(**kw) -> Position:
    base = dict(
        address="0xa", coin="BTC", szi=1.0, mark=100.0, max_leverage=40,
        margin_type="cross", account_value=1000.0, maint_margin=0.0,
    )
    return Position(**{**base, **kw})


def test_derive_long_and_short_bracket_the_mark():
    long_px = derive(_position(szi=1.0, account_value=10.0))
    short_px = derive(_position(szi=-1.0, account_value=10.0))
    assert long_px < 100.0 < short_px


def test_published_wins_over_derived():
    position = _position(liquidation_px=42.0)
    assert effective(position) == (42.0, "published")
    assert effective(_position())[1] == "derived"


def test_overmargined_position_cannot_be_liquidated():
    # Equity far exceeding the position: the implied liquidation price is
    # below zero, which means "never", not "at zero".
    assert derive(_position(account_value=1000.0)) is None


def test_validate_reports_error_against_published():
    truth = derive(_position(account_value=10.0))
    exact = _position(account_value=10.0, liquidation_px=truth)
    wrong = _position(address="0xb", account_value=10.0, liquidation_px=truth * 2)
    stats = validate([exact, wrong])
    assert stats["n"] == 2
    assert stats["exact_frac"] == 0.5


def test_coverage_uses_two_sided_denominator():
    # Every long has a matching short, so venue-wide position notional is
    # OI_SIDES x OI notional. Getting this wrong is a factor of two on the one
    # number the map is judged by.
    positions = [_position(address=f"0x{i}", szi=1.0, liquidation_px=95.0) for i in range(4)]
    liqmap = build(positions, "BTC", mark=100.0, oi_notional=1000.0)
    assert liqmap.total_notional == 400.0
    assert liqmap.coverage == 400.0 / (1000.0 * OI_SIDES)


def test_map_places_notional_and_computes_imbalance():
    below = [_position(address=f"0xd{i}", szi=1.0, liquidation_px=99.0) for i in range(3)]
    above = [_position(address="0xu", szi=-1.0, liquidation_px=101.0)]
    liqmap = build(below + above, "BTC", mark=100.0, oi_notional=1000.0)
    assert liqmap.positions == 4
    assert liqmap.down[0.02] == 300.0 and liqmap.up[0.02] == 100.0
    assert liqmap.imbalance(0.02) == 0.5
    assert sum(b.notional for b in liqmap.buckets) == 400.0


def test_unplaceable_position_still_counts_toward_coverage():
    # Observed but unmappable: coverage must not quietly shrink to hide it.
    liqmap = build([_position(account_value=1000.0)], "BTC", mark=100.0, oi_notional=1000.0)
    assert liqmap.skipped == 1
    assert liqmap.total_notional > 0


def test_parse_state_reads_published_liquidation_px():
    state = {
        "crossMarginSummary": {"accountValue": "1000"},
        "crossMaintenanceMarginUsed": "10",
        "assetPositions": [
            {"position": {"coin": "BTC", "szi": "-2", "positionValue": "200",
                          "maxLeverage": 40, "liquidationPx": "150",
                          "leverage": {"type": "cross", "value": 3}}},
            {"position": {"coin": "ETH", "szi": "0", "positionValue": "0",
                          "maxLeverage": 25, "leverage": {"type": "cross", "value": 1}}},
        ],
    }
    positions = parse_state("0xa", state)
    assert len(positions) == 1
    assert positions[0].mark == 100.0 and positions[0].liquidation_px == 150.0


def test_seed_tags_union_of_both_orderings():
    rows = [
        LeaderboardRow("0xrich", 1e9, 0, 0, 0, 0),
        LeaderboardRow("0xbusy", 1, 0, 1e9, 0, 0),
        LeaderboardRow("0xboth", 1e8, 0, 1e8, 0, 0),
    ]
    tags = seed(rows, top_equity=2, top_volume=2)
    assert tags["0xrich"] == "equity" and tags["0xbusy"] == "volume"
    assert tags["0xboth"] == "both"


def test_gate_map_fails_on_insufficient_history(tmp_path):
    registry = Registry(tmp_path / "r.sqlite")
    registry.replace_positions([(_position(liquidation_px=95.0), "published")])
    liqmap = build(registry.positions("BTC"), "BTC", 100.0, oi_notional=1.0)
    verdict, checks = gate_map.run(registry, [liqmap], Ledger(tmp_path / "l.jsonl"))
    names = {c.name: c for c in checks}
    assert names["coverage"].passed          # tiny OI, so coverage is huge
    assert not names["predictive"].passed    # no liquidations observed yet
    assert not verdict.passed, "a gate that cannot be evaluated must not pass"


def test_gate_map_fails_without_a_snapshot(tmp_path):
    registry = Registry(tmp_path / "r.sqlite")
    verdict, checks = gate_map.run(registry, [], Ledger(tmp_path / "l.jsonl"))
    assert not verdict.passed
    assert not {c.name: c for c in checks}["positions_fresh"].passed


def test_resolution_changes_the_histogram_but_not_the_totals():
    # Resolution is a display choice. Band totals and imbalance come from each
    # position's exact price, so they must not move when buckets get finer.
    positions = [_position(address=f"0x{i}", szi=1.0, liquidation_px=99.0 + i * 0.1)
                 for i in range(8)]
    coarse = build(positions, "BTC", 100.0, 1000.0, bucket_pct=0.01)
    fine = build(positions, "BTC", 100.0, 1000.0, bucket_pct=0.0005)
    assert coarse.total_notional == fine.total_notional
    assert coarse.imbalance(0.02) == fine.imbalance(0.02)
    assert coarse.down[0.02] == fine.down[0.02]
    # ...but the finer map spreads the same notional over more buckets.
    assert sum(1 for b in fine.buckets if b.notional) > sum(
        1 for b in coarse.buckets if b.notional
    )


def test_finer_buckets_never_lose_notional():
    positions = [_position(address=f"0x{i}", szi=1.0, liquidation_px=99.5) for i in range(3)]
    fine = build(positions, "BTC", 100.0, 1000.0, bucket_pct=0.0001, span=0.1)
    assert sum(b.notional for b in fine.buckets) == pytest.approx(fine.total_notional)


def test_positions_beyond_the_span_are_counted_not_hidden():
    # A window that hides its own edges invites the reader to mistake it for
    # the whole picture.
    near = _position(address="0xnear", liquidation_px=99.0)
    far = _position(address="0xfar", liquidation_px=50.0)
    liqmap = build([near, far], "BTC", 100.0, 1000.0, span=0.05)
    assert liqmap.outside_span == 1
    assert liqmap.positions == 2
    assert liqmap.total_notional == 200.0
    assert sum(b.notional for b in liqmap.buckets) == 100.0
    assert liqmap.summary()["outside_span"] == 1


def test_span_widens_to_include_a_distant_cluster():
    far = _position(address="0xfar", liquidation_px=50.0)
    assert build([far], "BTC", 100.0, 1000.0, span=0.05).outside_span == 1
    assert build([far], "BTC", 100.0, 1000.0, span=0.60).outside_span == 0


def test_degenerate_resolution_still_produces_a_map():
    liqmap = build([_position(liquidation_px=99.0)], "BTC", 100.0, 1000.0,
                   bucket_pct=1.0, span=0.05)
    assert liqmap.buckets and liqmap.total_notional == 100.0
