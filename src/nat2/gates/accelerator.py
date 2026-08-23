"""gate accelerator -- after the touch, does the sweep continue, and is it the map's doing?

The rule of `HYPOTHESIS_2.md`, bound to the ledger by seq 191. Like `gate magnet` this file
decides *whether* the machinery may run and reads its answer against a rule fixed before the
data; the expert, the label, the walk-forward and the placebo already exist.

It refuses -- recorded as `passed=false, verdict=refused`, a progress stamp rather than an
error -- when the pre-registration is missing or its numbers moved, when upstream `gate map`
is missing/stale/not PASS, or below the resolved-touch and distinct-day floors.

Criterion 2 is not the same shape as `gate magnet`'s. There the second criterion asks whether
a distance kernel beats no kernel; here it asks whether a **mass-blind** model matches the full
one, because this sample is conditioned on a price move and the permutation placebo cannot see
momentum: shuffling map mass leaves the pre-touch move exactly where it was.

Criterion 3's statistic is that same margin, not the expert's total skill. `MagnetB` reads
non-map columns too, so a permutation corrupts its baseline as well and the expert-versus-
baseline delta stays high for a reason that has nothing to do with the hypothesis. What must
vanish under a shuffled map is the map's *contribution*.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from nat2.core.clock import parse_window
from nat2.core.guard import GateRefusal, Verdict, record, require
from nat2.gates.magnet import provenance
from nat2.ledger.chain import Entry, Ledger

NAME = "accelerator"
PREREG_NAME = "accelerator_stage_b"
MIN_TOUCHES = 2000
MIN_DAYS = 30
CELLS_T, CELLS_K = ("15m", "1h"), (1.0, 2.0)      # seq 191 §1.5; closed
CELL_WIN_FRAC = 2 / 3                              # §1.8 criterion 1
PLACEBO_ALPHA, PLACEBO_REPLICATIONS = 0.01, 200    # §1.7
MIN_COVERAGE = 0.25                                # §1.5 universe


def preregistered(ledger: Ledger) -> Entry | None:
    """Seq 191's floors and grid must match the code that reads them."""
    entry = ledger.latest("preregistration", name=PREREG_NAME)
    if entry is None:
        return None
    p = entry.payload
    ok = (p.get("min_resolved_touches") == MIN_TOUCHES and p.get("min_distinct_days") == MIN_DAYS
          and list(p.get("horizons") or []) == list(CELLS_T)
          and [float(k) for k in p.get("k") or []] == list(CELLS_K)
          and p.get("min_coverage") == MIN_COVERAGE)
    return entry if ok else None


def accrual(events, since_ns: int) -> dict:
    """Resolved touches since `since_ns` and the distinct UTC days they span.

    `events` are the touches that produced a label -- a touch whose race timed out is not
    resolved, so it does not count toward a floor that exists to guarantee answers.
    """
    days = {datetime.fromtimestamp(t / 1e9, timezone.utc).strftime("%Y-%m-%d")
            for t in events if t > since_ns}
    n = sum(1 for t in events if t > since_ns)
    return {"resolved": n, "need": MIN_TOUCHES, "days": len(days), "need_days": MIN_DAYS,
            "since_ts": since_ns}


def decide(cells: list[dict]) -> dict:
    """The pre-registered rule on per-cell results; pure.

    A cell is *won* when the expert beats `sign(F)`. A win *counts* only if it also collapses
    under the mass permutation and clears cost. Criterion 2 is separate and is about the map
    earning its place: in a majority of the cells won, the full expert must also beat its own
    map-blind ablation.
    """
    wins = [c for c in cells if c["beats_baseline"]]
    c1 = bool(cells) and len(wins) / len(cells) >= CELL_WIN_FRAC
    c2 = bool(wins) and sum(1 for c in wins if c.get("beats_without_map")) > len(wins) / 2
    c3 = bool(wins) and all(c.get("placebo_p") is not None and c["placebo_p"] <= PLACEBO_ALPHA for c in wins)
    c4 = bool(wins) and all((c.get("decision_hit_rate") or 0.0) > c["threshold"] for c in wins)
    return {
        "cells": cells, "cells_evaluated": len(cells), "cells_won": len(wins),
        "cells_map_earned": sum(1 for c in wins if c.get("beats_without_map")),
        "criteria": {"1_beats_baseline": c1, "2_map_earns_its_place": c2,
                     "3_placebo_collapses": c3, "4_clears_cost": c4},
        "passed": c1 and c2 and c3 and c4,
    }


def run(ledger: Ledger, resolved_ts, coverage: dict[str, float], evaluate_cell, repo: Path) -> Verdict:
    """`evaluate_cell(coin, horizon, k) -> dict | None` is injected so the rule is testable
    without a tape; `resolved_ts` are the decision times of resolved touches."""
    detail: dict = {"provenance": provenance(repo)}
    prereg = preregistered(ledger)
    if prereg is None:
        detail.update({"verdict": "refused", "reason": "preregistration_missing"})
        return record(ledger, NAME, False, detail)
    detail["judged_against"] = [prereg.seq]
    try:
        upstream = require(ledger, "map")
    except GateRefusal as exc:
        detail.update({"verdict": "refused", "reason": "upstream_map", "upstream": str(exc)})
        return record(ledger, NAME, False, detail)
    if upstream.detail.get("verdict") != "pass":
        detail.update({"verdict": "refused", "reason": "upstream_map", "upstream": "map did not pass"})
        return record(ledger, NAME, False, detail)
    acc = accrual(resolved_ts, prereg.ts)
    detail["window"] = acc
    if acc["resolved"] < MIN_TOUCHES or acc["days"] < MIN_DAYS:
        detail.update({"verdict": "refused", "reason": "insufficient_resolved_touches",
                       "progress": f"{acc['resolved']}/{MIN_TOUCHES} touches, {acc['days']}/{MIN_DAYS} days"})
        return record(ledger, NAME, False, detail)
    universe = sorted(c for c, cov in coverage.items() if cov >= MIN_COVERAGE)
    cells = [evaluate_cell(coin, h, k) for coin in universe for h in CELLS_T for k in CELLS_K]
    cells = [c for c in cells if c is not None]
    outcome = decide(cells)
    detail.update(outcome, universe=universe, verdict="pass" if outcome["passed"] else "fail",
                  horizons_ns={h: parse_window(h) for h in CELLS_T})
    return record(ledger, NAME, outcome["passed"], detail)


def cell_evaluator(root: Path, registry, placebo: int = PLACEBO_REPLICATIONS, seed: int = 0):
    """The production `evaluate_cell`, per (coin, horizon, k): expert vs `sign(F)`, the same
    labelled rows re-featured for the placebo, and the map-blind ablation on the real map."""
    from nat2.core.costs import Costs
    from nat2.experts.magnet_b import MagnetB, build_dataset
    from nat2.features.bars import bars, iter_prints, path
    from nat2.features.context import by_coin, iter_contexts
    from nat2.features.frame import build as build_frame, rebuild_map_columns
    from nat2.io.mapsnap import STREAM, series
    from nat2.io.worm import read_records
    from nat2.labels.touch import touches
    from nat2.validate.evaluate import Comparison, evaluate
    from nat2.validate.placebo import PlaceboResult, permute_series

    def evaluate_cell(coin: str, horizon: str, k: float) -> dict | None:
        h_ns, bar_ns = parse_window(horizon), parse_window("1m")
        prints = iter_prints(read_records(root, "hl.trades"), coin=coin)
        built = bars(prints, bar_ns, coin=coin)
        contexts = by_coin(iter_contexts(read_records(root, "hl.assetctxs"))).get(coin, [])
        maps = series(read_records(root, STREAM), coin)
        events = [e for e in registry.liquidations() if e.coin == coin]
        rows, _ = build_frame(built, contexts, maps, liquidations=events, coin=coin)
        tape = path(prints, coin)
        expert = MagnetB(horizon_ns=h_ns)
        # Detected on the real map, and the labels are map-independent, so seq 191 §1.7 holds
        # both fixed across every replication: the placebo moves the covariate and nothing else.
        found = touches(tape, maps, coin)
        data, _ = build_dataset(found, rows, {coin: tape}, h_ns, expert.features, bar_ns=bar_ns, k=k)
        if not len(data):
            return None
        real = evaluate(expert, data, h_ns, Costs(), n_splits=5)
        ablation = expert.without_map()
        blind = evaluate(ablation, data.select(ablation.features), h_ns, Costs(), n_splits=5)
        # Criterion 2 is the *same* rule as criterion 1, with the ablation standing in for the
        # baseline: same folds, same test rows, so the per-row losses pair. A bare `delta >
        # delta` would make it a coin flip exactly where the two models are equally good --
        # which is the case the criterion exists to reject.
        earned = Comparison(expert=real.expert, baseline=blind.expert, threshold=real.threshold,
                            folds=real.folds, costs=real.costs)
        kept = {t.t: t for t in found}
        zs = []
        for i in range(placebo):
            shuffled = permute_series({coin: maps}, seed + i)[coin]
            fake = data.with_rows(_refuel(rebuild_map_columns(data.rows, shuffled), kept, shuffled))
            full = evaluate(expert, fake, h_ns, Costs(), n_splits=5)
            null = evaluate(ablation, fake.select(ablation.features), h_ns, Costs(), n_splits=5)
            zs.append(Comparison(expert=full.expert, baseline=null.expert, threshold=full.threshold,
                                 folds=full.folds, costs=full.costs).delta_z)
        v = real.verdict()
        return {"coin": coin, "horizon": horizon, "k": k, "n": v["expert"]["n"],
                "beats_baseline": real.beats_baseline, "delta_z": real.delta_z,
                "beats_without_map": earned.beats_baseline,
                "delta_vs_without_map": earned.delta, "z_vs_without_map": earned.delta_z,
                "placebo_p": PlaceboResult(earned.delta_z, zs).p_value if zs else None,
                "decision_hit_rate": v["expert"]["decision_hit_rate"], "threshold": v["threshold"]}
    return evaluate_cell


def _refuel(rows: list[dict], kept: dict, permuted: list[dict]) -> list[dict]:
    """Fuel and brake recomputed from the permuted map, for the *same* touches.

    Not re-detected: seq 191 §1.7 holds the touch set fixed, so what changes is only how much
    mass the permutation put ahead of a sweep that happened regardless. Permutation preserves
    each snapshot's arrival, so the shuffled counterpart is found by `t_ingest`.
    """
    from dataclasses import replace

    from nat2.experts.magnet_b import touch_features
    from nat2.labels.touch import fuel_and_brake

    by_ingest = {snap["t_ingest"]: snap for snap in permuted}
    out = []
    for row in rows:
        touch = kept.get(row["t_decision"])
        snap = by_ingest.get(touch.t_snap) if touch else None
        if snap is None:
            out.append(dict(row))
            continue
        fuel, brake = fuel_and_brake(snap, touch.side, touch.band)
        out.append({**row, **touch_features(replace(touch, fuel=fuel, brake=brake))})
    return out
