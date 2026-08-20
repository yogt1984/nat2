"""gate map -- is the liquidation map good enough to build on?

Three questions, and the third cannot be answered on demand:

  coverage    does the registry see enough of venue-wide positioning?
  fidelity    does our derivation still reproduce HL's published liquidationPx?
  predictive  do realized liquidations land where the map said they would?

`predictive` needs map snapshots followed by observed liquidations, so it is
unanswerable until capture has accumulated both. A gate that cannot be
evaluated must not pass -- so it reports insufficient history and FAILs, which
is the honest state of the world rather than an optimistic default.

The verdict is judged against the pre-registered thresholds in the ledger
(kind `preregistration`, TASK_2/03, seq 117-119), on events that arrived AFTER
those entries. Until the registered window has filled the gate refuses: the
record says so and downstream treats it as not-passed. Everything computed on
the full history is reported as `cumulative` -- descriptive, never gating.
"""

from __future__ import annotations

from pathlib import Path

from nat2.core.clock import NS
from nat2.core.guard import Verdict, record
from nat2.core.registry import Registry
from nat2.features.liqmap import OI_SIDES, LiqMap
from nat2.features.liqmath import validate
from nat2.features.liquidations import band_null, population_overlap, score_clusters
from nat2.features.liqmath import effective
from nat2.ledger.chain import Entry, Ledger
from nat2.validate.audit_feed import Check

NAME = "map"
MIN_COVERAGE = 0.25
MAX_POSITION_AGE_NS = 6 * 3600 * NS
# A map that got three liquidations right is not a validated map. The sample
# floor is what stops a lucky handful from clearing the gate.
MIN_SCORED_LIQUIDATIONS = 30
# Against a coin flip, not against zero: a map that calls the denser side right
# half the time has told you nothing, so the bar is how far above 0.5 it gets.
MIN_SIDE_HIT_RATE = 0.60
# The derivation is a maintenance approximation, not the primary source, so a
# poor score does not fail the gate on its own -- but a *collapse* means HL's
# margin rules moved and the fill-maintained map is drifting blind.
MIN_DERIVATION_EXACT_FRAC = 0.20
# Pre-registered bars (ledger seq 117/118/119). The numbers live here so the
# code is readable; `preregistered()` refuses if the chain says otherwise.
PREREG_NAMES = ("map_per_position_threshold", "map_cluster_threshold", "magnet_runnable_when")
PER_POSITION_MIN = 0.60
CLUSTER_SIDE_MIN = 0.60
CLUSTER_Z_MIN = 3.0
WINDOW_EVENTS = 1000
PERMUTATIONS = 1000


def preregistered(ledger: Ledger) -> dict[str, Entry] | None:
    """Latest entry per pre-registration name, or None if any is missing or
    its text disagrees with the constants above (a superseding entry must be
    matched by a code change, never silently)."""
    found = {e.payload.get("name"): e for e in ledger.entries()
             if e.kind == "preregistration" and e.payload.get("name") in PREREG_NAMES}
    if len(found) < len(PREREG_NAMES):
        return None
    pp, cl = found[PREREG_NAMES[0]].payload, found[PREREG_NAMES[1]].payload
    ok = (f"{PER_POSITION_MIN:.2f}" in str(pp.get("pass_if"))
          and pp.get("window", {}).get("n") == WINDOW_EVENTS
          and f"{CLUSTER_SIDE_MIN:.2f}" in str(cl.get("pass_if", {}).get("side_hit_rate"))
          and f"z >= {CLUSTER_Z_MIN:.0f}" in str(cl.get("pass_if", {}).get("band_hit_rate"))
          and cl.get("window", {}).get("n") == WINDOW_EVENTS)
    return found if ok else None


def run(
    registry: Registry,
    maps: list[LiqMap],
    ledger: Ledger,
    min_coverage: float = MIN_COVERAGE,
    map_series: dict[str, list[dict]] | None = None,
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

    # Scored against the map that PREDATED each liquidation, from persisted
    # snapshot history -- not against the current sweep.
    series = map_series or {}
    all_events = registry.liquidations()
    cumulative = score_clusters(all_events, series)
    detail = {
        "coverage": {m.coin: m.coverage for m in maps},
        "derivation": stats,
        "cumulative": {"predictive": cumulative.summary()},
    }

    prereg = preregistered(ledger)
    if prereg is None:
        checks.append(Check("preregistered", "-", False,
                            "no matching pre-registration in the ledger -- TASK_2/03 first", {}))
        detail.update({"verdict": "refused", "reason": "preregistration_missing"})
        return _finish(ledger, checks, detail), checks
    window_start = max(e.ts for e in prereg.values())
    seqs = sorted(e.seq for e in prereg.values())
    forward = [e for e in all_events if e.t_event > window_start]

    # Per-position, as `nat2 liq coverage` computes it (seq 117 cites it).
    mapped = {(p.address, p.coin) for p in registry.positions() if (effective(p)[0] or 0) > 0}
    overlap = population_overlap(forward, set(registry.addresses()), mapped)
    cluster = score_clusters(forward, series)
    null = band_null(forward, series, permutations=PERMUTATIONS) if cluster.scored else None
    n = cluster.scored
    detail.update({
        "judged_against": seqs,
        "window": {"start_ts": window_start, "need": WINDOW_EVENTS, "scored": n,
                   "events": len(forward), "notional": overlap.notional},
        "per_position": {"mapped_notional_frac": overlap.mapped_notional_frac, "min": PER_POSITION_MIN},
        "cluster": {**cluster.summary(), "side_min": CLUSTER_SIDE_MIN, "z_min": CLUSTER_Z_MIN,
                    "band_null": null.summary() if null else None},
    })
    progress = f"{n}/{WINDOW_EVENTS} forward scoreable events since seq {seqs[-1]}"
    if n < WINDOW_EVENTS:
        checks.append(Check("window", "-", False, f"window not filled: {progress}", detail["window"]))
        detail.update({"verdict": "refused", "reason": "insufficient_forward_events"})
        return _finish(ledger, checks, detail), checks

    pp_pass = overlap.mapped_notional_frac >= PER_POSITION_MIN
    checks.append(Check("per_position", "-", pp_pass,
                        f"mapped notional {overlap.mapped_notional_frac:.1%} (need {PER_POSITION_MIN:.0%}) "
                        f"over {progress}", detail["per_position"]))
    if null is None or not null.informative:
        cl_pass = False
        cl_text = "cluster component refused: permutation null is degenerate (every slot carries mass)"
    else:
        cl_pass = (cluster.side_hit_rate >= CLUSTER_SIDE_MIN and cluster.z >= CLUSTER_Z_MIN
                   and null.z >= CLUSTER_Z_MIN)
        cl_text = (f"side {cluster.side_hit_rate:.1%} z {cluster.z:+.1f} (need {CLUSTER_SIDE_MIN:.0%}, z {CLUSTER_Z_MIN:.0f}); "
                   f"band {null.observed:.1%} vs null {null.null_mean:.1%}±{null.null_sd:.1%} z {null.z:+.1f}")
    checks.append(Check("cluster", "-", cl_pass, cl_text, detail["cluster"]))
    # Either component clears its own pre-registered bar -> the map is usable
    # (seq 117/118 rule); the structural checks above must hold regardless.
    structural = all(c.passed for c in checks if c.name not in ("per_position", "cluster"))
    passed = structural and (pp_pass or cl_pass)
    detail.update({"verdict": "pass" if passed else "fail",
                   "failed": [f"{c.stream}:{c.name}" for c in checks if not c.passed]})
    return record(ledger, NAME, passed, detail), checks


def _finish(ledger: Ledger, checks: list[Check], detail: dict) -> Verdict:
    detail["failed"] = [f"{c.stream}:{c.name}" for c in checks if not c.passed]
    return record(ledger, NAME, False, detail)


def default_paths(root: Path) -> Path:
    return root / "registry.sqlite"
