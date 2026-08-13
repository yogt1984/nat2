"""Stage A — does mass asymmetry move first-passage probability off 0.5?

**Barriers are placed at `±k·sigma·sqrt(T)`, without reference to the map.**
An earlier version raced price to the nearest cluster, which made the barrier
location a function of the covariate: distance-to-cluster leaked into the null,
the null stopped being 0.5, and the result was manufactured before any model
was fitted. `HYPOTHESIS_1.md` §3 forbids that construction, and this module now
follows it — the map is a covariate and only a covariate.

The null is therefore free. Under a driftless walk with symmetric absorbing
barriers, the probability of touching either side first is exactly 0.5, so the
whole hypothesis reduces to one question: does mass asymmetry move that number?

The baseline is `-imbalance`. Mass above the mark is short positions, shorts
liquidate by buying, and forced buying pushes price up — so mass above implies
upward drift, and `imbalance()` is defined `(below - above)`. Getting this
backwards inverts every result while leaving it plausible-looking, and no gate
would catch it.

Rows with no map snapshot are dropped, not imputed: training on imputed
neutrals would manufacture agreement between "no reading" and "balanced book".
"""

from __future__ import annotations

from dataclasses import dataclass

from nat2.experts.base import ColumnExpert, Dataset, Expert, NotFitted, finite_rows, to_matrix
from nat2.labels.barriers import race

MIN_TRAIN_ROWS = 200


@dataclass
class LabelStats:
    """Why rows did not become training examples."""

    rows: int = 0
    no_sigma: int = 0        # cannot size a barrier without a volatility estimate
    unresolved: int = 0      # no prices in the window
    timeouts: int = 0        # neither barrier hit; a question, not an answer
    labelled: int = 0
    positives: int = 0

    @property
    def positive_rate(self) -> float:
        return self.positives / self.labelled if self.labelled else 0.0

    def summary(self) -> dict:
        return {
            "rows": self.rows, "no_sigma": self.no_sigma,
            "unresolved": self.unresolved, "timeouts": self.timeouts,
            "labelled": self.labelled, "positive_rate": self.positive_rate,
        }


def barrier_pct(row: dict, horizon_ns: int, bar_ns: int, k: float) -> float | None:
    """Half-width of the symmetric barrier pair, `k · sigma · sqrt(T)`.

    Derived from volatility alone. Nothing about the map enters, which is the
    entire point: a barrier placed at a cluster is a barrier whose location is
    a function of the covariate, and the null stops being 0.5.
    """
    sigma = row.get("sigma")
    if not sigma or sigma <= 0 or bar_ns <= 0 or horizon_ns <= 0 or k <= 0:
        return None
    bars = max(1.0, horizon_ns / bar_ns)
    return k * sigma * (bars ** 0.5)


def build_dataset(
    rows: list[dict],
    paths: dict[str, list[tuple[int, float]]],
    horizon_ns: int,
    features: list[str],
    bar_ns: int = 0,
    k: float = 1.0,
    include_timeouts: bool = False,
) -> tuple[Dataset, LabelStats]:
    """Label each row by a symmetric race, and say what was dropped.

    `y = 1` when the **upper** barrier is touched first. The sign matters and
    is easy to invert: mass above the mark is short positions, shorts liquidate
    by buying, and forced buying pushes price up. So mass above implies upward
    drift, and the mandated baseline is `-imbalance` rather than `imbalance`.

    A timeout is excluded by default. The race never finished, so it is not a
    negative answer, and counting it as one collapsed an earlier version of
    this label to a 0.7% positive rate.
    """
    from nat2.labels.barriers import TIMEOUT, sample_weights, triple_barrier

    stats = LabelStats(rows=len(rows))
    usable = finite_rows(rows, ["close", "sigma"])
    kept, labels, t0s, results = [], [], [], []
    for row in usable:
        width = barrier_pct(row, horizon_ns, bar_ns, k)
        if width is None:
            stats.no_sigma += 1
            continue
        result = triple_barrier(
            paths.get(row["coin"], []), row["t_decision"], row["close"],
            width, width, horizon_ns,
        )
        if not result.resolved:
            stats.unresolved += 1
            continue
        if result.outcome == TIMEOUT:
            stats.timeouts += 1
            if not include_timeouts:
                continue
        kept.append(row)
        results.append(result)
        # triple_barrier gives +1 upper, -1 lower, 0 timeout.
        labels.append(1 if result.label == 1 else 0)
        t0s.append(row["t_decision"])

    stats.no_sigma += len(rows) - len(usable)
    stats.labelled = len(labels)
    stats.positives = sum(1 for v in labels if v)
    return Dataset(
        columns=list(features),
        X=to_matrix(kept, features),
        y=labels,
        weight=sample_weights(results, t0s),
        rows=kept,
    ), stats


class MagnetA(Expert):
    name = "magnet_a"
    features = [
        "imb_0005", "imb_001", "imb_002", "imb_005", "imb_cross_002",
        "l_up_002", "l_dn_002", "d_near_up_pct", "d_near_dn_pct",
        "premium", "premium_z", "funding_z", "oi_z",
        # Trailing return is the momentum control, non-negotiable per
        # CONSISTENCY.md §7: a learned function that is secretly
        # trend-following has to be exposed as such rather than credited.
        "ret",
        "sigma", "sigma_regime", "range_frac", "tau", "liq_flow",
        "coverage", "published_frac", "map_age_s",
    ]

    def __init__(self, horizon_ns: int, model_factory=None, min_rows: int = MIN_TRAIN_ROWS):
        self.horizon_ns = horizon_ns
        self.min_rows = min_rows
        self._model_factory = model_factory or default_model
        self._model = None

    def fit(self, data: Dataset) -> "MagnetA":
        if len(data) < self.min_rows:
            # Refusing beats returning a model trained on a handful of
            # overlapping cascades, which would look confident and mean nothing.
            raise ValueError(
                f"{self.name}: {len(data)} labelled rows, need {self.min_rows}"
            )
        if len(set(data.y)) < 2:
            raise ValueError(f"{self.name}: labels are all {data.y[0]}; nothing to learn")
        model = self._model_factory()
        model.fit(data.X, data.y, sample_weight=data.weight)
        self._model = model
        return self

    def predict(self, rows: list[dict]) -> list[float]:
        if self._model is None:
            raise NotFitted(f"{self.name} has not been fitted")
        if not rows:
            return []
        matrix = to_matrix(rows, self.features)
        # Iterate rather than slice, so the expert does not depend on the
        # model returning a numpy array. The protocol is "something with
        # predict_proba", not "sklearn".
        return [float(row[1]) for row in self._model.predict_proba(matrix)]

    def baseline(self) -> Expert:
        return ImbalanceBaseline()


class ImbalanceBaseline(Expert):
    """The mandated baseline: `-imbalance`, one column, no fitting.

    Negated because `imbalance()` is `(below - above)` while mass above drives
    price up. This is the `alpha = 0` special case of the kernel family, so the
    comparison against it is nested rather than a contest between different
    objects.
    """

    name = "neg_imbalance"
    features = ["imb_002"]

    def fit(self, data: Dataset) -> "ImbalanceBaseline":
        return self

    def predict(self, rows: list[dict]) -> list[float]:
        out = []
        for row in rows:
            imb = row.get("imb_002")
            # No reading is not a neutral reading, but a baseline must answer
            # something; 0.5 is the honest abstention.
            out.append(0.5 if imb is None else 0.5 * (1.0 - max(-1.0, min(1.0, float(imb)))))
        return out

    def baseline(self) -> "ImbalanceBaseline":
        return self


def default_model():
    """Shallow GBDT. Depth 3 because HL's history is short and punishes variance."""
    import lightgbm as lgb

    return lgb.LGBMClassifier(
        max_depth=3, num_leaves=8, n_estimators=200, learning_rate=0.05,
        min_child_samples=40, subsample=0.8, colsample_bytree=0.8,
        verbose=-1, random_state=0,
    )


__all__ = [
    "ColumnExpert", "ImbalanceBaseline", "LabelStats", "MagnetA",
    "barrier_pct", "build_dataset",
]
