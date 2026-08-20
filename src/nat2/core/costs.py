"""The cost model, made explicit and hash-stamped.

Every number here is a decision that changes whether an edge exists. Leaving
them implicit is how a backtest reports a profit that the spread would have
eaten, so they live in one place, are recorded with every result, and carry a
hash -- a result whose cost hash is not on record is not a result.

`threshold()` turns costs into the probability an expert must clear before it
is credited with a decision. The design's rule is that expected edge must
exceed twice the round-trip cost, so a coin flip is not enough and a small
genuine edge below the spread scores as what it is: nothing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

# VERIFY against HL's fee schedule; see FINDINGS.md for the standing rule that
# venue constants carry a checked date rather than being assumed.
@dataclass(frozen=True)
class Costs:
    maker_bps: float = 1.5
    taker_bps: float = 4.5
    half_spread_bps: float = 1.0
    funding_bps_per_hour: float = 0.0
    slippage_bps: float = 1.0
    safety_multiple: float = 2.0
    horizon_hours: float = 1.0
    taker: bool = False

    def round_trip_bps(self) -> float:
        fee = self.taker_bps if self.taker else self.maker_bps
        return 2 * fee + 2 * self.half_spread_bps + self.slippage_bps + \
            self.funding_bps_per_hour * self.horizon_hours

    def threshold(self, move_bps: float = 100.0) -> float:
        """Probability an expert must clear before a decision is credited.

        A binary bet paying `move_bps` on a win and losing it on a miss breaks
        even at p = 0.5 + cost / (2 * move). The safety multiple pushes that
        out so a marginal edge is not mistaken for a tradable one.
        """
        if move_bps <= 0:
            return 1.0
        edge_needed = self.safety_multiple * self.round_trip_bps() / (2 * move_bps)
        return min(0.99, 0.5 + edge_needed)

    def hash(self) -> str:
        body = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()[:16]

    def describe(self) -> dict:
        return {**asdict(self), "round_trip_bps": self.round_trip_bps(),
                "hash": self.hash()}
