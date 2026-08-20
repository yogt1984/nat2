"""Isotonic calibration, fitted out of sample only.

A model's raw score is not a probability. Thresholding it against a
cost-derived number is only meaningful once the score has been mapped to
observed frequencies -- and that mapping has to be learned on predictions the
model did not train on, or it inherits the model's optimism and the threshold
becomes decorative.

Isotonic rather than a sigmoid because the shape of the miscalibration is not
known in advance, only that it should be monotone: a higher score should never
mean a lower observed rate.
"""

from __future__ import annotations


class Isotonic:
    """Monotone mapping from score to observed frequency."""

    def __init__(self):
        self._model = None
        self._constant: float | None = None

    def fit(self, scores: list[float], labels: list[int]) -> "Isotonic":
        if not scores or len(set(labels)) < 2:
            # Nothing to learn from: fall back to the observed base rate rather
            # than inventing a curve.
            self._constant = (sum(labels) / len(labels)) if labels else 0.5
            return self
        from sklearn.isotonic import IsotonicRegression

        self._model = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        self._model.fit(scores, labels)
        return self

    def apply(self, scores: list[float]) -> list[float]:
        if self._model is None:
            return [self._constant if self._constant is not None else 0.5] * len(scores)
        return [float(v) for v in self._model.predict(scores)]
