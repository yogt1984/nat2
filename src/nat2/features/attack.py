"""The attack ratio: is a cluster big enough and close enough to be worth pushing into?

An agent standing `d` away from a liquidation cluster can walk price into it.
Walking costs money; the cascade pays money. This module asks whether the
second exceeds the first, and asks it for every candidate distance at once.

    Psi*  =  sup_d   A * sigma * sqrt(R(d)/V)  /  (kappa*d + c)

`R(d)` is the effective mass within `d`, `A*sigma*sqrt(R/V)` the displacement
that mass produces under the same square-root impact law that already sets the
capacity cap in the sizing chain, and `kappa*d + c` the unrecovered cost of the
walk. `Psi* > 1` means some push is profitable; the maximising distance names
the cluster worth attacking.

Three properties are worth stating because they are the reason this is not a
scoring heuristic dressed as physics.

**The exponents are not free.** Mass enters at one half and distance at one,
both forced by the impact law rather than fitted. A free exponent that beats
this refutes the impact law, which is a larger finding than the study.

**The cost term is the principled floor.** A kernel `M/d^alpha` explodes as
`d -> 0` and needs an arbitrary `d_min`. Here mass sitting on the mark is not
infinitely attractive because there is no distance left to profit from, and
the floor is the round-trip cost, which is a measured quantity.

**The supremum is exact.** `R` steps up only at positions and the denominator
strictly increases, so between positions `Psi` can only fall: the maximum is
attained *at* a position. Sort, sweep a prefix sum, done -- no grid search.

Sign convention, which inverts the whole study if it moves: mass **above** the
mark is shorts, shorts liquidate by **buying**, forced buying pushes price
**up**. So mass above implies positive drift, and `mu` is the negative of
`LiqMap.imbalance()`. There is a test on this.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from nat2.features.liqmath import Position, effective

# Fraction of the displacement a push does not recover on exit. A round trip
# that moved price and came back costs the temporary component; `kappa = 1`
# would mean none of it is recovered.
DEFAULT_KAPPA = 0.5

# Cross positions are discounted because their liquidation level is not fixed:
# it moves whenever any other position in the account moves, so cross mass is a
# distribution over prices rather than a point.
DEFAULT_OMEGA_CROSS = 0.5

# Impact coefficient times the square root of the firing fraction. Not
# separately identified by price data, so carried as one constant and profiled.
DEFAULT_A = 1.0

UP = 1
DOWN = -1


@dataclass(frozen=True)
class Reach:
    """The best attack available on one side of the mark."""

    psi: float                    # the supremum
    psi_jackknife: float          # recomputed without the winning cluster's largest member
    distance: float | None        # d*, relative; None when there is no mass
    mass: float = 0.0             # effective mass within d*
    positions: int = 0            # how many contributed to it

    @property
    def viable(self) -> bool:
        """Is a push into this side profitable at all?"""
        return self.psi > 1.0

    @property
    def concentration(self) -> float:
        """How much of the reading one position is responsible for, in [0, 1].

        A supremum is brittle: one mispriced whale can set it. This says so
        directly. 0 means the cluster is a single position; near 0 means it
        survives losing its largest member and is a genuine crowd.
        """
        return 1.0 - (self.psi_jackknife / self.psi) if self.psi > 0 else 0.0

    def summary(self) -> dict:
        return {
            "psi": self.psi,
            "psi_jackknife": self.psi_jackknife,
            "concentration": self.concentration,
            "distance": self.distance,
            "mass": self.mass,
            "positions": self.positions,
            "viable": self.viable,
        }


EMPTY = Reach(psi=0.0, psi_jackknife=0.0, distance=None)


def _distances(
    positions: list[Position],
    coin: str,
    mark: float,
    side: int,
    omega_cross: float,
) -> list[tuple[float, float]]:
    """(|relative distance|, effective notional) for one side, sorted."""
    rows: list[tuple[float, float]] = []
    for position in positions:
        if position.coin != coin or position.size == 0:
            continue
        price, _ = effective(position)
        if price is None or price <= 0:
            # Observed but unplaceable. It counts toward coverage elsewhere; it
            # cannot count toward a cluster whose location is unknown.
            continue
        distance = (price - mark) / mark
        if distance * side <= 0:
            continue
        weight = omega_cross if position.margin_type == "cross" else 1.0
        rows.append((abs(distance), position.notional * weight))
    rows.sort()
    return rows


def attack_ratio(
    positions: list[Position],
    coin: str,
    mark: float,
    sigma: float | None,
    volume: float | None,
    cost: float,
    side: int,
    a: float = DEFAULT_A,
    kappa: float = DEFAULT_KAPPA,
    omega_cross: float = DEFAULT_OMEGA_CROSS,
) -> Reach | None:
    """`Psi*` and the distance attaining it, for one side of the mark.

    Returns None when the inputs required to answer are missing -- a zero here
    would read as a confident "no attack available", which is a different claim
    from "we could not see".
    """
    if not sigma or not volume or sigma <= 0 or volume <= 0 or mark <= 0:
        return None
    if cost < 0 or kappa < 0 or (kappa == 0 and cost == 0):
        # With no cost floor the ratio diverges as d -> 0 and the sup is
        # meaningless. Refuse rather than return an infinity.
        return None

    rows = _distances(positions, coin, mark, side, omega_cross)
    if not rows:
        return EMPTY

    best, best_distance, best_mass, best_count = _sweep(rows, a, sigma, volume, kappa, cost)

    # Drop the winning cluster's largest member and re-run. If the reading
    # collapses, one position was responsible for it -- a supremum is brittle
    # and this is the cheapest way to say by how much. A log-sum-exp was tried
    # here first and rejected: it grows like log(N)/tau, so it reported
    # position *count* rather than concentration and answered a different
    # question than the one asked.
    inside = [r for r in rows if r[0] <= (best_distance or 0.0)]
    jackknife = 0.0
    if len(inside) > 1:
        heaviest = max(range(len(inside)), key=lambda i: inside[i][1])
        remaining = [r for i, r in enumerate(rows) if r is not inside[heaviest]]
        jackknife = _sweep(remaining, a, sigma, volume, kappa, cost)[0]

    return Reach(
        psi=best,
        psi_jackknife=jackknife,
        distance=best_distance,
        mass=best_mass,
        positions=best_count,
    )


def _sweep(
    rows: list[tuple[float, float]],
    a: float,
    sigma: float,
    volume: float,
    kappa: float,
    cost: float,
) -> tuple[float, float | None, float, int]:
    """The supremum, by prefix sum over sorted distances.

    `R(d)` steps up only at positions and `kappa*d + c` strictly increases, so
    between positions the ratio can only fall: the maximum is attained *at* a
    position and this sweep is exact rather than a grid search.
    """
    best = 0.0
    best_distance: float | None = None
    best_mass = 0.0
    best_count = 0
    cumulative = 0.0
    for count, (distance, notional) in enumerate(rows, start=1):
        cumulative += notional
        psi = a * sigma * math.sqrt(cumulative / volume) / (kappa * distance + cost)
        if psi > best:
            best, best_distance, best_mass, best_count = psi, distance, cumulative, count
    return best, best_distance, best_mass, best_count


@dataclass(frozen=True)
class Signal:
    up: Reach
    down: Reach
    drift: float

    @property
    def abstain(self) -> bool:
        """Fuel on both sides is a volatility state, not a direction."""
        return self.up.viable and self.down.viable

    def summary(self) -> dict:
        return {
            "drift": self.drift,
            "abstain": self.abstain,
            "up": self.up.summary(),
            "down": self.down.summary(),
        }


def _hinge(x: float) -> float:
    return max(x - 1.0, 0.0)


def signal(
    positions: list[Position],
    coin: str,
    mark: float,
    sigma: float | None,
    volume: float | None,
    cost: float,
    gamma: float = 1.0,
    hinge: bool = True,
    **kw,
) -> Signal | None:
    """The drift, positive upward.

    `hinge=True` is the hidden-hand reading -- nothing happens until a push
    pays. `hinge=False` is the passive magnet, smooth and always on. The two
    hypotheses differ by this flag and by nothing else, which is what makes
    them comparable on the same sample.
    """
    up = attack_ratio(positions, coin, mark, sigma, volume, cost, UP, **kw)
    down = attack_ratio(positions, coin, mark, sigma, volume, cost, DOWN, **kw)
    if up is None or down is None:
        return None
    shape = _hinge if hinge else (lambda x: x)
    return Signal(up=up, down=down, drift=gamma * (shape(up.psi) - shape(down.psi)))


def logit_p(drift: float, sigma: float, k: float, horizon_years: float) -> float:
    """First-passage logit for symmetric barriers at +/- k*sigma*sqrt(T).

    Exact for Brownian motion with drift: P(hit +a first) = logistic(2*mu*a/sigma^2),
    and with a = k*sigma*sqrt(T) that collapses to 2*k*sqrt(T)*mu/sigma. The
    linear predictor *is* the drift rescaled, which is why logistic regression
    is the correctly specified model here rather than a convenient default.
    """
    return 2.0 * k * math.sqrt(horizon_years) * drift / sigma
