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
    build_dataset,
    oriented_imbalance,
    target_of,
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
    assert expert.describe()["baseline"] == "imb_toward_target"


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


# --- target selection ------------------------------------------------------

def test_the_nearer_cluster_is_the_target():
    assert target_of(_row(d_near_up_pct=0.01, d_near_dn_pct=-0.02)).side == 1
    assert target_of(_row(d_near_up_pct=0.03, d_near_dn_pct=-0.005)).side == -1


def test_a_tie_resolves_downward_and_stays_reproducible():
    target = target_of(_row(d_near_up_pct=0.01, d_near_dn_pct=-0.01))
    assert target.side == -1
    assert target_of(_row(d_near_up_pct=0.01, d_near_dn_pct=-0.01)).side == -1


def test_a_cluster_beyond_the_mapped_bands_is_not_a_target():
    # The map's bands stop at 5%; beyond that the features do not describe it.
    assert target_of(_row(d_near_up_pct=0.9, d_near_dn_pct=None)) is None


def test_no_cluster_means_no_target():
    assert target_of(_row(d_near_up_pct=None, d_near_dn_pct=None)) is None


def test_target_price_is_on_the_right_side_of_the_mark():
    up = target_of(_row(d_near_up_pct=0.01, d_near_dn_pct=None))
    down = target_of(_row(d_near_up_pct=None, d_near_dn_pct=-0.01))
    assert up.price > 100.0 > down.price


def test_a_row_without_a_price_has_no_target():
    assert target_of(_row(close=0.0)) is None


# --- the baseline ----------------------------------------------------------

def test_imbalance_is_oriented_toward_the_target():
    # imb is (below - above), so positive points down. A downside target with
    # positive imb means the magnet favours it.
    down_target = _row(d_near_up_pct=0.03, d_near_dn_pct=-0.01, imb_002=0.8)
    up_target = _row(d_near_up_pct=0.01, d_near_dn_pct=-0.03, imb_002=0.8)
    assert oriented_imbalance(down_target) == pytest.approx(0.8)
    assert oriented_imbalance(up_target) == pytest.approx(-0.8)


def test_the_baseline_abstains_rather_than_guessing():
    scores = ImbalanceBaseline().predict([_row(imb_002=None)])
    assert scores == [0.5]


def test_the_baseline_is_its_own_baseline():
    baseline = ImbalanceBaseline()
    assert baseline.baseline() is baseline


def test_baseline_scores_stay_inside_the_unit_interval():
    scores = ImbalanceBaseline().predict([
        _row(imb_002=5.0, d_near_dn_pct=-0.01, d_near_up_pct=0.03),
        _row(imb_002=-5.0, d_near_dn_pct=-0.01, d_near_up_pct=0.03),
    ])
    assert all(0.0 <= s <= 1.0 for s in scores)


def test_a_column_expert_rejects_an_undeclared_column():
    with pytest.raises(ValueError, match="undeclared"):
        ColumnExpert("not_a_feature")


def test_a_constant_expert_is_the_floor():
    assert ConstantExpert(0.5).predict([_row(), _row()]) == [0.5, 0.5]


# --- labelling -------------------------------------------------------------

def _path(*points):
    return {"BTC": list(points)}


def test_rows_whose_race_cannot_resolve_are_dropped():
    # No prices in the window: unlabelled, and dropped rather than defaulted to
    # the majority class.
    data, stats = build_dataset([_row()], _path(), horizon_ns=MIN, features=["imb_002"])
    assert len(data) == 0 and stats.unresolved == 1


def test_a_timeout_is_excluded_rather_than_called_a_miss():
    # The race never finished. Counting it as 0 is what collapsed the first
    # live run to a 0.7% positive rate.
    rows = [_row(d_near_up_pct=0.01, d_near_dn_pct=None)]
    data, stats = build_dataset(rows, _path((2 * MIN, 100.05)), 10 * MIN, ["imb_002"])
    assert stats.timeouts == 1 and len(data) == 0


def test_timeouts_can_be_included_deliberately():
    rows = [_row(d_near_up_pct=0.01, d_near_dn_pct=None)]
    data, stats = build_dataset(rows, _path((2 * MIN, 100.05)), 10 * MIN, ["imb_002"],
                                include_timeouts=True)
    assert stats.timeouts == 1 and data.y == [0]


def test_an_unreachable_target_is_not_raced():
    # A cluster five sigma away is decided by the clock, not the magnet.
    rows = [_row(d_near_up_pct=0.05, d_near_dn_pct=None, sigma=0.0001)]
    _data, stats = build_dataset(rows, _path((2 * MIN, 101.0)), 10 * MIN, ["imb_002"],
                                 bar_ns=MIN, max_reach_sigma=1.0)
    assert stats.unreachable == 1


def test_a_reachable_target_is_raced():
    rows = [_row(d_near_up_pct=0.001, d_near_dn_pct=None, sigma=0.01)]
    _data, stats = build_dataset(rows, _path((2 * MIN, 100.2)), 10 * MIN, ["imb_002"],
                                 bar_ns=MIN, max_reach_sigma=1.0)
    assert stats.unreachable == 0 and stats.labelled == 1


def test_reach_gating_is_off_unless_asked_for():
    rows = [_row(d_near_up_pct=0.05, d_near_dn_pct=None, sigma=0.0001)]
    _data, stats = build_dataset(rows, _path((2 * MIN, 101.0)), 10 * MIN, ["imb_002"])
    assert stats.unreachable == 0


def test_label_stats_report_the_positive_rate():
    rows = [_row(d_near_up_pct=0.01, d_near_dn_pct=None)]
    _data, stats = build_dataset(rows, _path((2 * MIN, 101.5)), 10 * MIN, ["imb_002"])
    assert stats.summary()["positive_rate"] == 1.0


def test_rows_without_a_map_are_dropped_not_imputed():
    rows = [_row(imb_002=None, d_near_up_pct=None, d_near_dn_pct=None)]
    data, _stats = build_dataset(rows, _path((2 * MIN, 101.0)), MIN, ["imb_002"])
    assert len(data) == 0


def test_a_reached_target_is_labelled_one():
    rows = [_row(d_near_up_pct=0.01, d_near_dn_pct=None)]
    data, _stats = build_dataset(rows, _path((MIN + 1, 101.5)), 10 * MIN, ["imb_002"])
    assert data.y == [1]
    assert len(data.X) == 1 and len(data.weight) == 1


def test_the_opposite_barrier_is_labelled_zero():
    rows = [_row(d_near_up_pct=0.01, d_near_dn_pct=None)]
    data, _stats = build_dataset(rows, _path((MIN + 1, 98.0)), 10 * MIN, ["imb_002"])
    assert data.y == [0]


def test_dataset_arrays_stay_aligned():
    rows = [
        _row(t_decision=MIN, d_near_up_pct=0.01, d_near_dn_pct=None),
        _row(t_decision=2 * MIN, d_near_up_pct=0.01, d_near_dn_pct=None),
    ]
    data, _stats = build_dataset(rows, _path((3 * MIN, 101.5)), 10 * MIN, ["imb_002", "close"])
    assert len(data.X) == len(data.y) == len(data.weight) == len(data.rows)
    assert all(len(r) == 2 for r in data.X)


def test_overlapping_labels_are_down_weighted():
    # Two rows resolved by the same price move are not two observations.
    rows = [
        _row(t_decision=MIN, d_near_up_pct=0.01, d_near_dn_pct=None),
        _row(t_decision=MIN, d_near_up_pct=0.01, d_near_dn_pct=None),
    ]
    data, _stats = build_dataset(rows, _path((3 * MIN, 101.5)), 10 * MIN, ["imb_002"])
    assert len(data) == 2
    assert all(w < 1.0 for w in data.weight)


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
