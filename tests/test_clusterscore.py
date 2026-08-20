"""Cluster-level scoring: did forced flow arrive where the map said mass was?

Per-position scoring could not accumulate. `replace_positions` keeps only the
present, so a liquidated wallet is gone from the table before it can be
matched, and `snapshot_ts` advances past every event on each sweep. Twenty-eight
consecutive readings of `mapped_notional_frac = 0.0` were the shape of that
measurement, not a fact about the market.

This drops the ownership claim and keeps the falsifiable one: the map named
prices, and liquidations either landed there or they did not.
"""

from __future__ import annotations

from nat2.features.liquidations import LiquidationEvent, _band_for, score_clusters

BANDS = (0.005, 0.01, 0.02, 0.05)


def _snap(t_ingest: int, mark: float, up: dict, down: dict, coin: str = "BTC") -> dict:
    return {
        "t_ingest": t_ingest,
        "coin": coin,
        "mark": mark,
        "up": {str(b): up.get(b, 0.0) for b in BANDS},
        "down": {str(b): down.get(b, 0.0) for b in BANDS},
    }


def _event(t_event: int, mark_px: float, coin: str = "BTC", tid: int = 1) -> LiquidationEvent:
    return LiquidationEvent(
        tid=tid, t_event=t_event, coin=coin, liquidated_user="0xw", mark_px=mark_px,
        method="market", px=mark_px, sz=1.0, observer="0xo", source="counterparty",
    )


# --- the lookahead rule ----------------------------------------------------

def test_a_snapshot_taken_after_the_liquidation_is_never_used():
    # The later map may already contain the liquidation's consequences.
    later = _snap(t_ingest=200, mark=100.0, up={}, down={0.005: 1e6})
    result = score_clusters([_event(t_event=100, mark_px=99.7)], {"BTC": [later]}, BANDS)
    assert result.scored == 0 and result.pre_map == 1


def test_a_snapshot_at_the_same_nanosecond_is_also_refused():
    same = _snap(t_ingest=100, mark=100.0, up={}, down={0.005: 1e6})
    result = score_clusters([_event(t_event=100, mark_px=99.7)], {"BTC": [same]}, BANDS)
    assert result.scored == 0 and result.pre_map == 1


def test_the_most_recent_snapshot_before_the_event_is_the_one_used():
    stale = _snap(t_ingest=10, mark=100.0, up={0.005: 1e6}, down={})
    fresh = _snap(t_ingest=90, mark=100.0, up={}, down={0.005: 1e6})
    later = _snap(t_ingest=500, mark=100.0, up={0.005: 1e9}, down={})
    result = score_clusters(
        [_event(t_event=100, mark_px=99.7)], {"BTC": [stale, fresh, later]}, BANDS
    )
    # `fresh` put the mass below, and the event landed below.
    assert result.scored == 1 and result.side_hits == 1


# --- what a hit means ------------------------------------------------------

def test_a_liquidation_below_the_mark_scores_against_the_downside_mass():
    snap = _snap(t_ingest=1, mark=100.0, up={0.005: 10.0}, down={0.005: 1e6})
    result = score_clusters([_event(t_event=50, mark_px=99.7)], {"BTC": [snap]}, BANDS)
    assert result.scored == 1
    assert result.side_hits == 1 and result.band_hits == 1


def test_the_map_is_marked_wrong_when_flow_arrives_on_the_thin_side():
    # Mass sat below; the liquidation happened above. That is a miss, and the
    # score has to say so rather than crediting "there was a cluster somewhere".
    snap = _snap(t_ingest=1, mark=100.0, up={0.005: 10.0}, down={0.005: 1e6})
    result = score_clusters([_event(t_event=50, mark_px=100.3)], {"BTC": [snap]}, BANDS)
    assert result.scored == 1 and result.side_hits == 0


def test_an_event_beyond_the_widest_band_is_not_scored():
    # The map made no claim out there, so counting it either way would be a
    # verdict on a prediction that was never made.
    snap = _snap(t_ingest=1, mark=100.0, up={}, down={0.05: 1e6})
    result = score_clusters([_event(t_event=50, mark_px=80.0)], {"BTC": [snap]}, BANDS)
    assert result.scored == 0 and result.outside_span == 1


def test_a_band_with_no_mass_is_scored_and_missed_not_skipped():
    # An empty band is a real claim -- "nothing here" -- and flow arriving
    # there is evidence against the map.
    snap = _snap(t_ingest=1, mark=100.0, up={}, down={})
    result = score_clusters([_event(t_event=50, mark_px=99.7)], {"BTC": [snap]}, BANDS)
    assert result.scored == 1 and result.band_hits == 0


def test_a_coin_with_no_snapshots_is_counted_apart(  ):
    snap = _snap(t_ingest=1, mark=100.0, up={}, down={0.005: 1e6})
    result = score_clusters([_event(t_event=50, mark_px=99.7, coin="ETH")], {"BTC": [snap]}, BANDS)
    assert result.scored == 0 and result.no_map == 1


# --- staleness -------------------------------------------------------------

def test_a_map_older_than_the_bands_can_survive_is_set_aside():
    # The tightest band is 0.5% wide; a low-cap alt moves further than that in
    # minutes, so scoring against an old mark measures elapsed price movement
    # and calls it a prediction.
    snap = _snap(t_ingest=0, mark=100.0, up={}, down={0.005: 1e6})
    event = _event(t_event=10 * 60 * 1_000_000_000, mark_px=99.7)
    result = score_clusters([event], {"BTC": [snap]}, BANDS, max_age_ns=5 * 60 * 1_000_000_000)
    assert result.scored == 0 and result.stale_map == 1


def test_a_fresh_map_is_scored_normally():
    snap = _snap(t_ingest=0, mark=100.0, up={}, down={0.005: 1e6})
    event = _event(t_event=60 * 1_000_000_000, mark_px=99.7)
    result = score_clusters([event], {"BTC": [snap]}, BANDS, max_age_ns=5 * 60 * 1_000_000_000)
    assert result.scored == 1 and result.stale_map == 0


def test_the_age_bound_can_be_disabled():
    snap = _snap(t_ingest=0, mark=100.0, up={}, down={0.005: 1e6})
    event = _event(t_event=10 * 60 * 1_000_000_000, mark_px=99.7)
    assert score_clusters([event], {"BTC": [snap]}, BANDS, max_age_ns=0).scored == 1


# --- band selection --------------------------------------------------------

def test_the_tightest_containing_band_is_chosen():
    assert _band_for(0.003, BANDS) == 0.005
    assert _band_for(0.005, BANDS) == 0.005   # inclusive at the edge
    assert _band_for(0.012, BANDS) == 0.02
    assert _band_for(0.2, BANDS) is None


# --- the summary -----------------------------------------------------------

def test_the_summary_carries_its_own_baseline():
    snap = _snap(t_ingest=1, mark=100.0, up={0.005: 10.0}, down={0.005: 1e6})
    events = [_event(t_event=50 + i, mark_px=99.7, tid=i) for i in range(4)]
    summary = score_clusters(events, {"BTC": [snap]}, BANDS).summary()
    # A hit rate reported without the coin flip it must beat is not a result.
    assert summary["side_hit_rate"] == 1.0
    assert summary["side_baseline"] == 0.5
    assert summary["scored"] == 4 and summary["events"] == 4


def test_rates_are_zero_rather_than_undefined_when_nothing_scored():
    result = score_clusters([], {}, BANDS)
    assert result.side_hit_rate == 0.0 and result.band_hit_rate == 0.0
    assert result.median_distance == 0.0


def test_the_summary_reports_precision_not_just_a_rate():
    # A rate without its precision invites the same mistake as a rate without
    # its baseline. 42.8% over 208 events is ~2 standard errors from chance.
    snap = _snap(t_ingest=1, mark=100.0, up={0.005: 10.0}, down={0.005: 1e6})
    events = [_event(t_event=50 + i, mark_px=99.7, tid=i) for i in range(100)]
    summary = score_clusters(events, {"BTC": [snap]}, BANDS).summary()
    assert summary["stderr"] == 0.05
    assert summary["z"] == 10.0


def test_precision_is_zero_rather_than_undefined_on_an_empty_score():
    result = score_clusters([], {}, BANDS)
    assert result.stderr == 0.0 and result.z == 0.0
