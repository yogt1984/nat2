"""The L0 frame, and the joins that could silently import the future.

Every input is joined by asking what had arrived by the bar's decision time.
Using the nearest observation instead — which is frequently the *next* one — is
the lookahead this system exists to prevent, so the as-of behaviour is pinned
from several directions.

The second theme is that missing must stay missing. A null map column and a
zero imbalance mean opposite things: one is "no reading", the other is "a
balanced book". Collapsing them would teach a model that quiet periods look
like balanced ones.
"""

from __future__ import annotations

import pytest

from nat2.features.bars import Bar
from nat2.features.context import Context
from nat2.features.frame import build
from nat2.features.liquidations import LiquidationEvent
from nat2.features.spec import FEATURES, declared, max_lookback, undeclared

MIN = 60_000_000_000


def _bar(i, close=100.0, available=None) -> Bar:
    t_open = i * MIN
    return Bar(coin="BTC", t_open=t_open, t_close=t_open + MIN, open=100.0,
               high=close, low=close, close=close, volume=1.0, notional=close,
               prints=5, available_at=available if available is not None else t_open + MIN)


def _ctx(t_ingest, mark=101.0, oracle=100.0) -> Context:
    return Context(t_ingest=t_ingest, coin="BTC", mark=mark, oracle=oracle,
                   funding=0.0001, open_interest=10.0, day_volume=1_000_000.0)


def _map(t_ingest, imb=0.5, up=1000.0, down=3000.0) -> dict:
    return {
        "t_ingest": t_ingest, "coin": "BTC", "coverage": 0.3, "published_frac": 0.9,
        "up": {"0.02": up}, "down": {"0.02": down},
        "imb": {"0.005": imb, "0.01": imb, "0.02": imb, "0.05": imb},
        "imb_cross": {"0.02": imb},
        "near": {"up_dist": 0.01, "down_dist": -0.02},
    }


# --- the contract with the registry ---------------------------------------

def test_every_emitted_column_is_declared():
    rows, _ = build([_bar(0)], [_ctx(0)], [_map(0)])
    assert undeclared(rows[0]) == set()
    assert set(rows[0]) <= declared()


def test_an_undeclared_column_fails_the_build(monkeypatch):
    # A column with no declared lookback is a column nobody can audit, and the
    # embargo width is computed from those lookbacks.
    import nat2.features.frame as frame_mod

    original = frame_mod.undeclared
    monkeypatch.setattr(frame_mod, "undeclared", lambda cols: {"sneaky"})
    with pytest.raises(ValueError, match="undeclared"):
        build([_bar(0)], [_ctx(0)], [_map(0)])
    monkeypatch.setattr(frame_mod, "undeclared", original)


def test_the_embargo_floor_comes_from_the_deepest_feature():
    assert max_lookback() == max(f.lookback for f in FEATURES.values())
    assert max_lookback() >= 120


# --- as-of joins -----------------------------------------------------------

def test_context_is_joined_as_of_the_decision_time():
    bar = _bar(0, available=5 * MIN)
    contexts = [_ctx(1 * MIN, mark=101.0), _ctx(9 * MIN, mark=200.0)]
    rows, _ = build([bar], contexts, [])
    # The 9-minute context had not arrived; using it would be the future.
    assert rows[0]["premium"] == pytest.approx(0.01)


def test_a_context_arriving_exactly_at_the_decision_time_is_used():
    rows, _ = build([_bar(0, available=5 * MIN)], [_ctx(5 * MIN)], [])
    assert rows[0]["premium"] is not None


def test_map_is_joined_as_of_the_decision_time():
    bar = _bar(0, available=5 * MIN)
    maps = [_map(1 * MIN, imb=0.2), _map(9 * MIN, imb=0.9)]
    rows, _ = build([bar], [_ctx(0)], maps)
    assert rows[0]["imb_002"] == pytest.approx(0.2)


def test_a_bar_that_predates_every_input_gets_nulls_not_zeros():
    # Null and zero mean opposite things: "no reading" versus "a balanced book".
    rows, stats = build([_bar(0, available=1)], [_ctx(10 * MIN)], [_map(10 * MIN)])
    row = rows[0]
    assert row["imb_002"] is None and row["coverage"] is None
    assert row["premium"] is None
    assert stats.with_map == 0 and stats.with_context == 0


def test_stats_report_how_much_of_the_frame_is_actually_populated():
    bars = [_bar(0, available=1), _bar(1, available=10 * MIN)]
    rows, stats = build(bars, [_ctx(9 * MIN)], [_map(9 * MIN)])
    assert stats.rows == 2
    assert stats.map_frac == 0.5 and stats.context_frac == 0.5
    assert stats.summary()["with_map"] == 1


# --- map-derived features --------------------------------------------------

def test_band_notionals_are_scaled_by_day_volume():
    rows, _ = build([_bar(0)], [_ctx(0)], [_map(0, up=2000.0, down=4000.0)])
    assert rows[0]["l_up_002"] == pytest.approx(2000.0 / 1_000_000.0)
    assert rows[0]["l_dn_002"] == pytest.approx(4000.0 / 1_000_000.0)


def test_band_notionals_are_withheld_without_a_volume_to_scale_by():
    ctx = Context(t_ingest=0, coin="BTC", mark=101.0, oracle=100.0, funding=0.0,
                  open_interest=1.0, day_volume=0.0)
    rows, _ = build([_bar(0)], [ctx], [_map(0)])
    assert rows[0]["l_up_002"] is None


def test_the_raw_distance_survives_even_without_sigma():
    # The sigma-scaled version needs a window; the fractional distance does
    # not, and losing it would leave the expert nothing to scale itself.
    rows, _ = build([_bar(0)], [_ctx(0)], [_map(0)])
    assert rows[0]["sigma"] is None
    assert rows[0]["d_near_up"] is None
    assert rows[0]["d_near_up_pct"] == pytest.approx(0.01)


def test_the_scaled_distance_is_in_one_bar_sigma_and_says_so():
    # Live data produced 72 "sigma" for a 1% cluster against 1.3bp bar sigma.
    # That is arithmetically right and meaningless at an hours-long horizon, so
    # the unit is declared and the raw distance travels alongside.
    from nat2.features.spec import FEATURES

    assert "ONE-BAR" in FEATURES["d_near_up"].description
    assert FEATURES["d_near_up_pct"].lookback == 0


def test_cluster_distance_is_expressed_in_sigma_once_available():
    bars = [_bar(i, close=100.0 + (i % 3)) for i in range(40)]
    rows, _ = build(bars, [_ctx(0)], [_map(0)])
    last = rows[-1]
    assert last["sigma"] is not None
    assert last["d_near_up"] == pytest.approx(0.01 / last["sigma"])
    assert last["d_near_dn"] < 0


def test_map_age_records_how_stale_the_join_was():
    rows, _ = build([_bar(0, available=5 * MIN)], [_ctx(0)], [_map(2 * MIN)])
    assert rows[0]["map_age_s"] == pytest.approx(180.0)


# --- event features --------------------------------------------------------

def _liq(t_event, px=100.0, sz=1.0) -> LiquidationEvent:
    return LiquidationEvent(tid=int(t_event), t_event=t_event, coin="BTC",
                            liquidated_user="0xv", mark_px=px, method="market",
                            px=px, sz=sz, observer="0xo", source="counterparty")


def test_tau_counts_bars_since_the_last_liquidation():
    bars = [_bar(i) for i in range(5)]
    rows, _ = build(bars, [_ctx(0)], [], liquidations=[_liq(1 * MIN)])
    # Bar 0's decision time is exactly 1 minute, and the liquidation landed on
    # it -- visible, same inclusive rule the context join uses.
    assert rows[0]["tau"] == 0
    assert rows[4]["tau"] == 4


def test_a_liquidation_strictly_after_the_decision_time_is_invisible():
    bars = [_bar(0)]
    rows, _ = build(bars, [_ctx(0)], [], liquidations=[_liq(1 * MIN + 1)])
    assert rows[0]["tau"] is None


def test_a_liquidation_that_has_not_reached_us_has_not_happened():
    bars = [_bar(0, available=1 * MIN)]
    rows, _ = build(bars, [_ctx(0)], [], liquidations=[_liq(9 * MIN)])
    assert rows[0]["tau"] is None and rows[0]["liq_flow"] == 0.0


def test_liq_flow_sums_only_the_trailing_window():
    bars = [_bar(i) for i in range(40)]
    rows, _ = build(bars, [_ctx(0)], [], liquidations=[_liq(0), _liq(38 * MIN)])
    # The first liquidation is far outside the 30-bar window by the last bar.
    assert rows[-1]["liq_flow"] == pytest.approx(100.0)


# --- degenerate inputs -----------------------------------------------------

def test_an_empty_bar_list_yields_an_empty_frame():
    rows, stats = build([], [_ctx(0)], [_map(0)])
    assert rows == [] and stats.rows == 0


def test_a_frame_with_no_inputs_at_all_still_produces_rows():
    rows, stats = build([_bar(0)], [], [])
    assert len(rows) == 1
    assert rows[0]["premium"] is None and rows[0]["imb_002"] is None
    assert undeclared(rows[0]) == set()
