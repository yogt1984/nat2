"""Bars, the tick path, and the two clocks that must not be conflated.

`t_event` says which bar a print belongs to. `t_ingest` says when that bar
could first have been used. Treating a close time as an arrival time is how
lookahead gets into a feature frame, so the distinction is pinned here.
"""

from __future__ import annotations

import pytest

from nat2.features.bars import Series, bars, iter_prints, path, visible_at

MIN = 60_000_000_000  # one minute in nanoseconds
MS = 1_000_000


def _record(t_ingest, trades):
    return {"t_ingest": t_ingest, "payload": trades}


def _trade(coin="BTC", px="100", sz="1", time_ms=0):
    return {"coin": coin, "px": px, "sz": sz, "time": time_ms, "users": ["0xa", "0xb"]}


# --- flattening ------------------------------------------------------------

def test_prints_carry_both_clocks():
    prints = iter_prints([_record(500 * MS, [_trade(time_ms=100)])])
    assert len(prints) == 1
    assert prints[0].t_event == 100 * MS
    assert prints[0].t_ingest == 500 * MS
    assert prints[0].notional == 100.0


def test_coin_filter_flattens_one_coin_and_keeps_order():
    records = [_record(1 * MS, [_trade("ETH", time_ms=5), _trade("BTC", time_ms=3)]),
               _record(2 * MS, [_trade("BTC", time_ms=1), _trade("SOL", time_ms=2)])]
    only = iter_prints(records, coin="BTC")
    assert [(p.coin, p.t_event) for p in only] == [("BTC", 1 * MS), ("BTC", 3 * MS)]
    assert only == [p for p in iter_prints(records) if p.coin == "BTC"]   # same prints, same order
    assert iter_prints(records, coin="DOGE") == []


def test_prints_are_sorted_by_market_time():
    records = [
        _record(10, [_trade(time_ms=300), _trade(time_ms=100)]),
        _record(20, [_trade(time_ms=200)]),
    ]
    assert [p.t_event for p in iter_prints(records)] == [100 * MS, 200 * MS, 300 * MS]


@pytest.mark.parametrize("bad", [
    {"coin": None}, {"px": "0"}, {"px": "-1"}, {"px": "abc"},
    {"sz": "0"}, {"sz": None}, {"time_ms": None},
])
def test_unusable_prints_are_dropped_not_defaulted(bad):
    trade = _trade(**bad)
    assert iter_prints([_record(10, [trade])]) == []


def test_junk_payloads_do_not_break_the_flattener():
    assert iter_prints([{"t_ingest": 1, "payload": {"not": "a list"}}]) == []
    assert iter_prints([{"payload": [_trade()]}]) == []
    assert iter_prints([_record(1, ["junk", None])]) == []


def test_negative_size_is_taken_as_magnitude():
    assert iter_prints([_record(1, [_trade(sz="-3")])])[0].sz == 3.0


# --- bar construction ------------------------------------------------------

def test_ohlc_follows_the_order_of_prints():
    prints = iter_prints([_record(1, [
        _trade(px="100", time_ms=0), _trade(px="105", time_ms=1),
        _trade(px="95", time_ms=2), _trade(px="102", time_ms=3),
    ])])
    bar = bars(prints, MIN)[0]
    assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 105.0, 95.0, 102.0)
    assert bar.prints == 4


def test_prints_bucket_by_market_time_not_arrival_time():
    # Both arrive in the same batch, but they happened a minute apart.
    prints = iter_prints([_record(999, [
        _trade(time_ms=0), _trade(time_ms=60_000),
    ])])
    assert len(bars(prints, MIN)) == 2


def test_a_bar_is_available_only_once_its_last_print_arrived():
    prints = iter_prints([
        _record(t_ingest=5 * MIN, trades=[_trade(time_ms=0)]),
        _record(t_ingest=9 * MIN, trades=[_trade(time_ms=1)]),
    ])
    bar = bars(prints, MIN)[0]
    assert bar.t_close == MIN
    # Closed at one minute, but not knowable until nine.
    assert bar.available_at == 9 * MIN


def test_a_bar_is_never_available_before_it_closes():
    # Real capture produced bars whose last print arrived 42s before the close.
    # Availability must not run ahead of completion: until a bar closes you do
    # not know it is finished, whatever has already arrived.
    prints = iter_prints([_record(t_ingest=1, trades=[_trade(time_ms=0)])])
    bar = bars(prints, MIN)[0]
    assert bar.available_at == bar.t_close
    assert visible_at([bar], bar.t_close - 1) == []


def test_visible_at_uses_arrival_not_close():
    prints = iter_prints([_record(t_ingest=9 * MIN, trades=[_trade(time_ms=0)])])
    built = bars(prints, MIN)
    assert visible_at(built, 2 * MIN) == [], "a closed bar is not yet a known bar"
    assert len(visible_at(built, 9 * MIN)) == 1


def test_empty_intervals_are_absent_not_zero_filled():
    # A period with no trades is a period with no information. Inventing a
    # flat zero-volume bar hands the model a fabricated observation.
    prints = iter_prints([_record(1, [_trade(time_ms=0), _trade(time_ms=180_000)])])
    built = bars(prints, MIN)
    assert [b.t_open for b in built] == [0, 3 * MIN]


def test_volume_notional_and_vwap():
    prints = iter_prints([_record(1, [
        _trade(px="100", sz="1", time_ms=0), _trade(px="200", sz="3", time_ms=1),
    ])])
    bar = bars(prints, MIN)[0]
    assert bar.volume == 4.0
    assert bar.notional == 700.0
    assert bar.vwap == 175.0


def test_derived_ratios_are_safe_on_a_degenerate_bar():
    from nat2.features.bars import Bar

    empty = Bar(coin="BTC", t_open=0, t_close=MIN)
    assert empty.vwap == 0.0 and empty.ret == 0.0 and empty.range_frac == 0.0


def test_coins_do_not_bleed_into_each_others_bars():
    prints = iter_prints([_record(1, [
        _trade(coin="BTC", px="100", time_ms=0),
        _trade(coin="ETH", px="5", time_ms=0),
    ])])
    assert bars(prints, MIN, coin="BTC")[0].close == 100.0
    assert bars(prints, MIN, coin="ETH")[0].close == 5.0


def test_a_non_positive_interval_yields_nothing():
    prints = iter_prints([_record(1, [_trade()])])
    assert bars(prints, 0) == [] and bars(prints, -1) == []


# --- the label path --------------------------------------------------------

def test_path_is_one_coin_in_market_order():
    prints = iter_prints([_record(1, [
        _trade(coin="BTC", px="100", time_ms=2),
        _trade(coin="ETH", px="5", time_ms=1),
        _trade(coin="BTC", px="101", time_ms=3),
    ])])
    assert path(prints, "BTC") == [(2 * MS, 100.0), (3 * MS, 101.0)]


def test_bars_and_path_agree_on_what_happened():
    # They are built from the same prints, so the OHLC and the label path can
    # never tell different stories about the same window.
    prints = iter_prints([_record(1, [
        _trade(px="100", time_ms=0), _trade(px="110", time_ms=1), _trade(px="90", time_ms=2),
    ])])
    bar = bars(prints, MIN)[0]
    prices = [px for _, px in path(prints, "BTC")]
    assert bar.high == max(prices) and bar.low == min(prices)


# --- series ----------------------------------------------------------------

def test_series_splits_by_coin():
    prints = iter_prints([_record(1, [
        _trade(coin="BTC", time_ms=0), _trade(coin="ETH", time_ms=0),
    ])])
    series = Series.build(prints, MIN)
    assert series.coins() == ["BTC", "ETH"]
    assert len(series.closes("BTC")) == 1
    assert series.closes("NOPE") == []
