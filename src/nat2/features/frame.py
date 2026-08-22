"""The L0 feature frame: one row per bar, joined as-of.

Everything here turns on a single rule. Each bar carries `t_decision` --
`Bar.available_at`, when the bar could first have been used -- and every other
input is joined by asking *what had arrived by then*. Never the nearest
observation in time: the nearest is frequently the next one, and using it is
the lookahead this whole system exists to prevent.

Missing is missing. If no map snapshot predates a bar, the map columns are
`None`, not zero and not the current map. Backfilling from today's map is
exactly the mistake persisting map history was built to avoid, and a zero would
be indistinguishable from a genuinely balanced book.

Every emitted column is declared in `spec.FEATURES`. That is enforced, not
documented: a column with no declared lookback is a column nobody can audit,
and the embargo width for walk-forward is computed from those lookbacks.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

from nat2.core.clock import NS
from nat2.features.bars import Bar
from nat2.features.context import Context, rolling_z
from nat2.features.spec import BAND_KEYS, SHELL_KEYS, undeclared

SIGMA_WINDOW = 30
SIGMA_REGIME_WINDOW = 120
LIQ_FLOW_WINDOW = 30
# Every column the map feeds. Null, never zero, when no snapshot predates the bar.
MAP_COLUMNS = (
    "coverage", "published_frac", "imb_0005", "imb_001", "imb_002", "imb_005", "imb_cross_002",
    "l_up_002", "l_dn_002", "d_near_up_pct", "d_near_dn_pct", "d_near_up", "d_near_dn", "map_age_s",
    *(f"m_up_{s}" for s in SHELL_KEYS), *(f"m_dn_{s}" for s in SHELL_KEYS),
)


@dataclass
class FrameStats:
    rows: int = 0
    with_map: int = 0
    with_context: int = 0

    @property
    def map_frac(self) -> float:
        return self.with_map / self.rows if self.rows else 0.0

    @property
    def context_frac(self) -> float:
        return self.with_context / self.rows if self.rows else 0.0

    def summary(self) -> dict:
        return {
            "rows": self.rows,
            "with_map": self.with_map,
            "map_frac": self.map_frac,
            "with_context": self.with_context,
            "context_frac": self.context_frac,
        }


def _as_of(rows, t: int, key):
    """Last entry whose key is <= t. Inputs are sorted, so bisect: the linear walk cost
    rows x contexts per frame build (150M steps on a week of tape) and was repeated per
    placebo replication."""
    i = bisect.bisect_right(rows, t, key=key)
    return rows[i - 1] if i else None


def _stdev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def build(
    bars: list[Bar],
    contexts: list[Context],
    maps: list[dict],
    liquidations: list = (),
    coin: str | None = None,
) -> tuple[list[dict], FrameStats]:
    """One row per bar for a single coin, joined as-of `available_at`."""
    stats = FrameStats()
    if not bars:
        return [], stats
    coin = coin or bars[0].coin

    returns = [b.ret for b in bars]
    sigmas: list[float | None] = []
    for i in range(len(bars)):
        window = returns[max(0, i + 1 - SIGMA_WINDOW) : i + 1]
        sigmas.append(_stdev(window) if len(window) >= SIGMA_WINDOW else None)

    premiums = [c.premium for c in contexts]
    fundings = [c.funding for c in contexts]
    ois = [c.open_interest for c in contexts]
    premium_z = rolling_z(premiums, SIGMA_WINDOW)
    funding_z = rolling_z(fundings, SIGMA_WINDOW)
    oi_z = rolling_z(ois, SIGMA_WINDOW)
    ctx_index = {id(c): i for i, c in enumerate(contexts)}

    events = sorted(liquidations, key=lambda e: e.t_event)

    rows = []
    for i, bar in enumerate(bars):
        t = bar.available_at
        sigma = sigmas[i]
        regime_window = [s for s in sigmas[max(0, i + 1 - SIGMA_REGIME_WINDOW) : i + 1] if s]
        regime_median = _median(regime_window)

        row: dict = {
            "coin": coin,
            "t_close": bar.t_close,
            "t_decision": t,
            "close": bar.close,
            "ret": bar.ret,
            "range_frac": bar.range_frac,
            "volume": bar.volume,
            "notional": bar.notional,
            "prints": bar.prints,
            "sigma": sigma,
            "sigma_regime": (sigma / regime_median) if (sigma and regime_median) else None,
        }

        ctx = _as_of(contexts, t, lambda c: c.t_ingest)
        if ctx is None:
            row.update({k: None for k in (
                "premium", "premium_z", "funding", "funding_z",
                "oi_notional", "oi_z", "day_volume")})
        else:
            stats.with_context += 1
            j = ctx_index[id(ctx)]
            row.update({
                "premium": ctx.premium, "premium_z": premium_z[j],
                "funding": ctx.funding, "funding_z": funding_z[j],
                "oi_notional": ctx.oi_notional, "oi_z": oi_z[j],
                "day_volume": ctx.day_volume,
            })

        snap = _as_of(maps, t, lambda m: m["t_ingest"])
        stats.with_map += snap is not None
        row.update(_map_row(snap, t, sigma, row.get("day_volume")))

        row.update(_event_features(events, bar, i, bars))
        rows.append(row)

    stats.rows = len(rows)
    bad = undeclared(rows[0]) if rows else set()
    if bad:
        raise ValueError(f"frame emitted undeclared column(s): {sorted(bad)}")
    return rows, stats


def rebuild_map_columns(rows: list[dict], maps: list[dict]) -> list[dict]:
    """`rows` with only their map columns recomputed from `maps`, under the same as-of rule.

    For the permutation placebo: barriers are placed from volatility alone, so a permuted
    map changes features and nothing else. Rebuilding the whole frame -- and relabelling --
    per replication was the cost that kept 200 replications out of reach.
    """
    out = []
    for row in rows:
        t = row["t_decision"]
        snap = _as_of(maps, t, lambda m: m["t_ingest"])
        out.append({**row, **_map_row(snap, t, row.get("sigma"), row.get("day_volume"))})
    return out


def _map_row(snap: dict | None, t: int, sigma: float | None, day_volume) -> dict:
    # No map predates this bar: null, never zero -- a zero imbalance is a real reading
    # about a balanced book, and this is the absence of any reading at all.
    return dict.fromkeys(MAP_COLUMNS) if snap is None else _map_features(snap, t, sigma, day_volume)


def _map_features(snap: dict, t: int, sigma: float | None, day_volume) -> dict:
    imb = snap.get("imb", {})
    imb_cross = snap.get("imb_cross", {})
    up = snap.get("up", {})
    down = snap.get("down", {})
    near = snap.get("near", {})

    def scaled(distance):
        # In units of ONE-BAR sigma, which is not the same as sigma over the
        # label horizon: on live data a 1% cluster against 1.3bp bar sigma came
        # out as 72 "sigma", a number that means nothing at an hours-long
        # horizon. The expert scales by sqrt(h) for its own horizon, so the
        # raw fractional distance is emitted alongside and the unit is named in
        # the registry rather than left to be inferred.
        if distance is None or not sigma:
            return None
        return distance / sigma

    def over_volume(value):
        if value is None or not day_volume:
            return None
        return value / day_volume

    return {
        "coverage": snap.get("coverage"),
        "published_frac": snap.get("published_frac"),
        "imb_0005": imb.get(BAND_KEYS["imb_0005"]),
        "imb_001": imb.get(BAND_KEYS["imb_001"]),
        "imb_002": imb.get(BAND_KEYS["imb_002"]),
        "imb_005": imb.get(BAND_KEYS["imb_005"]),
        "imb_cross_002": imb_cross.get("0.02"),
        "l_up_002": over_volume(up.get("0.02")),
        "l_dn_002": over_volume(down.get("0.02")),
        "d_near_up_pct": near.get("up_dist"),
        "d_near_dn_pct": near.get("down_dist"),
        "d_near_up": scaled(near.get("up_dist")),
        "d_near_dn": scaled(near.get("down_dist")),
        "map_age_s": (t - snap["t_ingest"]) / NS,
        **_shells("m_up", up),
        **_shells("m_dn", down),
    }


def _shells(prefix: str, cumulative: dict) -> dict:
    """Per-shell mass from the persisted cumulative band totals (seq 153 §1.2).

    `m_i = cum[B_i] - cum[B_{i-1}]`, floored at 0 -- the floor matters under
    the permutation placebo, which shuffles cumulative totals and can leave
    them non-monotone. A shell whose bounding totals are missing stays `None`.
    """
    out, previous = {}, 0.0
    for key, band in zip(SHELL_KEYS, BAND_KEYS.values()):
        total = cumulative.get(band)
        if total is None or previous is None:
            out[f"{prefix}_{key}"], previous = None, None
            continue
        out[f"{prefix}_{key}"], previous = max(0.0, float(total) - previous), float(total)
    return out


def _event_features(events, bar: Bar, index: int, bars: list[Bar]) -> dict:
    """Bars since the last liquidation, and recent liquidated notional.

    Both keyed on the bar's decision time: a liquidation that happened during
    the bar but reached us afterwards has not happened yet, as far as this row
    is concerned.
    """
    t = bar.available_at
    last_t = None
    flow = 0.0
    window_start = bars[max(0, index + 1 - LIQ_FLOW_WINDOW)].t_open
    for event in events:
        if event.t_event > t:
            break
        last_t = event.t_event
        if event.t_event >= window_start:
            flow += event.notional
    if last_t is None:
        return {"tau": None, "liq_flow": 0.0}
    span = bars[index].t_close - bars[index].t_open
    return {"tau": max(0, (t - last_t) // span) if span else None, "liq_flow": flow}
