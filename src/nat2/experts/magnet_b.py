"""Stage B — once price has reached the cluster, does the sweep continue?

`HYPOTHESIS_2.md`, bound to the ledger by seq 191. Stage A tests a pull that can only be
reflexive, because a liquidation exerts nothing until price arrives. This is the other half:
after the arrival the flow is mechanical, its size is on the map, and its direction is known
before it happens.

**The sign trap, and it is a different one from Stage A's.** `fade()` returns `+1` when the
*snap-back* wins and `-1` when the sweep continues, already relative to the sweep direction.
H2 is a claim about continuation, so `y = 1` iff `fade() == -1`. The other convention gives a
perfectly plausible, exactly inverted result that no gate would catch. There is a test on it.

**The confound is momentum, not geometry.** A touch is selected on price having moved, so
"price kept going" is what trend alone predicts, and the permutation placebo cannot see it:
shuffling map mass leaves the pre-touch move untouched. Hence `ret` is mandatory and
`without_map()` gates rather than diagnoses -- if the map-blind model matches this one, the
edge was never the map's.
"""

from __future__ import annotations

import bisect

from nat2.experts.base import Dataset, Expert, NotFitted, to_matrix
from nat2.experts.magnet_a import LabelStats, barrier_pct, default_model
from nat2.features.spec import MAP
from nat2.labels.touch import Touch

MIN_TRAIN_ROWS = 200
TOUCH_COLUMNS = ("fuel", "brake", "imb_fuel", "touch_shell", "touch_sweep")


def build_dataset(events: list[Touch], rows: list[dict], paths: dict, horizon_ns: int,
                  features: list[str], bar_ns: int = 0, k: float = 1.0) -> tuple[Dataset, LabelStats]:
    """Label each touch by the sweep's outcome, and say what was dropped.

    The decision time is the **touch**, not the bar it fell in: the bar supplies volatility
    and the trailing move, and is joined as-of so nothing later than the touch can reach it.
    """
    from nat2.labels.barriers import TIMEOUT, assert_sorted, fade, sample_weights

    for path in paths.values():
        assert_sorted(path)
    ordered = sorted(rows, key=lambda r: r["t_decision"])
    times = [r["t_decision"] for r in ordered]
    stats = LabelStats(rows=len(events))
    kept, labels, t0s, results = [], [], [], []
    for touch in events:
        i = bisect.bisect_right(times, touch.t) - 1
        row = ordered[i] if i >= 0 else None
        width = barrier_pct(row, horizon_ns, bar_ns, k) if row else None
        if width is None:
            stats.no_sigma += 1
            continue
        result = fade(paths.get(touch.coin, []), touch.t, touch.px, touch.sweep, width, horizon_ns)
        if not result.resolved:
            stats.unresolved += 1
            continue
        if result.outcome == TIMEOUT:
            # The sweep neither continued nor reversed inside the horizon. That is a
            # question, not a negative answer, and counting it as one would say the
            # cluster did nothing when in fact nothing was observed.
            stats.timeouts += 1
            continue
        kept.append({**row, **touch_features(touch), "t_decision": touch.t, "coin": touch.coin})
        results.append(result)
        labels.append(1 if result.label == -1 else 0)     # -1 is continuation; see the module docstring
        t0s.append(touch.t)
    stats.labelled = len(labels)
    stats.positives = sum(1 for v in labels if v)
    return Dataset(columns=list(features), X=to_matrix(kept, features), y=labels,
                   weight=sample_weights(results, t0s), rows=kept), stats


def touch_features(touch: Touch) -> dict:
    return {"fuel": touch.fuel, "brake": touch.brake, "imb_fuel": touch.f,
            "touch_shell": float(touch.shell_index), "touch_sweep": float(touch.sweep)}


class MagnetB(Expert):
    name = "magnet_b"
    features = [
        *TOUCH_COLUMNS,
        "coverage", "published_frac", "map_age_s",
        # Trailing return is the momentum control, and here it is not a formality: the
        # sample is conditioned on a move, so trend is the leading explanation to beat.
        "ret", "sigma", "sigma_regime", "range_frac", "tau", "liq_flow",
    ]

    def __init__(self, horizon_ns: int, model_factory=None, min_rows: int = MIN_TRAIN_ROWS,
                 features: list[str] | None = None, name: str | None = None):
        from nat2.features.spec import undeclared

        self.horizon_ns = horizon_ns
        self.min_rows = min_rows
        self._model_factory = model_factory or default_model
        self._model = None
        if features is not None:
            bad = undeclared(features)
            if bad:
                raise ValueError(f"undeclared feature(s): {sorted(bad)}")
            if not features:
                raise ValueError("an expert with no features cannot be fitted")
            self.features = list(features)
        if name:
            self.name = name

    def without_map(self) -> "MagnetB":
        """Blind to the map: seq 191 criterion 2. What survives is the pre-touch move,
        volatility and realized flow -- everything the confound needs and nothing the
        hypothesis is about. If it matches the full model, H2 is refuted whatever the
        placebo says."""
        from nat2.features.spec import by_source

        map_features = by_source(MAP)
        return MagnetB(self.horizon_ns, self._model_factory, self.min_rows,
                       features=[f for f in self.features if f not in map_features],
                       name=f"{self.name}:no_map")

    def fit(self, data: Dataset) -> "MagnetB":
        if len(data) < self.min_rows:
            raise ValueError(f"{self.name}: {len(data)} labelled rows, need {self.min_rows}")
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
        return [float(row[1]) for row in self._model.predict_proba(to_matrix(rows, self.features))]

    def baseline(self) -> Expert:
        return FuelBaseline()


class FuelBaseline(Expert):
    """`sign(F)`, one column, no fitting: the nested null of seq 191 §1.6.

    Positive `F` is more mass ahead than behind, so continuation is predicted. The sign is
    *not* negated as in Stage A's `neg_imbalance`, because `F` is already relative to the
    sweep direction rather than to the mark."""

    name = "sign_fuel"
    features = ["imb_fuel"]

    def fit(self, data: Dataset) -> "FuelBaseline":
        return self

    def predict(self, rows: list[dict]) -> list[float]:
        out = []
        for row in rows:
            f = row.get("imb_fuel")
            out.append(0.5 if f is None else 0.5 * (1.0 + max(-1.0, min(1.0, float(f)))))
        return out

    def baseline(self) -> "FuelBaseline":
        return self


__all__ = ["FuelBaseline", "MagnetB", "build_dataset", "touch_features"]
