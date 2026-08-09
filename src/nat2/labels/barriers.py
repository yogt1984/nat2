"""Path-dependent labels.

Two labels, both answering "what happened next" by walking the observed price
path rather than comparing endpoints.

**Stage A — the two-barrier race.** Did price reach the cluster within the
horizon *before* travelling the same distance the other way? The race matters:
"eventually touches" is a garbage label because everything eventually touches,
and a label that is almost always 1 teaches a model nothing.

**Stage B — the triple barrier.** From a cluster touch, did the sweep snap back
or extend? Up barrier, down barrier, and a vertical one at the horizon.

Three rules run through both.

*Absence of data is not an outcome.* A window with no observations returns
`None`, never 0. A confident zero manufactured from missing data is exactly the
failure the whole system is built to avoid.

*The path is authoritative and ordered.* These take a sequence of observed
prices, in observation order -- trades, not OHLC bars. A bar whose high and low
both breach cannot say which came first, and guessing there silently decides
the label for the most interesting cases: the violent ones.

*Nothing before `t0` is visible.* Observations at or before the decision time
are dropped, so a label can never be resolved by a price that had already
printed when the decision was made.

Prices are marks, because that is what a position actually experiences: HL
triggers liquidation off the mark, not the mid.
"""

from __future__ import annotations

from dataclasses import dataclass

TARGET = "target"
OPPOSITE = "opposite"
TIMEOUT = "timeout"
NO_DATA = "no_data"
INVALID = "invalid"

Path = list[tuple[int, float]]


@dataclass(frozen=True)
class BarrierResult:
    label: int | None      # None when the question could not be answered
    outcome: str
    t_end: int | None = None
    price_end: float | None = None
    span_ns: int = 0

    @property
    def resolved(self) -> bool:
        return self.label is not None

    @property
    def touched(self) -> bool:
        return self.outcome == TARGET


def _window(path: Path, t0: int, horizon_ns: int):
    """Observations strictly after t0 and no later than the vertical barrier."""
    deadline = t0 + horizon_ns
    for t, price in path:
        if t <= t0:
            continue
        if t > deadline:
            return
        yield t, price


def race(
    path: Path,
    t0: int,
    p0: float,
    target: float,
    horizon_ns: int,
) -> BarrierResult:
    """Stage A: reach `target` before travelling the same distance the other way.

    The opposite barrier is placed symmetrically about `p0`, so the label is a
    genuine race rather than a restatement of drift.
    """
    if horizon_ns <= 0 or p0 <= 0 or target <= 0 or target == p0:
        return BarrierResult(None, INVALID)

    up = target > p0
    opposite = 2 * p0 - target
    seen = False
    for t, price in _window(path, t0, horizon_ns):
        seen = True
        if (up and price >= target) or (not up and price <= target):
            return BarrierResult(1, TARGET, t, price, t - t0)
        if (up and price <= opposite) or (not up and price >= opposite):
            return BarrierResult(0, OPPOSITE, t, price, t - t0)
    if not seen:
        return BarrierResult(None, NO_DATA)
    return BarrierResult(0, TIMEOUT, t0 + horizon_ns, price, horizon_ns)


def triple_barrier(
    path: Path,
    t0: int,
    p0: float,
    up_pct: float,
    down_pct: float,
    horizon_ns: int,
) -> BarrierResult:
    """Stage B: +1 upper barrier first, -1 lower first, 0 neither within horizon.

    Barriers are fractions of `p0` and must both be positive; the caller
    chooses which side means snap-back (see `fade`).
    """
    if horizon_ns <= 0 or p0 <= 0 or up_pct <= 0 or down_pct <= 0:
        return BarrierResult(None, INVALID)

    upper = p0 * (1 + up_pct)
    lower = p0 * (1 - down_pct)
    seen = False
    for t, price in _window(path, t0, horizon_ns):
        seen = True
        if price >= upper:
            return BarrierResult(1, TARGET, t, price, t - t0)
        if price <= lower:
            return BarrierResult(-1, OPPOSITE, t, price, t - t0)
    if not seen:
        return BarrierResult(None, NO_DATA)
    return BarrierResult(0, TIMEOUT, t0 + horizon_ns, price, horizon_ns)


def fade(
    path: Path,
    t0: int,
    p0: float,
    sweep_side: int,
    barrier_pct: float,
    horizon_ns: int,
) -> BarrierResult:
    """Stage B in the terms the magnet expert asks in.

    `sweep_side` is the direction price swept to reach the cluster: +1 for a
    sweep upward (short liquidations firing), -1 downward. Snap-back means
    price reverses that direction.

    Returns +1 snap-back, -1 continuation, 0 timeout -- so the sign means the
    same thing regardless of which way the cascade ran, which is what lets one
    model learn from both.
    """
    if sweep_side not in (1, -1):
        return BarrierResult(None, INVALID)
    result = triple_barrier(path, t0, p0, barrier_pct, barrier_pct, horizon_ns)
    if result.label is None or result.label == 0:
        return result
    # A downward sweep snaps back by rising, so the raw sign flips.
    snapped = (result.label == -1) if sweep_side == 1 else (result.label == 1)
    return BarrierResult(
        1 if snapped else -1, result.outcome, result.t_end, result.price_end, result.span_ns
    )


def uniqueness(spans: list[tuple[int, int]]) -> list[float]:
    """Average uniqueness of each label's span, in (0, 1].

    Cluster episodes overlap heavily: consecutive bars around one cascade
    produce labels resolved by the same price move. Counting them as
    independent observations overstates the sample by an order of magnitude and
    makes every significance test a lie. Weighting by the reciprocal of
    concurrency is the correction.

    Time-weighted rather than bar-counted, because the bars here are irregular
    and a long quiet span should not be diluted by a short busy one.
    """
    if not spans:
        return []
    edges = sorted({edge for start, end in spans for edge in (start, end)})
    intervals = list(zip(edges, edges[1:]))
    concurrency = [
        sum(1 for start, end in spans if start <= low and end >= high)
        for low, high in intervals
    ]

    weights = []
    for start, end in spans:
        duration = end - start
        if duration <= 0:
            # Resolved instantly: it shares its span with nothing.
            weights.append(1.0)
            continue
        total = 0.0
        for (low, high), count in zip(intervals, concurrency):
            if start <= low and end >= high and count:
                total += (high - low) / count
        weights.append(total / duration)
    return weights


def sample_weights(results: list[BarrierResult], t0s: list[int]) -> list[float]:
    """Uniqueness weights for resolved labels, aligned to the input order.

    Unresolved labels get zero weight rather than being silently dropped, so
    the caller's arrays stay the same length and a mismatch cannot go unnoticed.
    """
    spans = [
        (t0, result.t_end if result.t_end is not None else t0)
        for result, t0 in zip(results, t0s)
    ]
    raw = uniqueness(spans)
    return [w if result.resolved else 0.0 for w, result in zip(raw, results)]
