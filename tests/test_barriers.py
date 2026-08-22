"""Adversarial tests for the path-dependent labels.

A label is the target a model learns from, so a wrong one is not a bug you
notice — it is a bug that gets validated. Three properties carry most of the
weight here: nothing before the decision time may resolve a label, a window
with no data must not become a confident zero, and the race must be a race
rather than a restatement of drift.
"""

from __future__ import annotations

import pytest

from nat2.labels.barriers import (
    INVALID,
    NO_DATA,
    OPPOSITE,
    TARGET,
    TIMEOUT,
    fade,
    race,
    sample_weights,
    triple_barrier,
    uniqueness,
)

H = 1000  # horizon, in the arbitrary time units these tests use


def path(*points) -> list[tuple[int, float]]:
    return list(points)


# --- Stage A: the race -----------------------------------------------------

def test_target_reached_first_is_a_hit():
    result = race(path((10, 100.0), (20, 105.0)), t0=0, p0=100.0, target=105.0, horizon_ns=H)
    assert result.label == 1 and result.outcome == TARGET
    assert result.t_end == 20 and result.span_ns == 20


def test_opposite_reached_first_is_a_miss():
    result = race(path((10, 95.0)), t0=0, p0=100.0, target=105.0, horizon_ns=H)
    assert result.label == 0 and result.outcome == OPPOSITE


def test_the_opposite_barrier_is_symmetric_about_the_start():
    # target +5 => opposite -5. At 95.01 nothing has happened yet.
    assert race(path((10, 95.01)), 0, 100.0, 105.0, H).outcome == TIMEOUT
    assert race(path((10, 95.0)), 0, 100.0, 105.0, H).outcome == OPPOSITE


def test_a_downward_target_races_the_same_way():
    result = race(path((10, 95.0)), t0=0, p0=100.0, target=95.0, horizon_ns=H)
    assert result.label == 1 and result.outcome == TARGET
    assert race(path((10, 105.0)), 0, 100.0, 95.0, H).outcome == OPPOSITE


def test_order_decides_not_extremes():
    # Touches the opposite barrier first, then the target. A label that
    # compared endpoints would call this a hit; the race calls it a miss.
    result = race(path((10, 95.0), (20, 106.0)), 0, 100.0, 105.0, H)
    assert result.label == 0 and result.outcome == OPPOSITE


def test_neither_barrier_within_the_horizon_is_a_timeout_not_a_hit():
    result = race(path((10, 101.0), (20, 99.0)), 0, 100.0, 105.0, H)
    assert result.label == 0 and result.outcome == TIMEOUT
    assert result.span_ns == H


def test_a_touch_after_the_horizon_does_not_count():
    # "Eventually touches" is the garbage label this design exists to avoid.
    result = race(path((H + 1, 200.0)), 0, 100.0, 105.0, H)
    assert result.outcome == NO_DATA


def test_a_touch_exactly_on_the_horizon_counts():
    assert race(path((H, 105.0)), 0, 100.0, 105.0, H).outcome == TARGET


# --- the lookahead guard ---------------------------------------------------

def test_prices_before_the_decision_time_cannot_resolve_a_label():
    # The 105 printed before t0: it was already history when the decision was
    # made, and must not be allowed to resolve the label.
    result = race(path((5, 105.0), (50, 101.0)), t0=10, p0=100.0, target=105.0, horizon_ns=H)
    assert result.outcome == TIMEOUT


def test_a_price_exactly_at_the_decision_time_is_also_excluded():
    result = race(path((10, 105.0)), t0=10, p0=100.0, target=105.0, horizon_ns=H)
    assert result.outcome == NO_DATA


# --- absence of data -------------------------------------------------------

def test_an_empty_path_is_unresolved_not_zero():
    result = race([], 0, 100.0, 105.0, H)
    assert result.label is None and result.outcome == NO_DATA
    assert not result.resolved


def test_a_window_with_no_observations_is_unresolved():
    result = race(path((5000, 105.0)), 0, 100.0, 105.0, H)
    assert result.label is None


@pytest.mark.parametrize("kwargs", [
    {"horizon_ns": 0}, {"horizon_ns": -1}, {"p0": 0.0}, {"p0": -1.0},
    {"target": 0.0}, {"target": 100.0},
])
def test_degenerate_inputs_are_invalid_not_guessed(kwargs):
    base = {"t0": 0, "p0": 100.0, "target": 105.0, "horizon_ns": H}
    base.update(kwargs)
    result = race(path((10, 105.0)), **base)
    assert result.label is None and result.outcome == INVALID


# --- Stage B: the triple barrier -------------------------------------------

def test_upper_barrier_first():
    result = triple_barrier(path((10, 102.0)), 0, 100.0, 0.01, 0.01, H)
    assert result.label == 1


def test_lower_barrier_first():
    result = triple_barrier(path((10, 98.0)), 0, 100.0, 0.01, 0.01, H)
    assert result.label == -1


def test_triple_barrier_timeout_is_a_real_zero():
    result = triple_barrier(path((10, 100.5)), 0, 100.0, 0.01, 0.01, H)
    assert result.label == 0 and result.outcome == TIMEOUT


def test_asymmetric_barriers_are_respected():
    # Up 5%, down 1%: a 2% fall hits the lower barrier, a 2% rise does not.
    assert triple_barrier(path((10, 98.0)), 0, 100.0, 0.05, 0.01, H).label == -1
    assert triple_barrier(path((10, 102.0)), 0, 100.0, 0.05, 0.01, H).label == 0


def test_triple_barrier_with_no_data_is_unresolved():
    assert triple_barrier([], 0, 100.0, 0.01, 0.01, H).label is None


@pytest.mark.parametrize("up,down", [(0.0, 0.01), (0.01, 0.0), (-0.01, 0.01)])
def test_non_positive_barriers_are_invalid(up, down):
    assert triple_barrier(path((10, 102.0)), 0, 100.0, up, down, H).outcome == INVALID


# --- the fade wrapper: sign means the same thing both ways ------------------

def test_an_upward_sweep_snaps_back_by_falling():
    result = fade(path((10, 98.0)), 0, 100.0, sweep_side=1, barrier_pct=0.01, horizon_ns=H)
    assert result.label == 1, "price fell after an upward sweep: the fade won"


def test_an_upward_sweep_continues_by_rising():
    result = fade(path((10, 102.0)), 0, 100.0, sweep_side=1, barrier_pct=0.01, horizon_ns=H)
    assert result.label == -1


def test_a_downward_sweep_snaps_back_by_rising():
    result = fade(path((10, 102.0)), 0, 100.0, sweep_side=-1, barrier_pct=0.01, horizon_ns=H)
    assert result.label == 1


def test_a_downward_sweep_continues_by_falling():
    result = fade(path((10, 98.0)), 0, 100.0, sweep_side=-1, barrier_pct=0.01, horizon_ns=H)
    assert result.label == -1


def test_fade_timeout_keeps_its_zero():
    assert fade(path((10, 100.1)), 0, 100.0, 1, 0.01, H).label == 0


@pytest.mark.parametrize("side", [0, 2, -2, None])
def test_an_unknown_sweep_side_is_invalid(side):
    assert fade(path((10, 98.0)), 0, 100.0, side, 0.01, H).outcome == INVALID


# --- uniqueness weights ----------------------------------------------------

def test_a_lone_label_is_fully_unique():
    assert uniqueness([(0, 100)]) == [1.0]


def test_two_identical_spans_each_count_a_half():
    # The same price move resolved both. Counting them as two independent
    # observations doubles the apparent sample and halves every p-value.
    assert uniqueness([(0, 100), (0, 100)]) == [0.5, 0.5]


def test_disjoint_spans_stay_unique():
    assert uniqueness([(0, 10), (20, 30)]) == [1.0, 1.0]


def test_partial_overlap_is_weighted_by_time_not_by_count():
    # A overlaps B for half its life, so A averages (0.5*1 + 0.5*0.5) = 0.75.
    weights = uniqueness([(0, 100), (50, 150)])
    assert weights[0] == pytest.approx(0.75)
    assert weights[1] == pytest.approx(0.75)


def test_a_heavily_overlapped_cluster_is_heavily_discounted():
    weights = uniqueness([(0, 100)] * 10)
    assert all(w == pytest.approx(0.1) for w in weights)


def test_an_instantly_resolved_span_is_unique():
    assert uniqueness([(50, 50), (0, 100)])[0] == 1.0


def test_uniqueness_of_nothing_is_nothing():
    assert uniqueness([]) == []


def test_sample_weights_zero_out_unresolved_labels():
    # Kept in place rather than dropped, so the caller's arrays cannot silently
    # fall out of alignment.
    results = [
        race(path((10, 105.0)), 0, 100.0, 105.0, H),
        race([], 0, 100.0, 105.0, H),
    ]
    weights = sample_weights(results, [0, 0])
    assert len(weights) == 2
    assert weights[0] > 0 and weights[1] == 0.0


# --- the bisect window is the linear window -------------------------------------

def _linear_window(path, t0, horizon_ns):
    deadline = t0 + horizon_ns
    for t, price in path:
        if t <= t0:
            continue
        if t > deadline:
            return
        yield t, price


def test_bisect_window_equals_the_linear_walk_on_random_sorted_paths():
    import random
    from nat2.labels.barriers import _window, assert_sorted
    rng = random.Random(3)
    for _ in range(200):
        ts = sorted(rng.randint(0, 500) for _ in range(rng.randint(0, 40)))      # ties included
        p = [(t, 100.0 + rng.random()) for t in ts]
        t0, h = rng.randint(-10, 510), rng.randint(0, 200)
        assert list(_window(p, t0, h)) == list(_linear_window(p, t0, h)), (p, t0, h)
    assert assert_sorted([(1, 1.0), (1, 2.0), (2, 3.0)]) == [(1, 1.0), (1, 2.0), (2, 3.0)]
    with pytest.raises(ValueError, match="sorted"):
        assert_sorted([(2, 1.0), (1, 2.0)])

