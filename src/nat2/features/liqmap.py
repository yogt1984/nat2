"""The liquidation map, and the coverage number that qualifies it.

One map, built from registry positions. No estimated map, no assumed leverage
mix: a position we do not observe is simply absent, and the coverage number
says how much is absent rather than filling the hole with an assumption.

**Coverage denominator.** HL reports `openInterest` per coin. Under the
standard one-sided convention that counts each contract once, so the total
position notional held venue-wide is *twice* OI notional -- every long has a
matching short, and a registry sees both. The conservative reading is the
default, and it halves the headline number relative to the naive one, so the
convention is printed on every card and listed as verify-before-coding. Being
wrong here is a factor of two on the one number the map is judged by.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from nat2.features.liqmath import Position, effective

# Contracts counted once in HL's `openInterest`, so venue-wide position
# notional is 2x OI notional. VERIFY against HL docs; flip to 1.0 if HL
# already reports both sides.
OI_SIDES = 2.0

DEFAULT_BANDS = (0.005, 0.01, 0.02, 0.05)
# The wide band set of the v2 snapshot (TASK_2/17). At a one-week horizon and 3%/day
# volatility, sigma*sqrt(T) is ~8% -- the whole +-5% of DEFAULT_BANDS sits inside one
# barrier width, so a long-horizon question cannot be asked of a map that narrow. The
# first four bands are DEFAULT_BANDS unchanged, so v1 remains a prefix of v2.
WIDE_BANDS = (0.005, 0.01, 0.02, 0.05, 0.10, 0.20, 0.30)


@dataclass
class Bucket:
    low: float
    high: float
    notional: float = 0.0
    cross_notional: float = 0.0
    positions: int = 0


@dataclass
class LiqMap:
    coin: str
    mark: float
    buckets: list[Bucket]
    up: dict[float, float] = field(default_factory=dict)      # band -> notional above
    down: dict[float, float] = field(default_factory=dict)    # band -> notional below
    # Cross-margin only. A cross position's liquidation price moves whenever
    # any other position in that account moves, so cross mass is the
    # cascade-prone subset and deserves its own imbalance.
    up_cross: dict[float, float] = field(default_factory=dict)
    down_cross: dict[float, float] = field(default_factory=dict)
    total_notional: float = 0.0
    cross_notional: float = 0.0
    published_notional: float = 0.0
    derived_notional: float = 0.0
    oi_notional: float = 0.0
    positions: int = 0
    skipped: int = 0
    outside_span: int = 0     # priced, but further from the mark than we looked

    @property
    def coverage(self) -> float:
        """Registry position notional as a fraction of venue-wide position notional."""
        denominator = self.oi_notional * OI_SIDES
        return self.total_notional / denominator if denominator else 0.0

    @property
    def published_frac(self) -> float:
        return self.published_notional / self.total_notional if self.total_notional else 0.0

    def imbalance(self, band: float) -> float:
        """(below - above) / (below + above): the magnet imbalance."""
        up, down = self.up.get(band, 0.0), self.down.get(band, 0.0)
        total = up + down
        return (down - up) / total if total else 0.0

    def imbalance_cross(self, band: float) -> float:
        up, down = self.up_cross.get(band, 0.0), self.down_cross.get(band, 0.0)
        total = up + down
        return (down - up) / total if total else 0.0

    def summary(self) -> dict:
        return {
            "coin": self.coin,
            "mark": self.mark,
            "coverage": self.coverage,
            "oi_sides": OI_SIDES,
            "positions": self.positions,
            "skipped": self.skipped,
            "outside_span": self.outside_span,
            "published_frac": self.published_frac,
            "notional": self.total_notional,
            "imb": {str(b): self.imbalance(b) for b in self.up},
        }


def nearest(liqmap: "LiqMap", min_notional: float = 0.0) -> dict:
    """Closest bucket above and below the mark carrying real mass.

    `d_near` in the feature list is a distance to a *cluster*, not to the
    nearest stray position, so a threshold is part of the definition rather
    than a tuning knob applied afterwards.
    """
    up_price = up_notional = down_price = down_notional = None
    for bucket in liqmap.buckets:
        if bucket.notional <= min_notional:
            continue
        mid = (bucket.low + bucket.high) / 2
        if mid >= liqmap.mark and up_price is None:
            up_price, up_notional = mid, bucket.notional
        elif mid < liqmap.mark:
            down_price, down_notional = mid, bucket.notional
    return {
        "up_px": up_price,
        "up_notional": up_notional,
        "up_dist": (up_price / liqmap.mark - 1) if up_price else None,
        "down_px": down_price,
        "down_notional": down_notional,
        "down_dist": (down_price / liqmap.mark - 1) if down_price else None,
    }


def sparse_buckets(liqmap: "LiqMap") -> list[list[float]]:
    """Non-empty buckets as `[lo_pct, notional, cross_notional, positions]`.

    `lo_pct` is the bucket's lower edge as a fraction of the mark, which is what a kernel
    reads and what survives a mark that moves between snapshots; the price is recoverable
    as `mark * (1 + lo_pct)`. Empty buckets are omitted rather than stored as zeros: over
    a +-30% span most of them are empty, and a zero and an absence mean the same thing
    *here* only because the span is recorded alongside.
    """
    out = []
    for bucket in liqmap.buckets:
        if bucket.notional <= 0:
            continue
        out.append([round(bucket.low / liqmap.mark - 1, 6), bucket.notional,
                    bucket.cross_notional, bucket.positions])
    return out


def build(
    positions: list[Position],
    coin: str,
    mark: float,
    oi_notional: float,
    bands: tuple[float, ...] = DEFAULT_BANDS,
    bucket_pct: float = 0.00125,
    span: float | None = None,
) -> LiqMap:
    """`bucket_pct` sets resolution, `span` how far from the mark to look.

    Resolution is a display choice, not a data one: the band totals and the
    imbalance are computed from each position's exact liquidation price, so
    they do not change when buckets get finer. Only the histogram does.
    """
    span = max(bands) * 2 if span is None else span
    edges = []
    steps = max(1, int(round(span / bucket_pct)))
    for i in range(-steps, steps):
        edges.append((mark * (1 + i * bucket_pct), mark * (1 + (i + 1) * bucket_pct)))
    buckets = [Bucket(low, high) for low, high in edges]

    liqmap = LiqMap(coin=coin, mark=mark, buckets=buckets, oi_notional=oi_notional)
    liqmap.up = {b: 0.0 for b in bands}
    liqmap.down = {b: 0.0 for b in bands}
    liqmap.up_cross = {b: 0.0 for b in bands}
    liqmap.down_cross = {b: 0.0 for b in bands}

    for position in positions:
        if position.coin != coin or position.size == 0:
            continue
        price, source = effective(position)
        notional = position.notional
        liqmap.total_notional += notional
        if position.margin_type == "cross":
            liqmap.cross_notional += notional
        if source == "published":
            liqmap.published_notional += notional
        else:
            liqmap.derived_notional += notional
        if price is None or price <= 0:
            # A position whose liquidation price is unknown still counts toward
            # coverage -- it is observed -- but cannot be placed on the map.
            liqmap.skipped += 1
            continue
        liqmap.positions += 1
        placed = False
        for bucket in buckets:
            if bucket.low <= price < bucket.high:
                bucket.notional += notional
                if position.margin_type == "cross":
                    bucket.cross_notional += notional
                bucket.positions += 1
                placed = True
                break
        if not placed:
            # Beyond the requested span. Still real, still in the totals -- the
            # histogram is a window, and a window that hides its own edges
            # invites the reader to mistake it for the whole picture.
            liqmap.outside_span += 1
        distance = (price - mark) / mark
        is_cross = position.margin_type == "cross"
        for band in bands:
            if 0 < distance <= band:
                liqmap.up[band] += notional
                if is_cross:
                    liqmap.up_cross[band] += notional
            elif -band <= distance < 0:
                liqmap.down[band] += notional
                if is_cross:
                    liqmap.down_cross[band] += notional
    return liqmap
