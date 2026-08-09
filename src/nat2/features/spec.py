"""What every feature is, and what it is allowed to look at.

The design promised a registry the causal auditor could enumerate. This is it,
and it earns its place by being enforced rather than documentary: the frame
builder may not emit a column that is not declared here, and a test fails the
build if it does. A feature that appears in a training set without a declared
lookback is a feature nobody can audit.

`lookback` is how far back a feature reads, in bars — the quantity that sets
the embargo width for purged walk-forward. `source` says which stream it comes
from, so a gap in one stream tells you exactly which columns went null rather
than which model got worse.
"""

from __future__ import annotations

from dataclasses import dataclass

BAR = "bar"
CONTEXT = "context"
MAP = "map"
EVENT = "event"


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    source: str
    lookback: int          # in bars; 0 means point-in-time
    description: str


def _spec(name, source, lookback, description) -> FeatureSpec:
    return FeatureSpec(name, source, lookback, description)


FEATURES: dict[str, FeatureSpec] = {
    f.name: f
    for f in [
        # --- identity, not features. Kept declared so nothing is undeclared.
        _spec("coin", BAR, 0, "asset"),
        _spec("t_close", BAR, 0, "bar close, market time"),
        _spec("t_decision", BAR, 0, "when the bar became usable; the as-of key"),
        # --- bar
        _spec("close", BAR, 0, "bar close price"),
        _spec("ret", BAR, 0, "close over open minus one"),
        _spec("range_frac", BAR, 0, "high minus low, over open"),
        _spec("volume", BAR, 0, "base-asset volume"),
        _spec("notional", BAR, 0, "quote volume"),
        _spec("prints", BAR, 0, "trade count"),
        _spec("sigma", BAR, 30, "stdev of bar returns over the trailing window"),
        _spec("sigma_regime", BAR, 120, "sigma over its own trailing median"),
        # --- context
        _spec("premium", CONTEXT, 0, "(mark - oracle) / oracle; global pressure"),
        _spec("premium_z", CONTEXT, 30, "premium z-score"),
        _spec("funding", CONTEXT, 0, "hourly funding rate"),
        _spec("funding_z", CONTEXT, 30, "funding z-score"),
        _spec("oi_notional", CONTEXT, 0, "open interest in quote terms"),
        _spec("oi_z", CONTEXT, 30, "open interest z-score"),
        _spec("day_volume", CONTEXT, 0, "24h notional volume"),
        # --- map
        _spec("coverage", MAP, 0, "registry share of venue position notional"),
        _spec("published_frac", MAP, 0, "share of mapped notional using HL's own price"),
        _spec("imb_0005", MAP, 0, "magnet imbalance within 0.5%"),
        _spec("imb_001", MAP, 0, "magnet imbalance within 1%"),
        _spec("imb_002", MAP, 0, "magnet imbalance within 2%"),
        _spec("imb_005", MAP, 0, "magnet imbalance within 5%"),
        _spec("imb_cross_002", MAP, 0, "imbalance of cross-margin mass within 2%"),
        _spec("l_up_002", MAP, 0, "liquidation notional 0-2% above, over day volume"),
        _spec("l_dn_002", MAP, 0, "liquidation notional 0-2% below, over day volume"),
        _spec("d_near_up_pct", MAP, 0, "distance to nearest cluster above, fractional"),
        _spec("d_near_dn_pct", MAP, 0, "distance to nearest cluster below, fractional"),
        _spec("d_near_up", MAP, 30, "d_near_up_pct in units of ONE-BAR sigma"),
        _spec("d_near_dn", MAP, 30, "d_near_dn_pct in units of ONE-BAR sigma"),
        _spec("map_age_s", MAP, 0, "seconds since the map snapshot was taken"),
        # --- events
        _spec("tau", EVENT, 0, "bars since the last observed liquidation in this coin"),
        _spec("liq_flow", EVENT, 30, "liquidated notional over the trailing window"),
    ]
}

BAND_KEYS = {"imb_0005": "0.005", "imb_001": "0.01", "imb_002": "0.02", "imb_005": "0.05"}


def declared() -> set[str]:
    return set(FEATURES)


def max_lookback() -> int:
    """The embargo floor: no fold may be shorter than the deepest feature."""
    return max(f.lookback for f in FEATURES.values())


def undeclared(columns) -> set[str]:
    return set(columns) - declared()
