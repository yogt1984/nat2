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


@dataclass
class PopulationOverlap:
    """Do the wallets that get liquidated overlap the wallets we map?

    The registry is seeded by equity and by volume -- whoever holds size and
    whoever trades. Neither is the same population as whoever gets liquidated,
    which skews small and leveraged. This measures the gap before any work is
    done to close it.

    Counted two ways on purpose. By wallet, a registry that misses thousands of
    small forced exits looks useless; by notional, the same registry may still
    cover the liquidations that actually move price. Per-position scoring lives
    or dies on the notional number, not the wallet count.
    """

    events: int = 0
    wallets: int = 0
    wallets_in_registry: int = 0
    wallets_mapped: int = 0
    notional: float = 0.0
    notional_in_registry: float = 0.0
    notional_mapped: float = 0.0

    def _ratio(self, part: float, whole: float) -> float:
        return part / whole if whole else 0.0

    @property
    def wallet_frac(self) -> float:
        return self._ratio(self.wallets_in_registry, self.wallets)

    @property
    def mapped_wallet_frac(self) -> float:
        return self._ratio(self.wallets_mapped, self.wallets)

    @property
    def notional_frac(self) -> float:
        return self._ratio(self.notional_in_registry, self.notional)

    @property
    def mapped_notional_frac(self) -> float:
        return self._ratio(self.notional_mapped, self.notional)

    def summary(self) -> dict:
        return {
            "events": self.events,
            "wallets": self.wallets,
            "wallet_frac": self.wallet_frac,
            "mapped_wallet_frac": self.mapped_wallet_frac,
            "notional": self.notional,
            "notional_frac": self.notional_frac,
            "mapped_notional_frac": self.mapped_notional_frac,
        }


def population_overlap(
    events,
    registry: set[str],
    mapped: set[tuple[str, str]],
) -> PopulationOverlap:
    """`mapped` is the set of (address, coin) the map made a claim about."""
    result = PopulationOverlap()
    seen: dict[str, bool] = {}
    seen_mapped: dict[str, bool] = {}
    for event in events:
        result.events += 1
        result.notional += event.notional
        user = event.liquidated_user
        in_registry = user in registry
        is_mapped = (user, event.coin) in mapped
        if in_registry:
            result.notional_in_registry += event.notional
        if is_mapped:
            result.notional_mapped += event.notional
        # A wallet liquidated five times is one wallet, but five events.
        seen[user] = seen.get(user, False) or in_registry
        seen_mapped[user] = seen_mapped.get(user, False) or is_mapped
    result.wallets = len(seen)
    result.wallets_in_registry = sum(1 for v in seen.values() if v)
    result.wallets_mapped = sum(1 for v in seen_mapped.values() if v)
    return result


@dataclass
class ClusterScore:
    """Did liquidations land where the map said mass was, whoever owned it?

    Per-position scoring asks "did *this wallet* blow up at the price we
    computed for it", and it cannot accumulate: `replace_positions` keeps only
    the present, so a wallet that has been liquidated is no longer in the table
    to be matched, and `snapshot_ts` advances past every event on each sweep.
    Measured over 28 readings, `mapped_notional_frac` was 0.0 every time --
    which was the measurement's shape, not the market's.

    The cluster claim is weaker and answerable: the map said mass sat at these
    prices, and forced flow either arrived there or it did not. Ownership never
    enters, so nothing has to survive in a mutable table for the score to
    count.

    Baselines are part of the result, not an afterthought. `side_hit_rate` is
    against a coin flip, because a map that calls the denser side right half
    the time has told you nothing.
    """

    events: int = 0
    scored: int = 0
    no_map: int = 0           # no snapshot for that coin at all
    pre_map: int = 0          # liquidation predates the first snapshot -- unpredicted
    stale_map: int = 0        # newest map older than the bands can survive
    outside_span: int = 0     # beyond the widest band, where the map claimed nothing
    side_hits: int = 0        # landed on the side the map marked denser
    band_hits: int = 0        # landed in a band that actually carried mass
    distances: list[float] = None

    def __post_init__(self):
        if self.distances is None:
            self.distances = []

    def _ratio(self, part: int, whole: int) -> float:
        return part / whole if whole else 0.0

    @property
    def side_hit_rate(self) -> float:
        return self._ratio(self.side_hits, self.scored)

    @property
    def band_hit_rate(self) -> float:
        return self._ratio(self.band_hits, self.scored)

    @property
    def median_distance(self) -> float:
        if not self.distances:
            return 0.0
        ordered = sorted(self.distances)
        return ordered[len(ordered) // 2]

    @property
    def stderr(self) -> float:
        """Standard error of the side hit rate under the coin-flip null."""
        return (0.25 / self.scored) ** 0.5 if self.scored else 0.0

    @property
    def z(self) -> float:
        """Standard errors from a coin flip, signed.

        Reported because a rate without its precision invites the same mistake
        as a rate without its baseline: 42.8% over 208 events is about two
        standard errors from chance, which is suggestive and not decisive, and
        the number alone does not say so.
        """
        return (self.side_hit_rate - 0.5) / self.stderr if self.stderr else 0.0

    def summary(self) -> dict:
        return {
            "events": self.events,
            "scored": self.scored,
            "side_hit_rate": self.side_hit_rate,
            "band_hit_rate": self.band_hit_rate,
            "side_baseline": 0.5,
            "stderr": self.stderr,
            "z": self.z,
            "no_map": self.no_map,
            "pre_map": self.pre_map,
            "stale_map": self.stale_map,
            "outside_span": self.outside_span,
            "median_distance": self.median_distance,
        }


def _band_for(distance: float, bands) -> float | None:
    """The tightest band containing `distance`, or None if beyond them all."""
    for band in sorted(bands):
        if distance <= band:
            return band
    return None


MAX_MAP_AGE_NS = 5 * 60 * 1_000_000_000


def score_clusters(
    events,
    rows_by_coin: dict[str, list[dict]],
    bands=(0.005, 0.01, 0.02, 0.05),
    min_notional: float = 0.0,
    max_age_ns: int = MAX_MAP_AGE_NS,
) -> ClusterScore:
    """Score liquidations against the map *as it was believed before each one*.

    `rows_by_coin` holds persisted map snapshots per coin, arrival ordered.
    Each event is matched to the last snapshot that had arrived **strictly
    before** it -- never the one after, which is the map that already knows.

    **Age is part of the claim.** The tightest band is 0.5% wide, and a two
    minute old mark on a low-cap alt is already further out than that: measured
    2026-08-09, CASHCAT and PUMP moved a median +1.7% and +0.7% between
    snapshot and liquidation, against +0.06% for ARB. Scoring against a stale
    mark measures elapsed price movement and calls it a prediction, so a map
    older than `max_age_ns` is set aside and counted rather than used.
    """
    from nat2.io.mapsnap import as_of

    result = ClusterScore()
    for event in events:
        result.events += 1
        rows = rows_by_coin.get(event.coin)
        if not rows:
            result.no_map += 1
            continue
        # Strictly before: a snapshot taken at the same nanosecond as the
        # liquidation may already contain its consequences.
        snap = as_of(rows, event.t_event - 1)
        if snap is None:
            result.pre_map += 1
            continue
        mark = snap.get("mark") or 0.0
        if mark <= 0:
            result.no_map += 1
            continue
        if max_age_ns and event.t_event - snap["t_ingest"] > max_age_ns:
            result.stale_map += 1
            continue

        offset = event.mark_px / mark - 1
        band = _band_for(abs(offset), bands)
        if band is None:
            result.outside_span += 1
            continue

        key = str(band)
        up = float((snap.get("up") or {}).get(key) or 0.0)
        down = float((snap.get("down") or {}).get(key) or 0.0)
        here, there = (up, down) if offset >= 0 else (down, up)

        result.scored += 1
        result.distances.append(abs(offset))
        if here > min_notional:
            result.band_hits += 1
        if here > there:
            result.side_hits += 1
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
