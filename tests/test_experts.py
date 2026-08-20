"""The Expert protocol and Stage A, tested for the ways they could cheat.

The protocol exists to enforce two things that are easy to violate quietly: an
expert may only read declared features, and it must name the dumb thing it has
to beat. Both are checked at class-definition time rather than after a training
run.

The labelling has its own trap. Rows with no map snapshot must be dropped, not
imputed — most of the current frame has no magnet features at all, and filling
them with neutral zeros would manufacture agreement between "no reading" and
"balanced book".
"""

from __future__ import annotations

import math

import pytest

from nat2.experts.base import (
    ColumnExpert,
    ConstantExpert,
    Dataset,
    Expert,
    NotFitted,
    finite_rows,
    to_matrix,
)
from nat2.experts.magnet_a import (
    ImbalanceBaseline,
    MagnetA,
    barrier_pct,
    build_dataset,
)

MIN = 60_000_000_000


def _row(**kw) -> dict:
    base = {
        "coin": "BTC", "t_close": MIN, "t_decision": MIN, "close": 100.0,
        "imb_002": 0.5, "d_near_up_pct": 0.01, "d_near_dn_pct": -0.02,
    }
    base.update(kw)
    return base


# --- the protocol's guarantees ---------------------------------------------

def test_an_expert_reading_an_undeclared_feature_fails_at_definition():
    with pytest.raises(TypeError, match="undeclared"):
        class Sneaky(Expert):
            features = ["imb_002", "tomorrows_close"]

            def fit(self, data): return self
            def predict(self, rows): return []
            def baseline(self): return self


def test_an_expert_without_a_baseline_cannot_be_instantiated():
    class NoBaseline(Expert):
        features = ["imb_002"]

        def fit(self, data): return self
        def predict(self, rows): return []

    with pytest.raises(TypeError):
        NoBaseline()


def test_lookback_is_the_deepest_feature_the_expert_reads():
    expert = MagnetA(horizon_ns=MIN)
    assert expert.lookback() >= 30
    assert expert.describe()["baseline"] == "neg_imbalance"


# --- matrix construction ---------------------------------------------------

def test_missing_becomes_nan_never_zero():
    # The frame took care to distinguish "no map yet" from "balanced book";
    # imputing zero here would throw that away at the last possible moment.
    matrix = to_matrix([_row(imb_002=None)], ["imb_002"])
    assert math.isnan(matrix[0][0])


def test_matrix_refuses_undeclared_columns():
    with pytest.raises(ValueError, match="undeclared"):
        to_matrix([_row()], ["imb_002", "invented_column"])


def test_finite_rows_drops_rather_than_imputes():
    rows = [_row(), _row(imb_002=None), _row(close=float("nan"))]
    assert len(finite_rows(rows, ["close", "imb_002"])) == 1


# --- barrier placement: independent of the map ------------------------------

def test_the_barrier_is_sized_from_volatility_alone():
    # A barrier placed at a cluster makes its location a function of the
    # covariate, distance-to-cluster leaks into the null, and 0.5 stops being
    # the null. HYPOTHESIS_1.md §3 forbids it.
    row = _row(sigma=0.01)
    width = barrier_pct(row, horizon_ns=100 * MIN, bar_ns=MIN, k=1.0)
    assert width == pytest.approx(0.01 * 10.0)


def test_the_barrier_ignores_the_map_entirely():
    near = _row(sigma=0.01, d_near_up_pct=0.001, d_near_dn_pct=-0.5, imb_002=0.9)
    far = _row(sigma=0.01, d_near_up_pct=0.04, d_near_dn_pct=-0.002, imb_002=-0.9)
    horizon = dict(horizon_ns=100 * MIN, bar_ns=MIN, k=1.0)
    assert barrier_pct(near, **horizon) == barrier_pct(far, **horizon)


def test_the_barrier_scales_with_k_and_with_the_root_of_the_horizon():
    row = _row(sigma=0.01)
    one = barrier_pct(row, 100 * MIN, MIN, 1.0)
    assert barrier_pct(row, 100 * MIN, MIN, 2.0) == pytest.approx(2 * one)
    assert barrier_pct(row, 400 * MIN, MIN, 1.0) == pytest.approx(2 * one)


@pytest.mark.parametrize("kw", [
    {"sigma": None}, {"sigma": 0.0}, {"sigma": -0.01},
])
def test_no_volatility_means_no_barrier(kw):
    assert barrier_pct(_row(**kw), 100 * MIN, MIN, 1.0) is None


@pytest.mark.parametrize("bad", [{"bar_ns": 0}, {"horizon_ns": 0}, {"k": 0.0}])
def test_degenerate_barrier_inputs_yield_none(bad):
    args = {"horizon_ns": 100 * MIN, "bar_ns": MIN, "k": 1.0, **bad}
    assert barrier_pct(_row(sigma=0.01), **args) is None


# --- the baseline ----------------------------------------------------------

def test_the_baseline_negates_the_imbalance():
    # Mass above the mark is shorts; shorts liquidate by buying; forced buying
    # pushes price up. imbalance() is (below - above), so mass above makes it
    # negative and the probability of the UPPER barrier must rise.
    mass_above = ImbalanceBaseline().predict([_row(imb_002=-0.8)])[0]
    mass_below = ImbalanceBaseline().predict([_row(imb_002=0.8)])[0]
    assert mass_above > 0.5 > mass_below


def test_the_baseline_abstains_rather_than_guessing():
    assert ImbalanceBaseline().predict([_row(imb_002=None)]) == [0.5]


def test_the_baseline_is_its_own_baseline():
    baseline = ImbalanceBaseline()
    assert baseline.baseline() is baseline


def test_baseline_scores_stay_inside_the_unit_interval():
    scores = ImbalanceBaseline().predict([_row(imb_002=5.0), _row(imb_002=-5.0)])
    assert all(0.0 <= s <= 1.0 for s in scores)


def test_a_column_expert_rejects_an_undeclared_column():
    with pytest.raises(ValueError, match="undeclared"):
        ColumnExpert("not_a_feature")


def test_a_constant_expert_is_the_floor():
    assert ConstantExpert(0.5).predict([_row(), _row()]) == [0.5, 0.5]


# --- labelling -------------------------------------------------------------

def _path(*points):
    return {"BTC": list(points)}


def _labelled(*points, **kw):
    args = {"horizon_ns": 100 * MIN, "bar_ns": MIN, "k": 1.0, "features": ["imb_002"]}
    args.update(kw)
    rows = args.pop("rows", [_row(sigma=0.01)])
    return build_dataset(rows, _path(*points), args.pop("horizon_ns"),
                         args.pop("features"), **args)


def test_touching_the_upper_barrier_first_is_a_one():
    data, stats = _labelled((2 * MIN, 110.5))
    assert data.y == [1] and stats.labelled == 1


def test_touching_the_lower_barrier_first_is_a_zero():
    data, _stats = _labelled((2 * MIN, 89.5))
    assert data.y == [0]


def test_order_decides_which_barrier_won():
    data, _stats = _labelled((2 * MIN, 89.5), (3 * MIN, 111.0))
    assert data.y == [0], "the lower barrier was reached first"


def test_a_timeout_is_excluded_rather_than_called_a_miss():
    data, stats = _labelled((2 * MIN, 100.1))
    assert stats.timeouts == 1 and len(data) == 0


def test_timeouts_can_be_included_deliberately():
    data, stats = _labelled((2 * MIN, 100.1), include_timeouts=True)
    assert stats.timeouts == 1 and data.y == [0]


def test_a_row_without_volatility_cannot_be_labelled():
    _data, stats = _labelled((2 * MIN, 110.5), rows=[_row(sigma=None)])
    assert stats.no_sigma == 1 and stats.labelled == 0


def test_rows_whose_race_cannot_resolve_are_dropped():
    data, stats = _labelled()
    assert len(data) == 0 and stats.unresolved == 1


def test_a_wider_barrier_is_harder_to_reach():
    tight, _ = _labelled((2 * MIN, 110.5), k=1.0)
    wide, wide_stats = _labelled((2 * MIN, 110.5), k=3.0)
    assert tight.y == [1]
    assert len(wide) == 0 and wide_stats.timeouts == 1


def test_dataset_arrays_stay_aligned():
    rows = [_row(sigma=0.01, t_decision=MIN), _row(sigma=0.01, t_decision=2 * MIN)]
    data, _stats = _labelled((5 * MIN, 110.5), rows=rows, features=["imb_002", "close"])
    assert len(data.X) == len(data.y) == len(data.weight) == len(data.rows)
    assert all(len(r) == 2 for r in data.X)


def test_overlapping_labels_are_down_weighted():
    rows = [_row(sigma=0.01, t_decision=MIN), _row(sigma=0.01, t_decision=MIN)]
    data, _stats = _labelled((5 * MIN, 110.5), rows=rows)
    assert len(data) == 2 and all(w < 1.0 for w in data.weight)


def test_label_stats_report_the_positive_rate():
    _data, stats = _labelled((2 * MIN, 110.5))
    assert stats.summary()["positive_rate"] == 1.0


# --- fitting refuses rather than pretending --------------------------------

class _Stub:
    def __init__(self):
        self.fitted = False

    def fit(self, X, y, sample_weight=None):
        self.fitted = True
        self.n = len(X)
        return self

    def predict_proba(self, X):
        return [[0.4, 0.6] for _ in X]


def _dataset(n: int, mixed: bool = True) -> Dataset:
    return Dataset(
        columns=["imb_002"],
        X=[[0.5]] * n,
        y=[(i % 2 if mixed else 1) for i in range(n)],
        weight=[1.0] * n,
        rows=[_row() for _ in range(n)],
    )


def test_fitting_on_too_few_rows_refuses():
    # A model trained on a handful of overlapping cascades looks confident and
    # means nothing.
    expert = MagnetA(horizon_ns=MIN, model_factory=_Stub, min_rows=200)
    with pytest.raises(ValueError, match="need 200"):
        expert.fit(_dataset(10))


def test_fitting_on_one_class_refuses():
    expert = MagnetA(horizon_ns=MIN, model_factory=_Stub, min_rows=5)
    with pytest.raises(ValueError, match="nothing to learn"):
        expert.fit(_dataset(10, mixed=False))


def test_predicting_before_fitting_raises():
    with pytest.raises(NotFitted):
        MagnetA(horizon_ns=MIN).predict([_row()])


def test_a_fitted_expert_predicts_per_row():
    expert = MagnetA(horizon_ns=MIN, model_factory=_Stub, min_rows=5).fit(_dataset(10))
    scores = expert.predict([_row(), _row()])
    assert len(scores) == 2 and all(0.0 <= s <= 1.0 for s in scores)


def test_predicting_nothing_returns_nothing():
    expert = MagnetA(horizon_ns=MIN, model_factory=_Stub, min_rows=5).fit(_dataset(10))
    assert expert.predict([]) == []
