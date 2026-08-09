"""Bars, and the tick path the labels actually walk.

Two clocks do two different jobs here, and conflating them is how lookahead
gets in.

`t_event` decides *which bar a print belongs to*. That is market time: a trade
that happened at 12:00:59 belongs to the 12:00 bar no matter when it reached us.

`t_ingest` decides *when the bar could first have been used*, and that is the
**later** of two things: the bar's close, and the arrival of its last print. A
bar cannot be used before it closes — until then you do not know it is finished
— and it cannot be used before its prints arrive either. Real capture showed
both cases: bars whose last print arrived 42 seconds before the close, and bars
whose prints arrived after it. `Bar.available_at` takes the max, so a feature
computed for a decision at time T can drop bars it could not yet have seen.

Bars are built from the same prints the labels walk, so the OHLC and the label
path can never disagree about what happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from nat2.core.clock import ms_to_ns


@dataclass(frozen=True)
class Print:
    t_event: int
    t_ingest: int
    coin: str
    px: float
    sz: float

    @property
    def notional(self) -> float:
        return self.px * self.sz


@dataclass
class Bar:
    coin: str
    t_open: int          # inclusive bucket start, in market time
    t_close: int         # exclusive bucket end
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    notional: float = 0.0
    prints: int = 0
    # max(close, last arrival): a bar is unusable before it is finished, and
    # unusable before its data lands. Whichever is later governs.
    available_at: int = 0

    @property
    def vwap(self) -> float:
        return self.notional / self.volume if self.volume else 0.0

    @property
    def ret(self) -> float:
        return (self.close / self.open - 1) if self.open else 0.0

    @property
    def range_frac(self) -> float:
        return ((self.high - self.low) / self.open) if self.open else 0.0


def iter_prints(records) -> list[Print]:
    """Flatten WORM `hl.trades` records into individual prints.

    Sorted by market time, ties broken by arrival order, which keeps the
    sequence the labels rely on stable and reproducible.
    """
    out: list[Print] = []
    for record in records:
        t_ingest = record.get("t_ingest")
        payload = record.get("payload")
        if t_ingest is None or not isinstance(payload, list):
            continue
        for trade in payload:
            if not isinstance(trade, dict):
                continue
            coin = trade.get("coin")
            t_event = ms_to_ns(trade.get("time")) if trade.get("time") is not None else None
            try:
                px = float(trade.get("px"))
                sz = abs(float(trade.get("sz")))
            except (TypeError, ValueError):
                continue
            if not coin or t_event is None or px <= 0 or sz <= 0:
                continue
            out.append(Print(t_event, t_ingest, coin, px, sz))
    out.sort(key=lambda p: p.t_event)
    return out


def bars(prints: list[Print], interval_ns: int, coin: str | None = None) -> list[Bar]:
    """Time bars in market time. Empty intervals are absent, not zero-filled.

    A bar that did not trade is not a bar with zero volume and a flat price --
    it is a period with no information, and inventing one would hand the model
    a fabricated observation.
    """
    if interval_ns <= 0:
        return []
    built: dict[int, Bar] = {}
    order: list[int] = []
    for p in prints:
        if coin is not None and p.coin != coin:
            continue
        bucket = (p.t_event // interval_ns) * interval_ns
        bar = built.get(bucket)
        if bar is None:
            bar = Bar(
                coin=p.coin, t_open=bucket, t_close=bucket + interval_ns,
                open=p.px, high=p.px, low=p.px, close=p.px,
            )
            built[bucket] = bar
            order.append(bucket)
        bar.high = max(bar.high, p.px)
        bar.low = min(bar.low, p.px)
        bar.close = p.px
        bar.volume += p.sz
        bar.notional += p.notional
        bar.prints += 1
        bar.available_at = max(bar.available_at, p.t_ingest, bar.t_close)
    return [built[k] for k in sorted(order)]


def path(prints: list[Print], coin: str) -> list[tuple[int, float]]:
    """The tick path for one coin, in the order the labels must walk it."""
    return [(p.t_event, p.px) for p in prints if p.coin == coin]


def visible_at(bars_: list[Bar], t: int) -> list[Bar]:
    """Bars whose last print had arrived by `t`.

    Not `t_close <= t`: a bar closes in market time but arrives later, and the
    gap between the two is precisely where lookahead hides.
    """
    return [b for b in bars_ if b.available_at <= t]


@dataclass
class Series:
    """Aligned per-coin bar series, for feature assembly."""

    interval_ns: int
    by_coin: dict[str, list[Bar]] = field(default_factory=dict)

    @classmethod
    def build(cls, prints: list[Print], interval_ns: int) -> "Series":
        coins = {p.coin for p in prints}
        return cls(
            interval_ns=interval_ns,
            by_coin={c: bars(prints, interval_ns, coin=c) for c in sorted(coins)},
        )

    def coins(self) -> list[str]:
        return sorted(self.by_coin)

    def closes(self, coin: str) -> list[float]:
        return [b.close for b in self.by_coin.get(coin, [])]
