"""The alpha-kernel expert: HYPOTHESIS_1 §4's distance kernel, shell form.

Pre-registered as ledger seq 153 (`magnet_alpha_kernel`, TASK_2/TASKS/09)
**before** this file existed, and nothing here departs from that text. The
persisted map carries four cumulative bands per side, not buckets, so the
kernel acts on shells `(B_{i-1}, B_i]` at their midpoints, on raw notional,
with no separate cross term -- both deviations from §4 are stated in the entry.

There is nothing to fit. `alpha` is the only parameter and it is a grid point,
not a choice: `alpha = 0` is the shipped `ImbalanceBaseline` by construction
(equal weights collapse to the 5% band imbalance), so criterion 2 -- "some
alpha > 0 beats alpha = 0" -- is a nested comparison inside one family.

Missing stays missing: a row with no map or no sigma abstains at 0.5, exactly
as the baseline does, so neither side is credited for rows nobody could read.
"""

from __future__ import annotations

import math

from nat2.core.clock import NS
from nat2.experts.base import Dataset, Expert
from nat2.experts.magnet_a import ImbalanceBaseline
from nat2.features.spec import SHELL_KEYS

SHELL_MID = (0.0025, 0.0075, 0.015, 0.035)   # (B_{i-1} + B_i) / 2, fraction of mark
D_MIN = 0.25                                  # §4 floor; pre-registered, does not move
UP = tuple(f"m_up_{s}" for s in SHELL_KEYS)
DOWN = tuple(f"m_dn_{s}" for s in SHELL_KEYS)


def shell_distances(sigma: float, horizon_ns: int, bar_ns: int) -> list[float]:
    """Shell midpoints in horizon-scaled volatility units, floored at `D_MIN`."""
    bars = max(1.0, horizon_ns / bar_ns)
    return [max(D_MIN, c / (sigma * math.sqrt(bars))) for c in SHELL_MID]


def asymmetry(below: list[float], above: list[float], distances: list[float], alpha: float) -> float:
    """`A_alpha`: positive means more kernel-weighted mass below, a predicted pull down."""
    weights = [d ** -alpha for d in distances]
    b = sum(m * w for m, w in zip(below, weights))
    a = sum(m * w for m, w in zip(above, weights))
    return (b - a) / (b + a) if (b + a) > 0 else 0.0


class MagnetAlpha(Expert):
    features = [*UP, *DOWN, "sigma"]

    def __init__(self, alpha: float, horizon_ns: int, bar_ns: int = 60 * NS):
        if alpha < 0:
            raise ValueError("alpha must be >= 0")
        self.alpha, self.horizon_ns, self.bar_ns = float(alpha), horizon_ns, bar_ns
        self.name = f"magnet_alpha:{self.alpha:g}"

    def fit(self, data: Dataset) -> "MagnetAlpha":
        return self

    def score(self, row: dict) -> float | None:
        """`A_alpha` for one row; `None` when the row cannot be read."""
        sigma = row.get("sigma")
        below = [row.get(c) for c in DOWN]
        above = [row.get(c) for c in UP]
        if not sigma or sigma <= 0 or any(v is None for v in below + above):
            return None
        return asymmetry(below, above, shell_distances(sigma, self.horizon_ns, self.bar_ns), self.alpha)

    def predict(self, rows: list[dict]) -> list[float]:
        out = []
        for row in rows:
            a = self.score(row)
            # Same map as ImbalanceBaseline.predict: p(up first) = 0.5 * (1 - A).
            out.append(0.5 if a is None else 0.5 * (1.0 - max(-1.0, min(1.0, a))))
        return out

    def baseline(self) -> Expert:
        return ImbalanceBaseline()


__all__ = ["D_MIN", "MagnetAlpha", "SHELL_MID", "asymmetry", "shell_distances"]
