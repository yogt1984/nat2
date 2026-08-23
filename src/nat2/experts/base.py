"""The Expert protocol, and the baseline it is not allowed to skip.

`baseline()` is mandatory, and it is the point of the protocol rather than a
convenience. GBDT-versus-`sign(imb)` and clone-versus-raw-flow are the same
rule — *does the machinery earn its keep* — so it is written once here and
enforced for every expert that follows. An expert that cannot beat its own
declared baseline out of sample, net of costs, does not enter the pool.

Two other rules live here because they are easy to violate quietly.

**Features must be declared.** An expert may only read columns registered in
`spec.FEATURES`; anything else has no recorded lookback, which means the
walk-forward embargo computed from those lookbacks would be too short.

**Missing stays missing.** Absent values become NaN, never zero. The frame took
care to distinguish "no map snapshot yet" from "a balanced book"; imputing
zeros at the matrix boundary would throw that away at the last possible moment
and teach the model that unknown looks like neutral.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from nat2.features.spec import FEATURES, undeclared


class NotFitted(RuntimeError):
    pass


@dataclass
class Dataset:
    """Aligned design matrix, labels and sample weights."""

    columns: list[str]
    X: list[list[float]] = field(default_factory=list)
    y: list[int] = field(default_factory=list)
    weight: list[float] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.y)

    @property
    def positive_rate(self) -> float:
        return (sum(1 for v in self.y if v > 0) / len(self.y)) if self.y else 0.0

    def select(self, columns: list[str]) -> "Dataset":
        """The same labels, weights and rows over a subset of columns.

        An ablation is a *different design matrix*, not just a different feature list: the
        evaluator fits on `X`, so handing a six-feature expert a fourteen-column matrix fails
        at predict time -- which is what happens to any `without_map()` used without this.
        """
        return Dataset(list(columns), to_matrix(self.rows, list(columns)), self.y, self.weight, self.rows)

    def with_rows(self, rows: list[dict]) -> "Dataset":
        """The same labels and weights over re-featured rows; row i must still be decision i."""
        if len(rows) != len(self.rows):
            raise ValueError(f"{len(rows)} rows for {len(self.rows)} labels")
        return Dataset(self.columns, to_matrix(rows, self.columns), self.y, self.weight, rows)


def to_matrix(rows: list[dict], columns: list[str]) -> list[list[float]]:
    """Rows to a dense matrix, with `None` becoming NaN rather than zero."""
    bad = undeclared(columns)
    if bad:
        raise ValueError(f"undeclared feature(s): {sorted(bad)}")
    matrix = []
    for row in rows:
        matrix.append([
            float("nan") if row.get(c) is None else float(row[c]) for c in columns
        ])
    return matrix


def finite_rows(rows: list[dict], required: list[str]) -> list[dict]:
    """Rows where every `required` column is present and finite.

    Used to drop rows that predate a stream rather than impute them. Dropping
    is honest and costs sample size; imputing is free and corrupts the label.
    """
    out = []
    for row in rows:
        values = [row.get(c) for c in required]
        if any(v is None or (isinstance(v, float) and not math.isfinite(v)) for v in values):
            continue
        out.append(row)
    return out


class Expert(ABC):
    """One opinion, with the dumb thing it must beat."""

    name: str = "expert"
    horizon_ns: int = 0
    features: list[str] = []

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # Caught at import rather than after a training run.
        bad = undeclared(cls.features or [])
        if bad:
            raise TypeError(f"{cls.__name__} reads undeclared feature(s): {sorted(bad)}")

    @abstractmethod
    def fit(self, data: Dataset) -> "Expert":
        ...

    @abstractmethod
    def predict(self, rows: list[dict]) -> list[float]:
        """Raw scores in [0, 1]. **Not calibrated** -- thresholds mean nothing
        until `validate.calibrate` has fitted a mapping on out-of-sample folds,
        so comparing these to a cost-derived threshold directly is a mistake."""

    @abstractmethod
    def baseline(self) -> "Expert":
        """The dumb alternative this expert has to beat."""

    def lookback(self) -> int:
        return max((FEATURES[c].lookback for c in self.features), default=0)

    def describe(self) -> dict:
        return {
            "name": self.name,
            "features": list(self.features),
            "lookback": self.lookback(),
            "baseline": self.baseline().name,
        }


class ConstantExpert(Expert):
    """Predicts one number, forever. The floor any expert must clear."""

    name = "constant"
    features: list[str] = []

    def __init__(self, value: float = 0.5):
        self.value = value

    def fit(self, data: Dataset) -> "ConstantExpert":
        return self

    def predict(self, rows: list[dict]) -> list[float]:
        return [self.value] * len(rows)

    def baseline(self) -> "ConstantExpert":
        return self


class ColumnExpert(Expert):
    """Reads one declared column and maps it to a score.

    This is what a "dumb baseline" looks like in practice: no fitting, no
    parameters, one feature. If the gradient-boosted version cannot beat it out
    of sample net of costs, the extra machinery is not earning anything.
    """

    def __init__(self, column: str, name: str | None = None, scale: float = 1.0):
        if undeclared([column]):
            raise ValueError(f"undeclared feature: {column}")
        self.column = column
        self.scale = scale
        self.name = name or f"raw:{column}"
        self.features = [column]

    def fit(self, data: Dataset) -> "ColumnExpert":
        return self

    def predict(self, rows: list[dict]) -> list[float]:
        out = []
        for row in rows:
            value = row.get(self.column)
            if value is None:
                # No reading is not a neutral reading, but a baseline has to
                # answer something; 0.5 is the honest abstention.
                out.append(0.5)
                continue
            out.append(_squash(float(value) * self.scale))
        return out

    def baseline(self) -> "ColumnExpert":
        return self


def _squash(value: float) -> float:
    """Map a signed score into (0, 1) without pretending it is a probability."""
    if not math.isfinite(value):
        return 0.5
    return 0.5 * (1.0 + max(-1.0, min(1.0, value)))
