"""The attack ratio: its invariants, and the sign convention that inverts the study."""

from __future__ import annotations

import math

import pytest

from nat2.features.attack import (
    DOWN,
    UP,
    attack_ratio,
    logit_p,
    signal,
)
from nat2.features.liqmath import Position

MARK = 100.0
SIGMA = 0.02
VOLUME = 1_000_000.0
COST = 0.001


def _position(liq_px: float, notional: float, margin_type: str = "isolated") -> Position:
    """A position whose published liquidation price and notional are what we choose."""
    return Position(
        address="0xa",
        coin="BTC",
        szi=notional / MARK,
        mark=MARK,
        max_leverage=40,
        margin_type=margin_type,
        account_value=0.0,
        maint_margin=0.0,
        liquidation_px=liq_px,
    )


def _ratio(positions, side=UP, **kw):
    return attack_ratio(positions, "BTC", MARK, SIGMA, VOLUME, COST, side, **kw)


# --- refusal --------------------------------------------------------------


def test_missing_inputs_refuse_rather_than_return_zero():
    # A zero would read as "no attack available", which is a different claim
    # from "we could not see".
    positions = [_position(105.0, 50_000.0)]
    assert attack_ratio(positions, "BTC", MARK, None, VOLUME, COST, UP) is None
    assert attack_ratio(positions, "BTC", MARK, SIGMA, None, COST, UP) is None
    assert attack_ratio(positions, "BTC", MARK, SIGMA, 0.0, COST, UP) is None


def test_no_cost_floor_is_refused_not_infinite():
    positions = [_position(105.0, 50_000.0)]
    assert _ratio(positions, kappa=0.0) is not None       # cost still floors it
    assert attack_ratio(positions, "BTC", MARK, SIGMA, VOLUME, 0.0, UP, kappa=0.0) is None


def test_no_mass_on_a_side_is_a_real_zero():
    reach = _ratio([_position(95.0, 50_000.0)], side=UP)
    assert reach.psi == 0.0
    assert reach.distance is None
    assert not reach.viable


def test_unplaceable_positions_are_skipped():
    # No published price and no account state to derive one from.
    blind = Position(
        address="0xb", coin="BTC", szi=1.0, mark=MARK, max_leverage=0,
        margin_type="cross", account_value=0.0, maint_margin=0.0,
    )
    assert _ratio([blind]).psi == 0.0


def test_other_coins_do_not_contribute():
    other = _position(105.0, 50_000.0)
    other = Position(**{**other.__dict__, "coin": "ETH"})
    assert _ratio([other]).psi == 0.0


# --- the supremum ---------------------------------------------------------


def test_supremum_is_attained_at_a_position():
    # R steps up only at positions and the denominator strictly increases, so
    # the maximum cannot fall between them. This is what makes the sweep exact.
    prices = [101.0, 103.0, 107.5, 112.0]
    positions = [_position(p, 40_000.0) for p in prices]
    reach = _ratio(positions)
    distances = {(p - MARK) / MARK for p in prices}
    assert reach.distance == pytest.approx(min(distances, key=lambda d: abs(d - reach.distance)))
    assert any(math.isclose(reach.distance, d) for d in distances)


# --- brittleness of a supremum -------------------------------------------


def test_a_lone_position_is_fully_concentrated():
    reach = _ratio([_position(105.0, 500_000.0)])
    assert reach.psi_jackknife == 0.0
    assert reach.concentration == pytest.approx(1.0)


def test_one_whale_among_minnows_is_reported_as_concentrated():
    positions = [_position(105.0, 5_000_000.0)] + [
        _position(104.0 + i * 0.1, 1_000.0) for i in range(8)
    ]
    assert _ratio(positions).concentration > 0.7


def test_a_genuine_crowd_survives_losing_its_largest():
    positions = [_position(104.0 + i * 0.1, 100_000.0) for i in range(10)]
    assert _ratio(positions).concentration < 0.2


def test_jackknife_never_exceeds_the_supremum():
    positions = [_position(p, 40_000.0) for p in (101.0, 103.0, 107.5, 112.0)]
    reach = _ratio(positions)
    assert reach.psi_jackknife <= reach.psi
    assert 0.0 <= reach.concentration <= 1.0


# --- scaling --------------------------------------------------------------


def test_psi_scales_as_the_square_root_of_mass():
    # The impact law is square-root, so four times the mass is twice the
    # displacement. If this ever changes, the exponent stopped being forced.
    one = _ratio([_position(105.0, 100_000.0)]).psi
    four = _ratio([_position(105.0, 400_000.0)]).psi
    assert four == pytest.approx(2.0 * one, rel=1e-9)


def test_psi_falls_with_distance():
    near = _ratio([_position(102.0, 100_000.0)]).psi
    far = _ratio([_position(110.0, 100_000.0)]).psi
    assert near > far


def test_psi_rises_with_mass():
    small = _ratio([_position(105.0, 100_000.0)]).psi
    large = _ratio([_position(105.0, 300_000.0)]).psi
    assert large > small


def test_bigger_and_closer_beats_bigger_or_closer():
    close_small = _ratio([_position(101.0, 50_000.0)]).psi
    far_large = _ratio([_position(115.0, 50_000.0 * 4)]).psi
    both = _ratio([_position(101.0, 50_000.0 * 4)]).psi
    assert both > close_small and both > far_large


def test_thin_book_makes_the_same_mass_more_dangerous():
    thick = attack_ratio([_position(105.0, 100_000.0)], "BTC", MARK, SIGMA, VOLUME, COST, UP)
    thin = attack_ratio([_position(105.0, 100_000.0)], "BTC", MARK, SIGMA, VOLUME / 4, COST, UP)
    assert thin.psi == pytest.approx(2.0 * thick.psi, rel=1e-9)


# --- cross vs isolated ----------------------------------------------------


def test_cross_mass_is_discounted():
    isolated = _ratio([_position(105.0, 100_000.0, "isolated")]).psi
    cross = _ratio([_position(105.0, 100_000.0, "cross")]).psi
    assert cross < isolated


def test_zero_omega_ignores_cross_entirely():
    assert _ratio([_position(105.0, 100_000.0, "cross")], omega_cross=0.0).psi == 0.0


# --- symmetry and sign ----------------------------------------------------


def test_reflection_symmetry():
    up = _ratio([_position(105.0, 100_000.0)], side=UP)
    down = _ratio([_position(95.0, 100_000.0)], side=DOWN)
    assert up.psi == pytest.approx(down.psi, rel=1e-9)


def test_distance_is_signed_by_side():
    # An unsigned magnitude reads as a real number pointing the wrong way: a
    # cluster 3% *below* the mark rendering as +3% is exactly the answer that
    # looks right and is not.
    up = _ratio([_position(105.0, 100_000.0)], side=UP)
    down = _ratio([_position(95.0, 100_000.0)], side=DOWN)
    assert up.distance == pytest.approx(0.05)
    assert down.distance == pytest.approx(-0.05)


def test_mass_above_the_mark_drives_price_up():
    # THE sign trap. Mass above is shorts; shorts liquidate by buying; forced
    # buying pushes price up. `LiqMap.imbalance()` has the opposite sign, and
    # confusing the two inverts every result while looking entirely plausible.
    #
    # Tested unhinged so the convention is isolated from the threshold: a
    # sub-threshold cluster is correctly silent under the hidden-hand reading,
    # which would let a sign error pass unnoticed.
    kw = dict(hinge=False)
    above = signal([_position(105.0, 500_000.0)], "BTC", MARK, SIGMA, VOLUME, COST, **kw)
    below = signal([_position(95.0, 500_000.0)], "BTC", MARK, SIGMA, VOLUME, COST, **kw)
    assert above.drift > 0
    assert below.drift < 0
    assert above.drift == pytest.approx(-below.drift, rel=1e-9)


def test_sign_survives_the_hinge():
    above = signal([_position(101.0, 5_000_000.0)], "BTC", MARK, SIGMA, VOLUME, COST)
    below = signal([_position(99.0, 5_000_000.0)], "BTC", MARK, SIGMA, VOLUME, COST)
    assert above.up.viable and above.drift > 0
    assert below.down.viable and below.drift < 0


def test_balanced_mass_gives_no_direction():
    # Unhinged, so this is genuine cancellation rather than two zeros.
    both = signal(
        [_position(105.0, 500_000.0), _position(95.0, 500_000.0)],
        "BTC", MARK, SIGMA, VOLUME, COST, hinge=False,
    )
    assert both.up.psi > 0 and both.down.psi > 0
    assert both.drift == pytest.approx(0.0, abs=1e-12)


# --- the hinge, which is the hypothesis ----------------------------------


def test_hinge_is_silent_below_the_threshold():
    # Nothing worth pushing into: the hidden hand does nothing, while the
    # passive magnet still leans. The two readings differ by this flag alone.
    tiny = [_position(115.0, 100.0)]
    hidden = signal(tiny, "BTC", MARK, SIGMA, VOLUME, COST, hinge=True)
    passive = signal(tiny, "BTC", MARK, SIGMA, VOLUME, COST, hinge=False)
    assert hidden.up.psi < 1.0
    assert hidden.drift == 0.0
    assert passive.drift > 0.0


def test_hinge_fires_once_the_push_pays():
    big = [_position(101.0, 5_000_000.0)]
    result = signal(big, "BTC", MARK, SIGMA, VOLUME, COST, hinge=True)
    assert result.up.viable
    assert result.drift > 0


def test_fuel_on_both_sides_abstains():
    both = signal(
        [_position(101.0, 5_000_000.0), _position(99.0, 5_000_000.0)],
        "BTC", MARK, SIGMA, VOLUME, COST,
    )
    assert both.up.viable and both.down.viable
    assert both.abstain


def test_gamma_scales_the_drift():
    positions = [_position(101.0, 5_000_000.0)]
    one = signal(positions, "BTC", MARK, SIGMA, VOLUME, COST, gamma=1.0)
    two = signal(positions, "BTC", MARK, SIGMA, VOLUME, COST, gamma=2.0)
    assert two.drift == pytest.approx(2.0 * one.drift, rel=1e-9)


def test_signal_refuses_when_the_ratio_does():
    assert signal([_position(105.0, 1.0)], "BTC", MARK, None, VOLUME, COST) is None


# --- the first-passage link ----------------------------------------------


def test_zero_drift_is_a_coin_flip():
    assert logit_p(0.0, SIGMA, k=1.0, horizon_years=1.0) == 0.0


def test_logit_scales_with_barrier_and_horizon():
    # logit p = 2*k*sqrt(T)*mu/sigma. The cross-cell constraint in
    # CONSISTENCY.md rests on exactly this scaling.
    base = logit_p(0.01, SIGMA, k=1.0, horizon_years=1.0)
    assert logit_p(0.01, SIGMA, k=2.0, horizon_years=1.0) == pytest.approx(2 * base)
    assert logit_p(0.01, SIGMA, k=1.0, horizon_years=4.0) == pytest.approx(2 * base)
    assert logit_p(0.01, 2 * SIGMA, k=1.0, horizon_years=1.0) == pytest.approx(base / 2)


def test_upward_drift_favours_the_upper_barrier():
    p = 1 / (1 + math.exp(-logit_p(0.05, SIGMA, k=1.0, horizon_years=1.0)))
    assert p > 0.5
