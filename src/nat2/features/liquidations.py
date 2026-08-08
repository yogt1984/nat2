"""Realized liquidations, and scoring the map against them.

**Where liquidations are actually observable.** The public trade tape carries
no liquidation marker. `userFills` does, in two places:

  the liquidated wallet's own fill    `dir` = "Liquidated Isolated Short", …
  the counterparty's fill             a `liquidation` object naming the
                                      liquidated user, the mark price and the
                                      method ("market" | "backstop")

The second is enormously more abundant -- measured 2026-08-08 over 117,485
fills from 70 wallets, exactly one carried a "Liquidated…" `dir` while 16 of
the 70 wallets had taken the other side of somebody's liquidation. So the
cheap observer set is a handful of high-volume market makers, not the whole
registry: whoever absorbs forced flow sees it.

`userFills` over REST has no per-user cap (unlike the websocket's 15), so this
scales by weight budget rather than by connection count.

**The lookahead trap.** Scoring the map means asking whether liquidations
landed where we said they would *before* they happened. An event that predates
the position snapshot it is scored against is not a prediction, it is a
memory. `score` drops those, and the count of dropped events is reported
rather than silently absorbed.
"""

from __future__ import annotations

from dataclasses import dataclass

from nat2.core.clock import ms_to_ns
from nat2.features.liqmath import Position, effective

LIQUIDATED_DIR_PREFIX = "liquidated"
METHODS = ("market", "backstop")


@dataclass(frozen=True)
class LiquidationEvent:
    tid: int
    t_event: int              # nanoseconds
    coin: str
    liquidated_user: str
    mark_px: float
    method: str
    px: float
    sz: float
    observer: str
    source: str               # "counterparty" | "self"

    @property
    def notional(self) -> float:
        return self.px * self.sz


def _float(value) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result == result and abs(result) != float("inf") else 0.0


def from_fill(fill: dict, observer: str) -> LiquidationEvent | None:
    """One fill -> one liquidation event, or None if it is not one.

    Defensive by construction: HL may add fields, rename methods, or return a
    malformed object, and none of those should produce a fabricated event.
    """
    if not isinstance(fill, dict):
        return None
    coin = fill.get("coin")
    tid = fill.get("tid")
    # bool is a subclass of int, and `True` as a trade id would collide with
    # tid 1 in the dedupe map.
    if not coin or not isinstance(tid, int) or isinstance(tid, bool):
        return None
    px, sz = _float(fill.get("px")), abs(_float(fill.get("sz")))
    t_event = ms_to_ns(fill.get("time")) if fill.get("time") is not None else None
    if px <= 0 or sz <= 0 or t_event is None:
        return None

    info = fill.get("liquidation")
    if isinstance(info, dict):
        liquidated = info.get("liquidatedUser")
        mark = _float(info.get("markPx"))
        if not liquidated or mark <= 0:
            return None
        method = str(info.get("method") or "unknown").lower()
        return LiquidationEvent(
            tid, t_event, coin, liquidated, mark, method, px, sz, observer, "counterparty"
        )

    direction = str(fill.get("dir") or "")
    if direction.lower().startswith(LIQUIDATED_DIR_PREFIX):
        # The liquidated wallet's own side. No mark price is published here, so
        # the fill price stands in -- flagged by `source` so scoring can tell
        # the difference between a published mark and an inferred one.
        return LiquidationEvent(
            tid, t_event, coin, observer, px, "unknown", px, sz, observer, "self"
        )
    return None


def from_fills(fills, observer: str) -> list[LiquidationEvent]:
    if not isinstance(fills, list):
        return []
    out = []
    for fill in fills:
        event = from_fill(fill, observer)
        if event is not None:
            out.append(event)
    return out


def dedupe(events) -> list[LiquidationEvent]:
    """One event per trade id.

    The same liquidation is seen by every observer that took part, and a
    counterparty's view (which carries HL's published mark) is preferred over
    the liquidated wallet's own (where the price is inferred).
    """
    best: dict[int, LiquidationEvent] = {}
    for event in events:
        current = best.get(event.tid)
        if current is None or (
            current.source == "self" and event.source == "counterparty"
        ):
            best[event.tid] = event
    return sorted(best.values(), key=lambda e: (e.t_event, e.tid))


@dataclass
class ScoreResult:
    scored: int = 0
    hits: int = 0
    unmatched: int = 0        # liquidated wallet had no mapped position
    pre_snapshot: int = 0     # happened before the map existed -- not a prediction
    errors: list[float] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []

    @property
    def hit_rate(self) -> float:
        return self.hits / self.scored if self.scored else 0.0

    @property
    def median_error(self) -> float:
        if not self.errors:
            return 0.0
        ordered = sorted(self.errors)
        return ordered[len(ordered) // 2]

    def summary(self) -> dict:
        return {
            "scored": self.scored,
            "hits": self.hits,
            "hit_rate": self.hit_rate,
            "unmatched": self.unmatched,
            "pre_snapshot": self.pre_snapshot,
            "median_error": self.median_error,
        }


def score(
    events,
    positions: list[Position],
    snapshot_ts: int,
    tolerance: float = 0.01,
) -> ScoreResult:
    """Did liquidations happen where the map said, for wallets we had mapped?

    Per-position rather than per-cluster: we predicted a specific price for a
    specific wallet, so that is what gets marked. Only events strictly after
    `snapshot_ts` count -- anything earlier was not predicted by this map.
    """
    mapped: dict[tuple[str, str], float] = {}
    for position in positions:
        price, _source = effective(position)
        if price and price > 0:
            mapped[(position.address, position.coin)] = price

    result = ScoreResult()
    for event in events:
        if event.t_event <= snapshot_ts:
            result.pre_snapshot += 1
            continue
        predicted = mapped.get((event.liquidated_user, event.coin))
        if predicted is None:
            result.unmatched += 1
            continue
        error = abs(event.mark_px - predicted) / predicted
        result.errors.append(error)
        result.scored += 1
        if error <= tolerance:
            result.hits += 1
    return result


def method_notional(events) -> dict[str, float]:
    """Liquidated notional split by method.

    `backstop` is the share the vault had to absorb because the book could not
    -- the direct measurement of unabsorbed cascade that `hlp_delta` wants.
    """
    out: dict[str, float] = {}
    for event in events:
        out[event.method] = out.get(event.method, 0.0) + event.notional
    return out
