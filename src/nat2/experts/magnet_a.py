"""Stage A — does the cluster pull?

For each bar the target is the **nearer** of the two clusters the map knows
about, and the label is the two-barrier race: did price reach it within the
horizon before travelling the same distance the other way. The opposite barrier
is symmetric by construction, so a model cannot win by learning drift.

The baseline is the raw magnet imbalance oriented toward that target. It is
deliberately trivial — one declared column, no fitting — because the question
`nat2 eval` asks is not "does the model work" but "does the model beat looking
at one number". Live data already shows why that matters: `imb_002` sat at
-0.69 for BTC for long stretches, and a model that merely reproduces the sign
of that has learned nothing the map did not already say.

Rows with no map snapshot are dropped, not imputed. Map history only began when
snapshots did, so most of the current frame has no magnet features at all —
training on imputed neutrals would manufacture agreement between "no reading"
and "balanced book".
"""

from __future__ import annotations

from dataclasses import dataclass

from nat2.experts.base import ColumnExpert, Dataset, Expert, NotFitted, finite_rows, to_matrix
from nat2.labels.barriers import race

# The map's own bands stop at 5%; a target beyond that is not something the
# features describe.
MAX_TARGET_DISTANCE = 0.05
MIN_TRAIN_ROWS = 200


@dataclass(frozen=True)
class Target:
    side: int          # +1 cluster above, -1 below
    distance: float    # signed fraction from the mark
    price: float


def target_of(row: dict) -> Target | None:
    """The nearer cluster, or None if the map named neither.

    Ties go to the downside: long liquidations are the more common cascade and
    breaking the tie deterministically keeps labels reproducible.
    """
    up, down = row.get("d_near_up_pct"), row.get("d_near_dn_pct")
    close = row.get("close")
    if not close or close <= 0:
        return None
    candidates = []
    if up is not None and 0 < up <= MAX_TARGET_DISTANCE:
        candidates.append((abs(up), 1, up))
    if down is not None and -MAX_TARGET_DISTANCE <= down < 0:
        candidates.append((abs(down), -1, down))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    _, side, distance = candidates[0]
    return Target(side=side, distance=distance, price=close * (1 + distance))


def oriented_imbalance(row: dict) -> float | None:
    """Imbalance signed so that positive always means "toward the target".

    `imb` is (below - above), so it points down when positive. Orienting it by
    the target side is what makes one baseline usable for clusters on either
    side, exactly as `fade()` does for Stage B labels.
    """
    target = target_of(row)
    imb = row.get("imb_002")
    if target is None or imb is None:
        return None
    return imb * (-1 if target.side == 1 else 1)


def build_dataset(
    rows: list[dict],
    paths: dict[str, list[tuple[int, float]]],
    horizon_ns: int,
    features: list[str],
) -> Dataset:
    """Label each row by racing its own target, and drop what cannot be labelled."""
    from nat2.labels.barriers import sample_weights

    usable = finite_rows(rows, ["close", "imb_002"])
    kept, labels, t0s = [], [], []
    results = []
    for row in usable:
        target = target_of(row)
        if target is None:
            continue
        path = paths.get(row["coin"], [])
        result = race(path, row["t_decision"], row["close"], target.price, horizon_ns)
        if not result.resolved:
            # No data in the window, or a degenerate target. Unlabelled rows
            # are dropped rather than defaulted to the majority class.
            continue
        kept.append(row)
        results.append(result)
        labels.append(result.label)
        t0s.append(row["t_decision"])

    weights = sample_weights(results, t0s)
    return Dataset(
        columns=list(features),
        X=to_matrix(kept, features),
        y=labels,
        weight=weights,
        rows=kept,
    )


class MagnetA(Expert):
    name = "magnet_a"
    features = [
        "imb_0005", "imb_001", "imb_002", "imb_005", "imb_cross_002",
        "l_up_002", "l_dn_002", "d_near_up_pct", "d_near_dn_pct",
        "premium", "premium_z", "funding_z", "oi_z",
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
    """`sign(imb)` oriented toward the target. One column, no fitting."""

    name = "imb_toward_target"
    features = ["imb_002", "d_near_up_pct", "d_near_dn_pct", "close"]

    def fit(self, data: Dataset) -> "ImbalanceBaseline":
        return self

    def predict(self, rows: list[dict]) -> list[float]:
        out = []
        for row in rows:
            oriented = oriented_imbalance(row)
            out.append(0.5 if oriented is None else 0.5 * (1.0 + max(-1.0, min(1.0, oriented))))
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
    "ColumnExpert", "ImbalanceBaseline", "MagnetA", "Target",
    "build_dataset", "oriented_imbalance", "target_of",
]
