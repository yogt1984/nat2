"""Asset-context features: premium, funding, open interest.

`premium = (mark - oracle) / oracle` is the reason this system does not need a
second venue. HL marks positions against an oracle built from CEX feeds and
publishes both numbers, so the premium is a native, exact measurement of where
the global market is pushing relative to HL's own book -- the thing a Binance
sidecar was going to be built for.

These records carry no exchange timestamp. HL does not stamp the asset-context
cross-section, so `t_ingest` is the only clock they have, which is exactly why
the poller cadence is captured alongside them: for this stream, arrival time
*is* the observation time, and that is a fact to state rather than paper over.

Rolling statistics look strictly backwards, including the current point. A
z-score that peeks one sample ahead is the classic way a backtest learns to
predict its own inputs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from nat2.hl.schemas import asset_contexts


@dataclass(frozen=True)
class Context:
    t_ingest: int
    coin: str
    mark: float
    oracle: float
    funding: float
    open_interest: float
    day_volume: float

    @property
    def premium(self) -> float:
        """Signed fraction; positive means HL trades above the global oracle."""
        return (self.mark / self.oracle - 1) if self.oracle > 0 else 0.0

    @property
    def oi_notional(self) -> float:
        return self.open_interest * self.mark


def _float(value) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result == result and abs(result) != math.inf else 0.0


def iter_contexts(records) -> list[Context]:
    """Flatten WORM `hl.assetctxs` records into per-coin observations."""
    out: list[Context] = []
    for record in records:
        t_ingest = record.get("t_ingest")
        if t_ingest is None:
            continue
        for ctx in asset_contexts(record.get("payload")):
            coin = ctx.get("coin")
            mark = _float(ctx.get("markPx"))
            if not coin or mark <= 0:
                continue
            out.append(
                Context(
                    t_ingest=t_ingest,
                    coin=coin,
                    mark=mark,
                    oracle=_float(ctx.get("oraclePx")),
                    funding=_float(ctx.get("funding")),
                    open_interest=_float(ctx.get("openInterest")),
                    day_volume=_float(ctx.get("dayNtlVlm")),
                )
            )
    out.sort(key=lambda c: c.t_ingest)
    return out


def by_coin(contexts: list[Context]) -> dict[str, list[Context]]:
    grouped: dict[str, list[Context]] = {}
    for ctx in contexts:
        grouped.setdefault(ctx.coin, []).append(ctx)
    return grouped


def rolling_z(values: list[float], window: int) -> list[float | None]:
    """Backward-looking z-score, current point included.

    `None` until the window is full: a z-score computed from three samples is
    not a small-sample estimate, it is noise wearing a statistic's clothes, and
    emitting 0.0 there would let a model trade on it.
    """
    if window < 2:
        return [None] * len(values)
    out: list[float | None] = []
    for i in range(len(values)):
        if i + 1 < window:
            out.append(None)
            continue
        chunk = values[i + 1 - window : i + 1]
        mean = sum(chunk) / window
        variance = sum((v - mean) ** 2 for v in chunk) / (window - 1)
        sigma = math.sqrt(variance)
        out.append(0.0 if sigma == 0 else (values[i] - mean) / sigma)
    return out


def as_of(contexts: list[Context], t: int) -> Context | None:
    """The most recent observation that had arrived by `t`.

    Forward-fill, never interpolate: an interpolated context is a price nobody
    published, sitting between two that somebody did.
    """
    latest = None
    for ctx in contexts:
        if ctx.t_ingest > t:
            break
        latest = ctx
    return latest


def features(contexts: list[Context], window: int = 30) -> list[dict]:
    """Per-observation context features for one coin, in arrival order."""
    premiums = [c.premium for c in contexts]
    fundings = [c.funding for c in contexts]
    ois = [c.open_interest for c in contexts]
    premium_z = rolling_z(premiums, window)
    funding_z = rolling_z(fundings, window)
    oi_z = rolling_z(ois, window)
    return [
        {
            "t_ingest": c.t_ingest,
            "coin": c.coin,
            "mark": c.mark,
            "oracle": c.oracle,
            "premium": c.premium,
            "premium_z": premium_z[i],
            "funding": c.funding,
            "funding_z": funding_z[i],
            "open_interest": c.open_interest,
            "oi_notional": c.oi_notional,
            "oi_z": oi_z[i],
            "day_volume": c.day_volume,
        }
        for i, c in enumerate(contexts)
    ]
