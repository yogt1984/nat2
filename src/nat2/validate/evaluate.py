"""Run an expert and its baseline through the same folds, and compare.

The verdict this produces is not "is the model good". It is **"does the model
beat the dumb thing"**, out of sample, net of costs — which is the only
question that decides whether an expert enters the pool.

Both are scored on identical test rows from identical folds. That matters more
than it sounds: comparing a model's fold-averaged metric against a baseline
computed on the whole sample is a standard way to manufacture an edge, because
the fold structure itself changes the mix of regimes being scored.

Costs enter as a threshold, not as an afterthought. An expert is only credited
with a decision where its calibrated probability clears the cost-derived
threshold, so an edge that exists but is smaller than the spread is scored as
what it is: nothing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from nat2.core.costs import Costs
from nat2.experts.base import Dataset, Expert
from nat2.validate.calibrate import Isotonic
from nat2.validate.wfo import Fold, coverage, folds, leaks


@dataclass
class Scored:
    name: str
    y: list[int] = field(default_factory=list)
    p: list[float] = field(default_factory=list)
    w: list[float] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.y)

    @property
    def base_rate(self) -> float:
        return _weighted_mean([float(v) for v in self.y], self.w)

    def log_loss(self) -> float:
        if not self.y:
            return float("nan")
        losses = []
        for label, prob in zip(self.y, self.p):
            q = min(max(prob, 1e-9), 1 - 1e-9)
            losses.append(-(math.log(q) if label else math.log(1 - q)))
        return _weighted_mean(losses, self.w)

    def brier(self) -> float:
        if not self.y:
            return float("nan")
        return _weighted_mean([(p - y) ** 2 for y, p in zip(self.y, self.p)], self.w)

    def decisions(self, threshold: float) -> dict:
        """Only rows where the model actually commits, scored on their outcome."""
        taken = [(y, p, w) for y, p, w in zip(self.y, self.p, self.w) if p >= threshold]
        if not taken:
            return {"n": 0, "hit_rate": None, "edge": None}
        ys = [float(y) for y, _, _ in taken]
        ws = [w for _, _, w in taken]
        hit = _weighted_mean(ys, ws)
        return {"n": len(taken), "hit_rate": hit, "edge": hit - 0.5}

    def summary(self, threshold: float) -> dict:
        return {
            "name": self.name,
            "n": len(self),
            "base_rate": self.base_rate,
            "log_loss": self.log_loss(),
            "brier": self.brier(),
            **{f"decision_{k}": v for k, v in self.decisions(threshold).items()},
        }


@dataclass
class Comparison:
    expert: Scored
    baseline: Scored
    threshold: float
    folds: dict
    costs: dict
    skipped_folds: int = 0
    leaked: int = 0

    @property
    def beats_baseline(self) -> bool:
        """Lower log loss out of sample. Ties do not count as beating."""
        if not len(self.expert) or not len(self.baseline):
            return False
        return self.expert.log_loss() < self.baseline.log_loss()

    def verdict(self) -> dict:
        return {
            "expert": self.expert.summary(self.threshold),
            "baseline": self.baseline.summary(self.threshold),
            "beats_baseline": self.beats_baseline,
            "threshold": self.threshold,
            "folds": self.folds,
            "costs": self.costs,
            "skipped_folds": self.skipped_folds,
            "leaked": self.leaked,
        }


def _weighted_mean(values: list[float], weights: list[float]) -> float:
    total = sum(weights)
    if not values:
        return float("nan")
    if total <= 0:
        return sum(values) / len(values)
    return sum(v * w for v, w in zip(values, weights)) / total


def evaluate(
    expert: Expert,
    data: Dataset,
    horizon_ns: int,
    costs: Costs,
    n_splits: int = 5,
    embargo_ns: int | None = None,
    calibrate: bool = True,
) -> Comparison:
    """Purged walk-forward, expert versus baseline, on identical test rows."""
    times = [row["t_decision"] for row in data.rows]
    embargo = horizon_ns if embargo_ns is None else embargo_ns
    splits = folds(times, n_splits, horizon_ns, embargo)

    scored_expert = Scored(expert.name)
    scored_baseline = Scored(expert.baseline().name)
    skipped = 0
    leaked = 0

    for fold in splits:
        leaked += len(leaks(fold, times, horizon_ns))
        train = _subset(data, fold.train)
        test_rows = [data.rows[i] for i in fold.test]
        test_y = [data.y[i] for i in fold.test]
        test_w = [data.weight[i] for i in fold.test]

        try:
            fitted = expert.fit(train)
        except ValueError:
            # Not enough of a fold to fit. Counted, never quietly averaged over.
            skipped += 1
            continue

        scored_expert.y += test_y
        scored_expert.p += fitted.predict(test_rows)
        scored_expert.w += test_w

        baseline = expert.baseline()
        baseline.fit(train)
        scored_baseline.y += test_y
        scored_baseline.p += baseline.predict(test_rows)
        scored_baseline.w += test_w

    if calibrate:
        # Both, or neither. Calibrating only the expert lets it win on the
        # calibration rather than on the signal -- a constant 0.5 predictor
        # beat its identical baseline this way until a test caught it.
        # Fitted on out-of-sample predictions only: calibrating on training
        # predictions would make the cost-derived threshold decorative.
        for scored in (scored_expert, scored_baseline):
            if len(scored):
                scored.p = Isotonic().fit(scored.p, scored.y).apply(scored.p)

    return Comparison(
        expert=scored_expert,
        baseline=scored_baseline,
        threshold=costs.threshold(),
        folds=coverage(splits, len(data)),
        costs=costs.describe(),
        skipped_folds=skipped,
        leaked=leaked,
    )


def _subset(data: Dataset, index: list[int]) -> Dataset:
    return Dataset(
        columns=data.columns,
        X=[data.X[i] for i in index],
        y=[data.y[i] for i in index],
        weight=[data.weight[i] for i in index],
        rows=[data.rows[i] for i in index],
    )
