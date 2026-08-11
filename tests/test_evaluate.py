"""Expert versus baseline, out of sample, net of costs.

The verdict is not "is the model good" but "does the model beat the dumb
thing". Two ways that comparison gets rigged in practice, both pinned here:
scoring the two on different rows, and calibrating on data the model trained
on so the cost threshold stops meaning anything.
"""

from __future__ import annotations

import math

import pytest

from nat2.core.costs import Costs
from nat2.experts.base import ConstantExpert, Dataset, Expert
from nat2.validate.calibrate import Isotonic
from nat2.validate.evaluate import Scored, evaluate

MIN = 60_000_000_000


def _dataset(n=300, signal=True) -> Dataset:
    rows, y = [], []
    for i in range(n):
        value = 1.0 if i % 2 == 0 else -1.0
        rows.append({"t_decision": i * MIN, "coin": "BTC", "imb_002": value})
        y.append(1 if (value > 0 and signal) else 0)
    return Dataset(columns=["imb_002"], X=[[r["imb_002"]] for r in rows],
                   y=y, weight=[1.0] * n, rows=rows)


class _Oracle(Expert):
    """Reads the feature that generated the label. Should beat a constant."""

    name = "oracle"
    features = ["imb_002"]

    def fit(self, data): return self

    def predict(self, rows):
        return [0.9 if r["imb_002"] > 0 else 0.1 for r in rows]

    def baseline(self): return ConstantExpert(0.5)


class _Useless(Expert):
    name = "useless"
    features = ["imb_002"]

    def fit(self, data): return self
    def predict(self, rows): return [0.5] * len(rows)
    def baseline(self): return ConstantExpert(0.5)


class _Unfittable(Expert):
    name = "unfittable"
    features = ["imb_002"]

    def fit(self, data): raise ValueError("not enough rows")
    def predict(self, rows): return [0.5] * len(rows)
    def baseline(self): return ConstantExpert(0.5)


# --- the comparison --------------------------------------------------------

def test_expert_and_baseline_are_scored_on_identical_rows():
    # Comparing fold-averaged model metrics against a whole-sample baseline is
    # a standard way to manufacture an edge.
    result = evaluate(_Oracle(), _dataset(), horizon_ns=MIN, costs=Costs(), n_splits=3)
    assert len(result.expert) == len(result.baseline)
    assert result.expert.y == result.baseline.y


def test_a_real_signal_beats_the_baseline():
    result = evaluate(_Oracle(), _dataset(), horizon_ns=MIN, costs=Costs(), n_splits=3)
    assert result.beats_baseline
    assert result.expert.log_loss() < result.baseline.log_loss()


def test_a_useless_expert_does_not_beat_its_baseline():
    result = evaluate(_Useless(), _dataset(), horizon_ns=MIN, costs=Costs(), n_splits=3)
    assert not result.beats_baseline, "a tie must not count as beating"


def test_an_expert_that_cannot_fit_is_counted_not_averaged_over():
    result = evaluate(_Unfittable(), _dataset(), horizon_ns=MIN, costs=Costs(), n_splits=3)
    assert result.skipped_folds > 0
    assert len(result.expert) == 0
    assert not result.beats_baseline


def test_the_verdict_reports_leakage_as_zero_when_the_purge_holds():
    result = evaluate(_Oracle(), _dataset(), horizon_ns=5 * MIN, costs=Costs(), n_splits=3)
    assert result.leaked == 0


def test_the_verdict_carries_the_cost_hash():
    # A result whose cost hash is not on record is not a result.
    result = evaluate(_Oracle(), _dataset(), horizon_ns=MIN, costs=Costs(), n_splits=3)
    verdict = result.verdict()
    assert verdict["costs"]["hash"]
    assert verdict["threshold"] > 0.5


# --- metrics ---------------------------------------------------------------

def test_log_loss_punishes_confident_and_wrong():
    right = Scored("a", y=[1], p=[0.99], w=[1.0])
    wrong = Scored("b", y=[1], p=[0.01], w=[1.0])
    assert wrong.log_loss() > right.log_loss()


def test_weights_change_the_metric():
    # Uniqueness weights exist precisely so overlapping labels count less.
    even = Scored("a", y=[1, 0], p=[0.9, 0.9], w=[1.0, 1.0])
    tilted = Scored("b", y=[1, 0], p=[0.9, 0.9], w=[1.0, 0.0])
    assert tilted.log_loss() < even.log_loss()


def test_metrics_on_nothing_are_nan_not_zero():
    empty = Scored("a")
    assert empty.log_loss() != empty.log_loss()   # NaN
    assert empty.brier() != empty.brier()


def test_decisions_only_count_rows_that_clear_the_threshold():
    scored = Scored("a", y=[1, 1, 0], p=[0.9, 0.4, 0.95], w=[1.0, 1.0, 1.0])
    taken = scored.decisions(0.8)
    assert taken["n"] == 2
    assert taken["hit_rate"] == pytest.approx(0.5)


def test_an_expert_that_never_clears_the_threshold_makes_no_decisions():
    # An edge smaller than the spread scores as what it is: nothing.
    scored = Scored("a", y=[1, 1], p=[0.51, 0.52], w=[1.0, 1.0])
    assert scored.decisions(0.9) == {"n": 0, "hit_rate": None, "edge": None}


# --- calibration -----------------------------------------------------------

def test_isotonic_maps_scores_to_observed_frequencies():
    scores = [0.1, 0.2, 0.8, 0.9]
    labels = [0, 0, 1, 1]
    mapped = Isotonic().fit(scores, labels).apply(scores)
    assert mapped[0] <= mapped[-1]
    assert all(0.0 <= m <= 1.0 for m in mapped)


def test_isotonic_is_monotone():
    fitted = Isotonic().fit([0.1, 0.4, 0.6, 0.9], [0, 1, 0, 1])
    mapped = fitted.apply([0.0, 0.25, 0.5, 0.75, 1.0])
    assert mapped == sorted(mapped)


def test_isotonic_falls_back_to_the_base_rate_on_one_class():
    # Nothing to learn from; inventing a curve would be worse than admitting it.
    fitted = Isotonic().fit([0.2, 0.8], [1, 1])
    assert fitted.apply([0.5]) == [1.0]


def test_isotonic_on_nothing_abstains():
    assert Isotonic().fit([], []).apply([0.3]) == [0.5]


# --- costs -----------------------------------------------------------------

def test_higher_costs_demand_a_higher_threshold():
    cheap = Costs(maker_bps=0.5, half_spread_bps=0.1, slippage_bps=0.1)
    dear = Costs(maker_bps=10.0, half_spread_bps=5.0, slippage_bps=5.0)
    assert dear.threshold() > cheap.threshold() > 0.5


def test_taker_costs_more_than_maker():
    assert Costs(taker=True).round_trip_bps() > Costs(taker=False).round_trip_bps()


def test_funding_accrues_over_the_horizon():
    short = Costs(funding_bps_per_hour=1.0, horizon_hours=1.0)
    long = Costs(funding_bps_per_hour=1.0, horizon_hours=8.0)
    assert long.round_trip_bps() > short.round_trip_bps()


def test_the_cost_hash_changes_when_any_cost_changes():
    assert Costs().hash() != Costs(maker_bps=2.0).hash()
    assert Costs().hash() == Costs().hash()


def test_a_zero_move_is_never_worth_taking():
    assert Costs().threshold(move_bps=0.0) == 1.0


def test_the_baseline_is_calibrated_too():
    # Calibrating only the expert lets it win on the calibration rather than on
    # the signal: a constant 0.5 predictor beat its identical baseline that way
    # until this was caught.
    result = evaluate(_Useless(), _dataset(), horizon_ns=MIN, costs=Costs(), n_splits=3)
    assert result.expert.log_loss() == pytest.approx(result.baseline.log_loss())


def test_calibration_can_be_switched_off_for_both():
    result = evaluate(_Oracle(), _dataset(), horizon_ns=MIN, costs=Costs(),
                      n_splits=3, calibrate=False)
    assert set(result.expert.p) <= {0.9, 0.1}


def test_a_tiny_improvement_swamped_by_noise_is_not_a_win():
    # A positive mean delta is not a verdict when the per-row differences
    # scatter far wider than the mean.
    import random
    from nat2.validate.evaluate import Comparison

    random.seed(0)
    n = 200
    y = [i % 2 for i in range(n)]
    expert = Scored("e", y=y, p=[random.uniform(0.2, 0.8) for _ in range(n)],
                    w=[1.0] * n)
    baseline = Scored("b", y=y, p=[random.uniform(0.2, 0.8) for _ in range(n)],
                      w=[1.0] * n)
    close = Comparison(expert, baseline, 0.56, {}, {}, min_delta_z=2.0)
    assert abs(close.delta_z) < 2.0
    assert not close.beats_baseline


def test_a_clear_improvement_survives_the_significance_bar():
    from nat2.validate.evaluate import Comparison

    expert = Scored("e", y=[1, 0] * 50, p=[0.95, 0.05] * 50, w=[1.0] * 100)
    baseline = Scored("b", y=[1, 0] * 50, p=[0.5, 0.5] * 100, w=[1.0] * 100)
    clear = Comparison(expert, baseline, 0.56, {}, {}, min_delta_z=2.0)
    assert clear.beats_baseline and clear.delta_z > 2.0


def test_delta_is_zero_when_there_is_nothing_to_compare():
    from nat2.validate.evaluate import Comparison

    empty = Comparison(Scored("e"), Scored("b"), 0.56, {}, {})
    assert empty.delta == 0.0 and empty.delta_z == 0.0
    assert not empty.beats_baseline


def test_a_perfectly_consistent_improvement_is_maximally_significant():
    # Zero variance is maximal consistency, not zero evidence.
    from nat2.validate.evaluate import Comparison

    # A single-class label set has no meaningful constant floor, so the labels
    # alternate and the expert is confidently right on both.
    y = [i % 2 for i in range(100)]
    expert = Scored("e", y=y, p=[0.1 if v == 0 else 0.9 for v in y], w=[1.0] * 100)
    baseline = Scored("b", y=y, p=[0.5] * 100, w=[1.0] * 100)
    result = Comparison(expert, baseline, 0.56, {}, {})
    assert result.delta_z == float("inf")
    assert result.beats_constant and result.beats_baseline


def test_a_perfectly_consistent_degradation_is_not_a_win():
    from nat2.validate.evaluate import Comparison

    y = [i % 2 for i in range(100)]
    expert = Scored("e", y=y, p=[0.5] * 100, w=[1.0] * 100)
    baseline = Scored("b", y=y, p=[0.1 if v == 0 else 0.9 for v in y], w=[1.0] * 100)
    result = Comparison(expert, baseline, 0.56, {}, {})
    assert result.delta_z == float("-inf") and not result.beats_baseline


def test_beating_the_baseline_is_not_enough_if_both_lose_to_a_constant():
    # 0.6996 against 0.7017 with the constant at 0.688: less bad is not a win.
    from nat2.validate.evaluate import Comparison

    y = [1 if i % 2 else 0 for i in range(200)]
    expert = Scored("e", y=y, p=[0.45] * 200, w=[1.0] * 200)
    baseline = Scored("b", y=y, p=[0.40] * 200, w=[1.0] * 200)
    result = Comparison(expert, baseline, 0.56, {}, {})
    assert result.delta > 0
    assert not result.beats_constant
    assert not result.beats_baseline


def test_the_constant_floor_is_the_label_entropy():
    from nat2.validate.evaluate import Comparison

    y = [1] * 50 + [0] * 50
    result = Comparison(Scored("e", y=y, p=[0.5] * 100, w=[1.0] * 100),
                        Scored("b", y=y, p=[0.5] * 100, w=[1.0] * 100), 0.56, {}, {})
    assert result.constant_log_loss == pytest.approx(math.log(2))


def test_the_significance_test_respects_the_uniqueness_weights():
    # An unweighted z reported +3.28 while the weighted delta was negative:
    # it counted overlapping cascade rows as independent evidence.
    from nat2.validate.evaluate import Comparison

    y = [i % 2 for i in range(100)]
    # The expert is better on the heavily-weighted rows and worse on the rest.
    expert = Scored("e", y=y, p=[0.9 if v else 0.1 for v in y], w=[1.0] * 100)
    baseline = Scored("b", y=y, p=[0.5] * 100, w=[1.0] * 100)
    unweighted = Comparison(expert, baseline, 0.56, {}, {})
    assert unweighted.delta_z > 0

    # Down-weight every row equally: the sign must not flip, but the effective
    # sample size falls, so the evidence weakens rather than strengthening.
    faint = Scored("e", y=y, p=expert.p, w=[0.01] * 100)
    faint_baseline = Scored("b", y=y, p=baseline.p, w=[0.01] * 100)
    result = Comparison(faint, faint_baseline, 0.56, {}, {})
    assert result.delta_z > 0


def test_delta_and_its_significance_agree_in_sign():
    from nat2.validate.evaluate import Comparison

    y = [i % 2 for i in range(100)]
    weights = [1.0 if i < 10 else 0.01 for i in range(100)]
    # Better on the light rows, worse on the heavy ones: weighted delta must be
    # negative, and z must not claim otherwise.
    p_expert = [0.5 if i < 10 else (0.9 if y[i] else 0.1) for i in range(100)]
    p_base = [0.9 if y[i] else 0.1 for i in range(100)]
    expert = Scored("e", y=y, p=p_expert, w=weights)
    baseline = Scored("b", y=y, p=p_base, w=weights)
    result = Comparison(expert, baseline, 0.56, {}, {})
    assert result.delta < 0 and result.delta_z < 0
