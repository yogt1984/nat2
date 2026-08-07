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
    total_notional: float = 0.0
    cross_notional: float = 0.0
    published_notional: float = 0.0
    derived_notional: float = 0.0
    oi_notional: float = 0.0
    positions: int = 0
    skipped: int = 0

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

    def summary(self) -> dict:
        return {
            "coin": self.coin,
            "mark": self.mark,
            "coverage": self.coverage,
            "oi_sides": OI_SIDES,
            "positions": self.positions,
            "skipped": self.skipped,
            "published_frac": self.published_frac,
            "notional": self.total_notional,
            "imb": {str(b): self.imbalance(b) for b in self.up},
        }


def build(
    positions: list[Position],
    coin: str,
    mark: float,
    oi_notional: float,
    bands: tuple[float, ...] = DEFAULT_BANDS,
    bucket_pct: float = 0.0025,
) -> LiqMap:
    span = max(bands) * 2
    edges = []
    steps = int(span / bucket_pct)
    for i in range(-steps, steps):
        edges.append((mark * (1 + i * bucket_pct), mark * (1 + (i + 1) * bucket_pct)))
    buckets = [Bucket(low, high) for low, high in edges]

    liqmap = LiqMap(coin=coin, mark=mark, buckets=buckets, oi_notional=oi_notional)
    liqmap.up = {b: 0.0 for b in bands}
    liqmap.down = {b: 0.0 for b in bands}

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
        for bucket in buckets:
            if bucket.low <= price < bucket.high:
                bucket.notional += notional
                if position.margin_type == "cross":
                    bucket.cross_notional += notional
                bucket.positions += 1
                break
        distance = (price - mark) / mark
        for band in bands:
            if 0 < distance <= band:
                liqmap.up[band] += notional
            elif -band <= distance < 0:
                liqmap.down[band] += notional
    return liqmap
