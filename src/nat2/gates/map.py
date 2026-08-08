"""gate map -- is the liquidation map good enough to build on?

Three questions, and the third cannot be answered on demand:

  coverage    does the registry see enough of venue-wide positioning?
  fidelity    does our derivation still reproduce HL's published liquidationPx?
  predictive  do realized liquidations land where the map said they would?

`predictive` needs map snapshots followed by observed liquidations, so it is
unanswerable until capture has accumulated both. A gate that cannot be
evaluated must not pass -- so it reports insufficient history and FAILs, which
is the honest state of the world rather than an optimistic default.
"""

from __future__ import annotations

from pathlib import Path

from nat2.core.clock import NS
from nat2.core.guard import Verdict, record
from nat2.core.registry import Registry
from nat2.features.liqmap import OI_SIDES, LiqMap
from nat2.features.liqmath import validate
from nat2.features.liquidations import score
from nat2.ledger.chain import Ledger
from nat2.validate.audit_feed import Check

NAME = "map"
MIN_COVERAGE = 0.25
MAX_POSITION_AGE_NS = 6 * 3600 * NS
# A map that got three liquidations right is not a validated map. The sample
# floor is what stops a lucky handful from clearing the gate.
MIN_SCORED_LIQUIDATIONS = 30
MIN_HIT_RATE = 0.60
PRICE_TOLERANCE = 0.01
# The derivation is a maintenance approximation, not the primary source, so a
# poor score does not fail the gate on its own -- but a *collapse* means HL's
# margin rules moved and the fill-maintained map is drifting blind.
MIN_DERIVATION_EXACT_FRAC = 0.20


def run(
    registry: Registry,
    maps: list[LiqMap],
    ledger: Ledger,
    min_coverage: float = MIN_COVERAGE,
    tolerance: float = PRICE_TOLERANCE,
    min_events: int = MIN_SCORED_LIQUIDATIONS,
    min_hit_rate: float = MIN_HIT_RATE,
) -> tuple[Verdict, list[Check]]:
    checks: list[Check] = []

    snapshot_ts = registry.positions_ts()
    age = registry.position_age_ns()
    checks.append(
        Check(
            "positions_fresh",
            "-",
            age is not None and age <= MAX_POSITION_AGE_NS,
            "no positions recorded -- run `nat2 wallets snapshot`"
            if age is None
            else f"positions {age / NS / 3600:.1f}h old "
            f"(limit {MAX_POSITION_AGE_NS / NS / 3600:.0f}h)",
            {"age_h": age / NS / 3600 if age else None},
        )
    )

    for liqmap in maps:
        checks.append(
            Check(
                "coverage",
                liqmap.coin,
                liqmap.coverage >= min_coverage,
                f"{liqmap.coverage:.1%} of venue position notional "
                f"(OI x{OI_SIDES:g}; floor {min_coverage:.0%}), "
                f"{liqmap.published_frac:.0%} published",
                {"coverage": liqmap.coverage, "published_frac": liqmap.published_frac},
            )
        )

    stats = validate(registry.positions())
    exact = stats.get("exact_frac", 0.0)
    checks.append(
        Check(
            "derivation_fidelity",
            "-",
            stats.get("n", 0) > 0 and exact >= MIN_DERIVATION_EXACT_FRAC,
            f"derivation reproduces HL liquidationPx exactly for {exact:.0%} of "
            f"{stats.get('n', 0)} positions (median rel err {stats.get('median', 0):.1e})",
            stats,
        )
    )

    scored = score(
        registry.liquidations(), registry.positions(), snapshot_ts or 0, tolerance
    )
    detail_text = (
        f"{scored.hits}/{scored.scored} liquidations within {tolerance:.1%} of the mapped "
        f"price (median err {scored.median_error:.2%}); "
        f"{scored.unmatched} unmapped wallet(s), {scored.pre_snapshot} predate the map"
    )
    if snapshot_ts is None:
        detail_text = "no position snapshot, so nothing was predicted"
    elif scored.scored < min_events:
        detail_text = (
            f"insufficient history: {scored.scored} scored liquidation(s), need "
            f"{min_events}. {scored.unmatched} unmapped, {scored.pre_snapshot} predate the "
            "map. Capture must accumulate liquidations that happened AFTER a snapshot."
        )
    checks.append(
        Check(
            "predictive",
            "-",
            snapshot_ts is not None
            and scored.scored >= min_events
            and scored.hit_rate >= min_hit_rate,
            detail_text,
            scored.summary(),
        )
    )

    passed = all(c.passed for c in checks)
    detail = {
        "failed": [f"{c.stream}:{c.name}" for c in checks if not c.passed],
        "coverage": {m.coin: m.coverage for m in maps},
        "derivation": stats,
        "predictive": scored.summary(),
    }
    return record(ledger, NAME, passed, detail), checks


def default_paths(root: Path) -> Path:
    return root / "registry.sqlite"
