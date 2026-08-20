"""Adversarial tests for liquidation extraction and map scoring.

Two things must not happen here. A malformed or unexpected payload must never
produce a *fabricated* event -- a wrong liquidation is worse than a missing
one, because it silently validates a map that was never tested. And a
liquidation that predates the map must never be counted as a prediction, which
is the lookahead this system exists to prevent.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from nat2.core.registry import Registry
from nat2.features.liqmath import Position
from nat2.features.liquidations import (
    LiquidationEvent,
    dedupe,
    from_fill,
    from_fills,
    method_notional,
    score,
)
from nat2.gates import map as gate_map
from nat2.ledger.chain import Ledger

MS = 1_000_000


def _fill(**kw) -> dict:
    base = {
        "coin": "BTC", "px": "100", "sz": "2", "side": "A", "time": 1000,
        "dir": "Open Long", "tid": 7,
        "liquidation": {"liquidatedUser": "0xvictim", "markPx": "99", "method": "market"},
    }
    base.update(kw)
    return base


def _event(**kw) -> LiquidationEvent:
    base = dict(
        tid=1, t_event=1000, coin="BTC", liquidated_user="0xvictim", mark_px=99.0,
        method="market", px=100.0, sz=2.0, observer="0xobs", source="counterparty",
    )
    base.update(kw)
    return LiquidationEvent(**base)


def _position(**kw) -> Position:
    base = dict(
        address="0xvictim", coin="BTC", szi=1.0, mark=100.0, max_leverage=40,
        margin_type="cross", account_value=10.0, maint_margin=0.0,
    )
    base.update(kw)
    return Position(**base)


# --- extraction: the happy paths ------------------------------------------

def test_counterparty_fill_yields_event():
    event = from_fill(_fill(), "0xobs")
    assert event is not None
    assert event.liquidated_user == "0xvictim"
    assert event.mark_px == 99.0
    assert event.source == "counterparty"
    assert event.t_event == 1000 * MS


def test_self_fill_yields_event_with_inferred_price():
    # The liquidated wallet's own side publishes no mark, so the fill price
    # stands in -- and `source` records that it was inferred.
    event = from_fill(_fill(liquidation=None, dir="Liquidated Isolated Short"), "0xme")
    assert event is not None
    assert event.source == "self"
    assert event.liquidated_user == "0xme"
    assert event.mark_px == 100.0 and event.method == "unknown"


def test_liquidated_dir_is_case_insensitive():
    for direction in ("liquidated cross long", "LIQUIDATED ISOLATED SHORT"):
        assert from_fill(_fill(liquidation=None, dir=direction), "0xme") is not None


def test_notional_uses_fill_price_not_mark():
    assert from_fill(_fill(), "0xobs").notional == 200.0


# --- extraction: everything that must NOT produce an event ----------------

@pytest.mark.parametrize("direction", ["Open Long", "Close Short", "Buy", "Settlement", ""])
def test_ordinary_dir_is_not_a_liquidation(direction):
    assert from_fill(_fill(liquidation=None, dir=direction), "0xme") is None


def test_dir_mentioning_liquidation_mid_string_is_not_a_liquidation():
    # Prefix match only: a future enum like "Not Liquidated" must not fabricate.
    assert from_fill(_fill(liquidation=None, dir="Not Liquidated Long"), "0xme") is None


@pytest.mark.parametrize("bad", [None, "liquidated", 42, [], True])
def test_non_dict_liquidation_object_falls_back_to_dir(bad):
    assert from_fill(_fill(liquidation=bad, dir="Open Long"), "0xobs") is None


def test_liquidation_object_without_victim_is_rejected():
    assert from_fill(_fill(liquidation={"markPx": "99", "method": "market"}), "0xobs") is None


def test_liquidation_object_with_empty_victim_is_rejected():
    assert from_fill(
        _fill(liquidation={"liquidatedUser": "", "markPx": "99"}), "0xobs"
    ) is None


@pytest.mark.parametrize("mark", ["0", "-5", "abc", None, "nan", "inf", ""])
def test_unusable_mark_price_is_rejected(mark):
    fill = _fill(liquidation={"liquidatedUser": "0xv", "markPx": mark, "method": "market"})
    assert from_fill(fill, "0xobs") is None


@pytest.mark.parametrize("tid", ["7", None, 7.5, True, False])
def test_non_integer_trade_id_is_rejected(tid):
    # bool is a subclass of int; True would collide with tid 1 when deduping.
    assert from_fill(_fill(tid=tid), "0xobs") is None


@pytest.mark.parametrize("field,value", [
    ("coin", None), ("coin", ""), ("px", "0"), ("px", "-1"), ("px", "abc"),
    ("sz", "0"), ("sz", None), ("time", None),
])
def test_missing_or_unusable_core_fields_are_rejected(field, value):
    assert from_fill(_fill(**{field: value}), "0xobs") is None


def test_negative_size_is_taken_as_magnitude():
    assert from_fill(_fill(sz="-3"), "0xobs").sz == 3.0


@pytest.mark.parametrize("fill", [None, "a string", 42, [], {"coin": "BTC"}])
def test_junk_input_is_rejected(fill):
    assert from_fill(fill, "0xobs") is None


def test_method_defaults_and_normalises_case():
    assert from_fill(_fill(liquidation={"liquidatedUser": "0xv", "markPx": "9"}),
                     "0xo").method == "unknown"
    assert from_fill(_fill(liquidation={"liquidatedUser": "0xv", "markPx": "9",
                                        "method": "BackStop"}), "0xo").method == "backstop"


def test_from_fills_tolerates_non_list_and_mixed_junk():
    assert from_fills(None, "0xo") == []
    assert from_fills({"not": "a list"}, "0xo") == []
    assert len(from_fills([_fill(), "junk", None, _fill(liquidation=None)], "0xo")) == 1


# --- dedupe ----------------------------------------------------------------

def test_dedupe_keeps_one_event_per_trade_id():
    assert len(dedupe([_event(tid=1), _event(tid=1, observer="0xother")])) == 1


def test_dedupe_prefers_the_counterparty_view():
    # The counterparty carries HL's published mark; the victim's own fill only
    # has an inferred price.
    events = dedupe([
        _event(tid=1, source="self", mark_px=50.0),
        _event(tid=1, source="counterparty", mark_px=99.0),
    ])
    assert events[0].source == "counterparty" and events[0].mark_px == 99.0


def test_dedupe_keeps_self_view_when_it_is_all_there_is():
    assert dedupe([_event(tid=1, source="self")])[0].source == "self"


def test_dedupe_orders_by_time_then_trade_id():
    events = dedupe([_event(tid=3, t_event=20), _event(tid=1, t_event=10),
                     _event(tid=2, t_event=10)])
    assert [e.tid for e in events] == [1, 2, 3]


# --- scoring: the lookahead guard -----------------------------------------

def test_event_before_the_snapshot_is_not_a_prediction():
    result = score([_event(t_event=500, mark_px=95.0)], [_position(liquidation_px=95.0)],
                   snapshot_ts=1000)
    assert result.pre_snapshot == 1 and result.scored == 0


def test_event_exactly_at_the_snapshot_is_not_a_prediction():
    # The map is stamped at snapshot_ts; a liquidation on that same instant was
    # not predicted by it.
    result = score([_event(t_event=1000, mark_px=95.0)], [_position(liquidation_px=95.0)],
                   snapshot_ts=1000)
    assert result.pre_snapshot == 1 and result.scored == 0


def test_event_after_the_snapshot_is_scored():
    result = score([_event(t_event=1001, mark_px=95.0)], [_position(liquidation_px=95.0)],
                   snapshot_ts=1000)
    assert result.scored == 1 and result.hits == 1


def test_miss_outside_tolerance_is_scored_but_not_a_hit():
    result = score([_event(t_event=2000, mark_px=80.0)], [_position(liquidation_px=95.0)],
                   snapshot_ts=1000)
    assert result.scored == 1 and result.hits == 0
    assert result.errors and result.errors[0] == pytest.approx(15 / 95)


def test_tolerance_boundary_counts_as_a_hit():
    result = score([_event(t_event=2000, mark_px=101.0)], [_position(liquidation_px=100.0)],
                   snapshot_ts=1000, tolerance=0.01)
    assert result.hits == 1


def test_just_outside_tolerance_boundary_is_a_miss():
    result = score([_event(t_event=2000, mark_px=101.5)], [_position(liquidation_px=100.0)],
                   snapshot_ts=1000, tolerance=0.01)
    assert result.hits == 0


def test_unmapped_wallet_is_counted_separately_not_as_a_miss():
    # Scoring a wallet we never mapped would punish the map for something it
    # never claimed.
    result = score([_event(t_event=2000, liquidated_user="0xstranger")],
                   [_position(liquidation_px=95.0)], snapshot_ts=1000)
    assert result.unmatched == 1 and result.scored == 0


def test_position_in_a_different_coin_does_not_match():
    result = score([_event(t_event=2000, coin="ETH")],
                   [_position(coin="BTC", liquidation_px=95.0)], snapshot_ts=1000)
    assert result.unmatched == 1 and result.scored == 0


def test_position_with_no_liquidation_price_is_unmapped():
    # Over-margined: HL publishes nothing and the derivation returns None, so
    # the map made no claim about this wallet.
    result = score([_event(t_event=2000)], [_position(account_value=1e9)], snapshot_ts=1000)
    assert result.unmatched == 1


def test_published_price_is_preferred_over_the_derivation():
    position = _position(liquidation_px=99.0, account_value=1.0)
    result = score([_event(t_event=2000, mark_px=99.0)], [position], snapshot_ts=1000)
    assert result.hits == 1


def test_empty_score_has_no_division_by_zero():
    result = score([], [], snapshot_ts=0)
    assert result.hit_rate == 0.0 and result.median_error == 0.0


def test_hit_rate_and_median_error_over_a_mixed_sample():
    positions = [_position(address=f"0xv{i}", liquidation_px=100.0) for i in range(4)]
    events = [
        _event(tid=1, t_event=2000, liquidated_user="0xv0", mark_px=100.0),
        _event(tid=2, t_event=2000, liquidated_user="0xv1", mark_px=100.5),
        _event(tid=3, t_event=2000, liquidated_user="0xv2", mark_px=150.0),
        _event(tid=4, t_event=2000, liquidated_user="0xv3", mark_px=50.0),
    ]
    result = score(events, positions, snapshot_ts=1000)
    assert result.scored == 4 and result.hits == 2
    assert result.hit_rate == 0.5
    assert result.summary()["hit_rate"] == 0.5


def test_method_notional_splits_by_method():
    split = method_notional([
        _event(tid=1, method="market", px=100.0, sz=1.0),
        _event(tid=2, method="backstop", px=100.0, sz=3.0),
    ])
    assert split == {"market": 100.0, "backstop": 300.0}


# --- persistence -----------------------------------------------------------

def test_registry_deduplicates_liquidations_across_scans(tmp_path):
    registry = Registry(tmp_path / "r.sqlite")
    assert registry.record_liquidations([_event(tid=1), _event(tid=2)]) == 2
    # A rescan re-reads the same fills; only genuinely new events count.
    assert registry.record_liquidations([_event(tid=1), _event(tid=3)]) == 1
    assert len(registry.liquidations()) == 3


def test_registry_round_trips_every_field(tmp_path):
    registry = Registry(tmp_path / "r.sqlite")
    original = _event(tid=9, t_event=1234, coin="ETH", liquidated_user="0xv",
                      mark_px=12.5, method="backstop", px=12.0, sz=3.0,
                      observer="0xo", source="counterparty")
    registry.record_liquidations([original])
    assert registry.liquidations()[0] == original


def test_registry_filters_liquidations_by_time(tmp_path):
    registry = Registry(tmp_path / "r.sqlite")
    registry.record_liquidations([_event(tid=1, t_event=100), _event(tid=2, t_event=300)])
    assert [e.tid for e in registry.liquidations(since_ns=200)] == [2]


def test_positions_ts_is_the_maps_epoch(tmp_path):
    registry = Registry(tmp_path / "r.sqlite")
    assert registry.positions_ts() is None
    registry.replace_positions([(_position(liquidation_px=95.0), "published")])
    assert registry.positions_ts() > 0


# --- gate integration -------------------------------------------------------

BANDS = (0.005, 0.01, 0.02, 0.05)


def _snap(t_ingest: int, mark: float = 100.0, up=None, down=None, coin="BTC") -> dict:
    up, down = up or {}, down or {}
    return {"t_ingest": t_ingest, "coin": coin, "mark": mark,
            "up": {str(b): up.get(b, 0.0) for b in BANDS},
            "down": {str(b): down.get(b, 0.0) for b in BANDS}}


def _gate(tmp_path, events, positions, series=None, window_n=30):
    """Pre-register (as TASK_2/03 did), then run the gate on events that
    postdate the registration -- the only events the verdict may use."""
    from nat2.features.liqmap import build

    ledger = Ledger(tmp_path / "l.jsonl")
    ledger.append("preregistration", {"name": "map_per_position_threshold", "pass_if": ">= 0.60",
                                      "window": {"n": window_n}})
    ledger.append("preregistration", {"name": "map_cluster_threshold", "window": {"n": window_n},
                                      "pass_if": {"side_hit_rate": ">= 0.60 with z >= 3 vs 0.50",
                                                  "band_hit_rate": "exceeds permutation-null mean by z >= 3"}})
    start = ledger.append("preregistration", {"name": "magnet_runnable_when"}).ts
    registry = Registry(tmp_path / "r.sqlite")
    registry.replace_positions([(p, "published") for p in positions])
    registry.record_liquidations([replace(e, t_event=e.t_event + start) for e in events])
    series = {c: [{**r, "t_ingest": r["t_ingest"] + start} for r in rows] for c, rows in (series or {}).items()}
    liqmap = build(registry.positions("BTC"), "BTC", 100.0, oi_notional=1.0)
    gate_map.WINDOW_EVENTS, gate_map.PERMUTATIONS = window_n, 200
    try:
        verdict, checks = gate_map.run(registry, [liqmap], ledger, map_series=series)
    finally:
        gate_map.WINDOW_EVENTS, gate_map.PERMUTATIONS = 1000, 1000
    return verdict, {c.name: c for c in checks}


def test_gate_refuses_a_perfect_but_tiny_sample(tmp_path):
    # Three lucky hits is not a validated map: the registered window is not filled.
    events = [_event(tid=i, t_event=50, mark_px=99.7) for i in range(3)]
    series = {"BTC": [_snap(1, down={0.005: 1e6})]}
    verdict, checks = _gate(tmp_path, events, [_position(liquidation_px=95.0)], series=series)
    assert not verdict.passed and verdict.detail["verdict"] == "refused"
    assert "window not filled: 3/30" in checks["window"].detail


def test_gate_rejects_a_side_hit_rate_no_better_than_a_coin_flip(tmp_path):
    # Mass below; half the flow arrives above. A map that calls the denser side
    # right half the time has told you nothing.
    events = [_event(tid=i, t_event=50 + i, mark_px=99.7 if i % 2 else 100.3)
              for i in range(40)]
    series = {"BTC": [_snap(1, down={0.005: 1e6}, up={0.005: 1.0})]}
    verdict, checks = _gate(tmp_path, events, [_position(liquidation_px=95.0)], series=series)
    assert not checks["cluster"].passed and not verdict.passed
    assert checks["cluster"].stats["side_hit_rate"] == pytest.approx(0.5)


def test_gate_passes_on_the_cluster_component_when_the_map_calls_the_side(tmp_path, monkeypatch):
    # The single fixture position derives poorly; fidelity is not under test here.
    monkeypatch.setattr(gate_map, "validate", lambda _: {"n": 1, "exact_frac": 1.0, "median": 0.0})
    # One snapshot per event: the permutation null's unit of independence is
    # the snapshot, so forty hits on a single map would be one draw, not forty.
    events = [_event(tid=i, t_event=50 + 10 * i, mark_px=99.7) for i in range(40)]
    series = {"BTC": [_snap(45 + 10 * i, down={0.005: 1e6}, up={0.005: 1.0}) for i in range(40)]}
    verdict, checks = _gate(tmp_path, events, [_position(liquidation_px=95.0)], series=series)
    assert checks["cluster"].passed and verdict.passed
    assert checks["cluster"].stats["side_baseline"] == 0.5
    assert checks["cluster"].stats["band_null"]["z"] > 3  # 2 of 8 slots carry mass; null ~0.25


def test_gate_refuses_without_any_map_history(tmp_path):
    events = [_event(tid=i, t_event=50 + i) for i in range(40)]
    verdict, checks = _gate(tmp_path, events, [_position(liquidation_px=95.0)])
    assert verdict.detail["verdict"] == "refused"
    assert verdict.detail["cumulative"]["predictive"]["no_map"] == 40


def test_gate_records_the_cumulative_summary_and_the_seqs_to_the_ledger(tmp_path):
    _gate(tmp_path, [_event(tid=1, t_event=50, mark_px=99.7)],
          [_position(liquidation_px=95.0)],
          series={"BTC": [_snap(1, down={0.005: 1e6})]})
    entry = Ledger(tmp_path / "l.jsonl").latest("gate", gate="map")
    assert entry.payload["detail"]["cumulative"]["predictive"]["scored"] == 1
    assert entry.payload["detail"]["judged_against"] == [0, 1, 2]
    assert Ledger(tmp_path / "l.jsonl").verify()[0]


def test_gate_map_fails_without_a_snapshot(tmp_path):
    registry = Registry(tmp_path / "r.sqlite")
    verdict, checks = gate_map.run(registry, [], Ledger(tmp_path / "l.jsonl"))
    assert not verdict.passed
    assert not {c.name: c for c in checks}["positions_fresh"].passed
