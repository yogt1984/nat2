"""Premium, funding and OI features, and the statistics that must not peek.

`premium = (mark - oracle) / oracle` is the measurement that makes a
single-venue design defensible, so its sign convention and its edge cases are
worth pinning. The rolling statistics are worth pinning harder: a z-score that
includes one sample from the future is the classic way a backtest learns to
predict its own inputs.
"""

from __future__ import annotations

import pytest

from nat2.features.context import (
    Context,
    as_of,
    by_coin,
    features,
    iter_contexts,
    rolling_z,
)


def _payload(coins):
    return [
        {"universe": [{"name": c} for c in coins]},
        [
            {"markPx": str(v["mark"]), "oraclePx": str(v["oracle"]),
             "funding": str(v.get("funding", 0)), "openInterest": str(v.get("oi", 0)),
             "dayNtlVlm": str(v.get("vlm", 0))}
            for v in coins.values()
        ],
    ]


def _record(t_ingest, coins):
    return {"t_ingest": t_ingest, "payload": _payload(coins)}


def _ctx(**kw) -> Context:
    base = dict(t_ingest=1, coin="BTC", mark=100.0, oracle=100.0, funding=0.0,
                open_interest=0.0, day_volume=0.0)
    base.update(kw)
    return Context(**base)


# --- premium ---------------------------------------------------------------

def test_premium_is_positive_when_hl_trades_above_the_oracle():
    assert _ctx(mark=101.0, oracle=100.0).premium == pytest.approx(0.01)


def test_premium_is_negative_when_hl_trades_below_the_oracle():
    assert _ctx(mark=99.0, oracle=100.0).premium == pytest.approx(-0.01)


def test_premium_without_an_oracle_is_zero_not_infinite():
    assert _ctx(mark=100.0, oracle=0.0).premium == 0.0


def test_oi_notional_uses_the_mark():
    assert _ctx(mark=50.0, open_interest=4.0).oi_notional == 200.0


# --- flattening ------------------------------------------------------------

def test_contexts_are_flattened_per_coin():
    record = _record(10, {"BTC": {"mark": 100, "oracle": 99}, "ETH": {"mark": 5, "oracle": 5}})
    contexts = iter_contexts([record])
    assert {c.coin for c in contexts} == {"BTC", "ETH"}
    assert all(c.t_ingest == 10 for c in contexts)


def test_a_coin_without_a_mark_is_dropped():
    record = {"t_ingest": 1, "payload": [
        {"universe": [{"name": "BTC"}]}, [{"markPx": "0", "oraclePx": "1"}],
    ]}
    assert iter_contexts([record]) == []


def test_junk_payloads_are_ignored():
    assert iter_contexts([{"t_ingest": 1, "payload": {"not": "a pair"}}]) == []
    assert iter_contexts([{"payload": _payload({"BTC": {"mark": 1, "oracle": 1}})}]) == []


def test_contexts_are_sorted_by_arrival():
    # This stream carries no exchange clock, so arrival time IS observation
    # time -- a fact to state rather than paper over.
    records = [_record(30, {"BTC": {"mark": 3, "oracle": 3}}),
               _record(10, {"BTC": {"mark": 1, "oracle": 1}})]
    assert [c.t_ingest for c in iter_contexts(records)] == [10, 30]


def test_by_coin_groups_preserving_order():
    contexts = [_ctx(t_ingest=1), _ctx(t_ingest=2, coin="ETH"), _ctx(t_ingest=3)]
    grouped = by_coin(contexts)
    assert [c.t_ingest for c in grouped["BTC"]] == [1, 3]


# --- rolling statistics: no peeking ---------------------------------------

def test_z_score_is_none_until_the_window_is_full():
    # Three samples do not make a small-sample estimate; emitting 0.0 there
    # would let a model trade on noise wearing a statistic's clothes.
    assert rolling_z([1.0, 2.0, 3.0, 4.0], window=3)[:2] == [None, None]
    assert rolling_z([1.0, 2.0, 3.0, 4.0], window=3)[2] is not None


def test_z_score_looks_only_backwards():
    rising = [1.0, 2.0, 3.0, 100.0]
    values = rolling_z(rising, window=3)
    # The spike at index 3 must not affect index 2.
    assert values[2] == pytest.approx(rolling_z([1.0, 2.0, 3.0], window=3)[2])


def test_z_score_includes_the_current_point():
    values = rolling_z([0.0, 0.0, 3.0], window=3)
    assert values[2] > 0, "the current observation is part of its own window"


def test_a_flat_window_has_zero_z_not_a_division_by_zero():
    assert rolling_z([5.0, 5.0, 5.0], window=3)[2] == 0.0


def test_a_degenerate_window_yields_nothing_usable():
    assert rolling_z([1.0, 2.0], window=1) == [None, None]
    assert rolling_z([1.0, 2.0], window=0) == [None, None]


def test_z_score_of_a_short_series_is_all_none():
    assert rolling_z([1.0], window=5) == [None]


# --- as_of -----------------------------------------------------------------

def test_as_of_forward_fills_the_last_known_observation():
    contexts = [_ctx(t_ingest=10, mark=1.0), _ctx(t_ingest=20, mark=2.0)]
    assert as_of(contexts, 15).mark == 1.0
    assert as_of(contexts, 20).mark == 2.0


def test_as_of_never_interpolates():
    # An interpolated context is a price nobody published, sitting between two
    # that somebody did.
    contexts = [_ctx(t_ingest=10, mark=1.0), _ctx(t_ingest=20, mark=3.0)]
    assert as_of(contexts, 15).mark == 1.0


def test_as_of_before_the_first_observation_is_none():
    assert as_of([_ctx(t_ingest=10)], 5) is None


def test_as_of_on_an_empty_series_is_none():
    assert as_of([], 100) is None


# --- assembled features ----------------------------------------------------

def test_features_carry_the_arrival_clock_and_the_z_scores():
    contexts = [_ctx(t_ingest=i, mark=100.0 + i, oracle=100.0, funding=0.0001 * i,
                     open_interest=10.0 + i) for i in range(5)]
    rows = features(contexts, window=3)
    assert len(rows) == 5
    assert rows[0]["premium_z"] is None and rows[4]["premium_z"] is not None
    assert rows[4]["t_ingest"] == 4
    assert set(rows[0]) >= {"premium", "premium_z", "funding_z", "oi_z", "oi_notional"}


def test_features_on_an_empty_series():
    assert features([], window=3) == []


# --- the windowed reader (TASK_2/16 finding) -------------------------------

def test_latest_contexts_widens_until_it_finds_data_and_never_thins_the_answer(tmp_path):
    """Reading the whole `hl.assetctxs` stream cost 59.7s per call against 0.7s for the last
    hour, for identical values -- and `replay` paid it every five minutes while `mapsnap`
    paid it every pass, which is what left map snapshots ~7 min apart and stale over half
    the time. The window must widen rather than return fewer coins."""
    from nat2.core.clock import NS, now_ns
    from nat2.features.context import latest_contexts
    from nat2.io.worm import WormWriter

    now = now_ns()
    payload = lambda mark: [{"universe": [{"name": "BTC"}]},
                            [{"markPx": str(mark), "oraclePx": str(mark), "funding": "0",
                              "openInterest": "1", "dayNtlVlm": "1"}]]
    with WormWriter(tmp_path, "hl.assetctxs") as w:
        w.write(payload(100), None, now - 40 * 3600 * NS)     # older than the first two windows
        w.write(payload(101), None, now - 30 * 3600 * NS)

    windows = (3600 * NS, 6 * 3600 * NS, 48 * 3600 * NS, None)
    found = latest_contexts(tmp_path, windows)
    assert set(found) == {"BTC"} and found["BTC"].mark == 101.0     # newest, after widening twice
    assert latest_contexts(tmp_path, (3600 * NS,)) == {}            # a window with nothing says nothing
    assert latest_contexts(tmp_path, (None,))["BTC"].mark == 101.0  # the full scan agrees
    assert latest_contexts(tmp_path / "empty") == {}
