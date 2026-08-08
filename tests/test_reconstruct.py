"""Carrying positions forward, and the ways that quietly corrupts a map.

Three failures matter more than the rest. Keeping HL's published liquidation
price after the size changed is a silent lie — the number describes a position
that no longer exists. Applying the same tape twice moves a position that never
moved. And inventing a liquidation price for a wallet whose equity we have
never seen fabricates a cluster out of nothing.
"""

from __future__ import annotations

import pytest

from nat2.core.clock import now_ns
from nat2.core.reconstruct import DUST_FRACTION, apply, drift
from nat2.core.registry import Registry
from nat2.features.fills import Delta
from nat2.features.liqmath import Position, effective
from nat2.io.replay import replay
from nat2.io.worm import WormWriter


def _position(**kw) -> Position:
    base = dict(
        address="0xa", coin="BTC", szi=2.0, mark=100.0, max_leverage=40,
        margin_type="cross", account_value=50.0, maint_margin=1.0,
        liquidation_px=80.0,
    )
    base.update(kw)
    return Position(**base)


def _delta(**kw) -> Delta:
    base = dict(address="0xa", coin="BTC", dsz=1.0, notional=100.0, last_px=101.0, fills=1)
    base.update(kw)
    return Delta(**base)


# --- the published price must not survive a size change --------------------

def test_published_liquidation_price_is_discarded_when_size_changes():
    # It described a position that no longer exists. Keeping it would put a
    # confident, wrong cluster on the map.
    result = apply([_position(liquidation_px=80.0)], [_delta(dsz=1.0)])
    carried, source = result.upserts[0]
    assert carried.liquidation_px is None
    assert source == "derived"
    assert effective(carried)[1] == "derived"


def test_size_and_mark_are_updated_but_equity_is_carried_stale():
    # The tape reports trades, not equity. Carrying it forward is the best
    # available, and is exactly why derived is a fallback not a replacement.
    result = apply([_position(szi=2.0)], [_delta(dsz=1.5)], marks={"BTC": 111.0})
    carried, _ = result.upserts[0]
    assert carried.szi == 3.5
    assert carried.mark == 111.0
    assert carried.account_value == 50.0 and carried.maint_margin == 1.0


def test_mark_falls_back_to_the_last_traded_price():
    result = apply([_position()], [_delta(last_px=123.0)], marks={})
    assert result.upserts[0][0].mark == 123.0


# --- position lifecycle ----------------------------------------------------

def test_reducing_a_position_keeps_it():
    result = apply([_position(szi=5.0)], [_delta(dsz=-2.0)])
    assert result.upserts[0][0].szi == 3.0
    assert result.updated == 1 and not result.closes


def test_closing_to_exactly_zero_removes_the_position():
    result = apply([_position(szi=2.0)], [_delta(dsz=-2.0)])
    assert result.closes == [("0xa", "BTC")]
    assert not result.upserts


def test_dust_residue_is_treated_as_closed():
    # A floating-point crumb would otherwise sit on the map forever carrying a
    # meaningless liquidation price.
    result = apply([_position(szi=2.0)], [_delta(dsz=-2.0 + 2.0 * DUST_FRACTION / 2)])
    assert result.closes == [("0xa", "BTC")]


def test_flipping_through_zero_is_kept_and_counted():
    result = apply([_position(szi=2.0)], [_delta(dsz=-5.0)])
    carried, _ = result.upserts[0]
    assert carried.szi == -3.0
    assert result.flipped == 1


def test_a_flipped_position_prices_on_the_other_side_of_the_mark():
    long_side = apply([_position(szi=2.0, liquidation_px=None)], [_delta(dsz=1.0)])
    short_side = apply([_position(szi=2.0, liquidation_px=None)], [_delta(dsz=-5.0)])
    long_px = effective(long_side.upserts[0][0])[0]
    short_px = effective(short_side.upserts[0][0])[0]
    assert long_px < 100.0 < short_px


# --- new positions ---------------------------------------------------------

def test_a_new_coin_borrows_account_equity_from_the_same_wallet():
    result = apply([_position(coin="ETH")], [_delta(coin="BTC", dsz=3.0)])
    assert result.opened == 1
    carried, source = result.upserts[0]
    assert carried.coin == "BTC" and carried.szi == 3.0
    assert carried.account_value == 50.0
    assert source == "derived"


def test_a_new_position_without_a_leverage_figure_is_unpriceable_not_invented():
    # No maxLeverage for a coin this wallet was never swept holding, so no
    # maintenance fraction and no honest liquidation price.
    result = apply([_position(coin="ETH")], [_delta(coin="BTC", dsz=3.0)])
    carried, _ = result.upserts[0]
    assert result.unpriceable == 1
    assert effective(carried)[0] is None


def test_a_wallet_never_swept_is_ignored_entirely():
    # We have no equity figure for it, so any liquidation price would be
    # fabricated. Counted as ignored rather than guessed.
    result = apply([], [_delta(address="0xnever")])
    assert result.ignored == 1 and not result.upserts


def test_a_new_position_that_nets_to_dust_is_not_opened():
    result = apply([_position(coin="ETH")], [_delta(coin="BTC", dsz=DUST_FRACTION / 2)])
    assert result.opened == 0 and not result.upserts


# --- drift -----------------------------------------------------------------

def test_drift_measures_relative_size_error():
    stats = drift([_position(szi=2.0)], [_position(szi=2.0)])
    assert stats["compared"] == 1 and stats["exact_frac"] == 1.0
    stats = drift([_position(szi=3.0)], [_position(szi=2.0)])
    assert stats["median"] == pytest.approx(0.5)


def test_drift_counts_positions_the_sweep_no_longer_sees():
    stats = drift([_position(coin="DOGE")], [_position(coin="BTC")])
    assert stats["missing"] == 1 and stats["compared"] == 0


def test_drift_on_nothing_does_not_divide_by_zero():
    assert drift([], [])["median"] == 0.0


# --- replay idempotence ----------------------------------------------------

def _write_tape(root, trades):
    with WormWriter(root, "hl.trades") as writer:
        writer.write(trades, now_ns())


def test_replay_is_idempotent(tmp_path):
    # The tape is a stream of changes; applying one twice moves a position
    # that never moved.
    registry = Registry(tmp_path / "r.sqlite")
    registry.seed_wallets(
        [type("R", (), {"address": "0xa", "account_value": 1.0, "vlm_week": 1.0,
                        "vlm_day": 0.0, "pnl_month": 0.0})()],
        {"0xa": "equity"},
    )
    registry.replace_positions([(_position(szi=2.0), "published")])
    root = tmp_path / "raw"
    _write_tape(root, [{"coin": "BTC", "px": "100", "sz": "1",
                        "users": ["0xa", "0xb"], "side": "A"}])

    first = replay(registry, root, marks={"BTC": 100.0})
    assert first["updated"] == 1
    assert registry.positions("BTC")[0].szi == 3.0

    second = replay(registry, root, marks={"BTC": 100.0})
    assert second["skipped"] == "no new tape"
    assert registry.positions("BTC")[0].szi == 3.0


def test_replay_ignores_addresses_outside_the_registry(tmp_path):
    registry = Registry(tmp_path / "r.sqlite")
    registry.replace_positions([(_position(), "published")])
    root = tmp_path / "raw"
    _write_tape(root, [{"coin": "BTC", "px": "100", "sz": "1",
                        "users": ["0xstranger", "0xother"], "side": "A"}])
    result = replay(registry, root, marks={"BTC": 100.0})
    assert result["deltas"] == 0
    assert registry.positions("BTC")[0].szi == 2.0


def test_replay_reports_nothing_to_do_on_an_empty_store(tmp_path):
    registry = Registry(tmp_path / "r.sqlite")
    assert replay(registry, tmp_path / "raw")["skipped"] == "no new tape"


def test_upsert_does_not_evict_wallets_that_did_not_trade(tmp_path):
    # A wallet absent from a tape batch simply did not trade; it must keep the
    # position we last observed rather than dropping off the map.
    registry = Registry(tmp_path / "r.sqlite")
    registry.replace_positions([
        (_position(address="0xa"), "published"),
        (_position(address="0xquiet"), "published"),
    ])
    registry.upsert_positions([(_position(address="0xa", szi=9.0), "derived")])
    by_address = {p.address: p for p in registry.positions("BTC")}
    assert by_address["0xa"].szi == 9.0
    assert by_address["0xquiet"].szi == 2.0
    assert registry.source_counts() == {"published": 1, "derived": 1}
