"""Does the liquidated population overlap the mapped one?

This measurement decides whether per-position scoring is viable at all, so
its arithmetic is worth being suspicious of: wallet counts and notional
weights answer different questions, and conflating them would either condemn
a workable registry or bless an unworkable one.
"""

from __future__ import annotations

import pytest

from nat2.features.liquidations import LiquidationEvent, population_overlap


def _event(user="0xv", coin="BTC", px=100.0, sz=1.0, tid=1) -> LiquidationEvent:
    return LiquidationEvent(
        tid=tid, t_event=1, coin=coin, liquidated_user=user, mark_px=px,
        method="market", px=px, sz=sz, observer="0xo", source="counterparty",
    )


def test_empty_input_is_all_zero_and_does_not_divide_by_zero():
    overlap = population_overlap([], set(), set())
    assert overlap.events == 0 and overlap.wallets == 0
    assert overlap.wallet_frac == 0.0 and overlap.notional_frac == 0.0
    assert overlap.mapped_notional_frac == 0.0


def test_fully_covered_population():
    events = [_event(user="0xa", tid=1), _event(user="0xb", tid=2)]
    overlap = population_overlap(events, {"0xa", "0xb"}, {("0xa", "BTC"), ("0xb", "BTC")})
    assert overlap.wallet_frac == 1.0 and overlap.notional_frac == 1.0
    assert overlap.mapped_wallet_frac == 1.0


def test_fully_uncovered_population():
    overlap = population_overlap([_event(user="0xstranger")], {"0xother"}, set())
    assert overlap.wallets == 1 and overlap.wallets_in_registry == 0
    assert overlap.wallet_frac == 0.0 and overlap.notional_frac == 0.0


def test_repeat_liquidations_count_once_per_wallet_but_every_event():
    events = [_event(user="0xa", tid=i) for i in range(5)]
    overlap = population_overlap(events, {"0xa"}, set())
    assert overlap.events == 5 and overlap.wallets == 1
    assert overlap.wallet_frac == 1.0


def test_wallet_count_and_notional_can_disagree_sharply():
    # One large registry wallet against ninety-nine tiny strangers: by wallet
    # the registry looks useless, by notional it covers most of what moved.
    events = [_event(user="0xwhale", px=1000.0, sz=100.0, tid=0)]
    events += [_event(user=f"0x{i}", px=1.0, sz=1.0, tid=i + 1) for i in range(99)]
    overlap = population_overlap(events, {"0xwhale"}, {("0xwhale", "BTC")})
    assert overlap.wallet_frac == pytest.approx(0.01)
    assert overlap.notional_frac == pytest.approx(100_000 / 100_099, rel=1e-6)
    assert overlap.mapped_notional_frac > 0.99


def test_registry_membership_does_not_imply_a_mapped_position():
    # In the registry but holding nothing we could price: the map made no
    # claim about this wallet, so it must not be credited as mapped.
    overlap = population_overlap([_event(user="0xa")], {"0xa"}, set())
    assert overlap.wallet_frac == 1.0
    assert overlap.mapped_wallet_frac == 0.0
    assert overlap.notional_frac == 1.0 and overlap.mapped_notional_frac == 0.0


def test_mapping_is_per_coin_not_per_wallet():
    # We mapped this wallet's ETH position; it was liquidated in BTC.
    overlap = population_overlap([_event(user="0xa", coin="BTC")], {"0xa"}, {("0xa", "ETH")})
    assert overlap.mapped_wallet_frac == 0.0


def test_wallet_is_credited_if_any_of_its_events_is_mapped():
    events = [_event(user="0xa", coin="BTC", tid=1), _event(user="0xa", coin="DOGE", tid=2)]
    overlap = population_overlap(events, {"0xa"}, {("0xa", "BTC")})
    assert overlap.wallets == 1 and overlap.wallets_mapped == 1
    # Notional is not all-or-nothing: only the mapped event's notional counts.
    assert overlap.mapped_notional_frac == pytest.approx(0.5)


def test_zero_notional_events_do_not_break_the_ratio():
    overlap = population_overlap([_event(px=0.0, sz=0.0)], {"0xv"}, set())
    assert overlap.notional == 0.0 and overlap.notional_frac == 0.0
    assert overlap.wallet_frac == 1.0


def test_summary_exposes_both_views():
    overlap = population_overlap([_event(user="0xa")], {"0xa"}, {("0xa", "BTC")})
    summary = overlap.summary()
    assert set(summary) == {
        "events", "wallets", "wallet_frac", "mapped_wallet_frac",
        "notional", "notional_frac", "mapped_notional_frac",
    }
