"""gate magnet -- does the cluster pull, net of costs, better than sign(imb)?

Mostly running, not writing: expert, baseline, purged walk-forward, placebo
and cost model all exist. This file decides *whether* they may run and reads
their answer against the pre-registered rule (HYPOTHESIS_1.md §6, bound to the
ledger by seq 119). It refuses -- recorded as `passed=false, verdict=refused`,
like `gate map` -- when the pre-registration is missing or its numbers moved,
when upstream `gate map` is missing/stale/not PASS (seq 119 `also_requires`),
or below N forward scoreable events or the day floor. Undecidable is not
refuted: 2000 events from four volatile days is one regime sampled repeatedly.

Criterion 2 of §6 ("some alpha > 0 beats alpha = 0") needs a kernel exponent
the shipped expert does not expose. Per TASK_2/08 that is a result, not an
obstacle: reported `not_evaluable`, and the gate cannot PASS without it.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from nat2.core.clock import parse_window
from nat2.core.guard import GateRefusal, Verdict, record, require
from nat2.features.liquidations import match_slots
from nat2.ledger.chain import Entry, Ledger

NAME = "magnet"
PREREG_NAME = "magnet_runnable_when"
MIN_EVENTS = 2000
MIN_DAYS = 30
CELLS_T, CELLS_K = ("1h", "4h"), (1.0, 2.0)   # seq 119 cells_that_gate x HYPOTHESIS_1 §4 k grid
CELL_WIN_FRAC = 12 / 18        # §6 criterion 1, as a fraction of evaluated cells
PLACEBO_ALPHA, PLACEBO_REPLICATIONS = 0.01, 200   # §6 criterion 3; replications per cell
MIN_COVERAGE = 0.25            # §4 universe: coins clearing the `gate map` floor


def preregistered(ledger: Ledger) -> Entry | None:
    entry = ledger.latest("preregistration", name=PREREG_NAME)
    if entry is None:
        return None
    p = entry.payload
    ok = (p.get("min_scoreable_events") == MIN_EVENTS and p.get("min_distinct_days") == MIN_DAYS
          and list(p.get("cells_that_gate") or []) == list(CELLS_T))
    return entry if ok else None


def accrual(events, map_series: dict, since_ns: int) -> dict:
    """Forward scoreable events (the `scored` bucket) and the distinct UTC days they span."""
    days: set[str] = set()
    n = 0
    for kind, event, _, _ in match_slots([e for e in events if e.t_event > since_ns], map_series):
        if kind == "scored":
            n += 1
            days.add(datetime.fromtimestamp(event.t_event / 1e9, timezone.utc).strftime("%Y-%m-%d"))
    return {"scored": n, "need": MIN_EVENTS, "days": len(days), "need_days": MIN_DAYS, "since_ts": since_ns}


def decide(cells: list[dict]) -> dict:
    """The pre-registered rule on per-cell results; pure. Criterion 4 is the cost call
    already made by `Costs.threshold()`: OOS decision hit rate must exceed it."""
    wins = [c for c in cells if c["beats_baseline"]]
    c1 = bool(cells) and len(wins) / len(cells) >= CELL_WIN_FRAC
    c3 = bool(wins) and all(c.get("placebo_p") is not None and c["placebo_p"] <= PLACEBO_ALPHA for c in wins)
    c4 = bool(wins) and all((c.get("decision_hit_rate") or 0.0) > c["threshold"] for c in wins)
    return {
        "cells": cells, "cells_evaluated": len(cells), "cells_won": len(wins),
        "criteria": {"1_beats_baseline": c1, "2_alpha_kernel": "not_evaluable",
                     "3_placebo_collapses": c3, "4_clears_cost": c4},
        "passed": False,   # criterion 2 cannot be true with the shipped expert
        "would_pass_on_1_3_4": c1 and c3 and c4,
    }


def provenance(repo: Path) -> dict:
    def git(*a):
        return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True).stdout.strip()
    return {"commit": git("rev-parse", "HEAD"), "clean_tree": git("status", "--porcelain") == ""}


def run(ledger: Ledger, events, map_series: dict, coverage: dict[str, float],
        evaluate_cell, repo: Path) -> Verdict:
    """`evaluate_cell(coin, horizon, k) -> dict | None` is injected so the rule is testable
    without a tape; production uses `cell_evaluator` below."""
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
    acc = accrual(events, map_series, prereg.ts)
    detail["window"] = acc
    if acc["scored"] < MIN_EVENTS or acc["days"] < MIN_DAYS:
        detail.update({"verdict": "refused", "reason": "insufficient_forward_events",
                       "progress": f"{acc['scored']}/{MIN_EVENTS} events, {acc['days']}/{MIN_DAYS} days"})
        return record(ledger, NAME, False, detail)
    universe = sorted(c for c, cov in coverage.items() if cov >= MIN_COVERAGE)
    cells = [evaluate_cell(coin, h, k) for coin in universe for h in CELLS_T for k in CELLS_K]
    cells = [c for c in cells if c is not None]
    outcome = decide(cells)
    detail.update(outcome, universe=universe,
                  verdict="pass" if outcome["passed"] else "fail",
                  horizons_ns={h: parse_window(h) for h in CELLS_T})
    return record(ledger, NAME, outcome["passed"], detail)

def cell_evaluator(root: Path, registry, placebo: int = PLACEBO_REPLICATIONS, seed: int = 0):
    """The production `evaluate_cell`: exactly `nat2 eval --placebo`, per (coin, horizon, k)."""
    from nat2.core.costs import Costs
    from nat2.experts.magnet_a import MagnetA, build_dataset
    from nat2.features.bars import bars, iter_prints, path
    from nat2.features.context import by_coin, iter_contexts
    from nat2.features.frame import build as build_frame
    from nat2.io.mapsnap import STREAM, series
    from nat2.io.worm import read_records
    from nat2.validate.evaluate import evaluate
    from nat2.validate.placebo import PlaceboResult, permute_series

    def evaluate_cell(coin: str, horizon: str, k: float) -> dict | None:
        h_ns, bar_ns = parse_window(horizon), parse_window("1m")
        prints = iter_prints(read_records(root, "hl.trades"))
        built = bars(prints, bar_ns, coin=coin)
        contexts = by_coin(iter_contexts(read_records(root, "hl.assetctxs"))).get(coin, [])
        maps = series(read_records(root, STREAM), coin)
        events = [e for e in registry.liquidations() if e.coin == coin]
        expert = MagnetA(horizon_ns=h_ns)
        paths = {coin: path(prints, coin)}

        def score(snaps):
            rows, _ = build_frame(built, contexts, snaps, liquidations=events, coin=coin)
            data, _ = build_dataset(rows, paths, h_ns, expert.features, bar_ns=bar_ns, k=k)
            return evaluate(expert, data, h_ns, Costs(), n_splits=5) if len(data) else None

        real = score(maps)
        if real is None:
            return None
        zs = [r.delta_z for r in (score(permute_series({coin: maps}, seed + i)[coin]) for i in range(placebo)) if r]
        v = real.verdict()
        return {"coin": coin, "horizon": horizon, "k": k, "n": v["expert"]["n"],
                "beats_baseline": real.beats_baseline, "delta_z": real.delta_z,
                "placebo_p": PlaceboResult(real.delta_z, zs).p_value if zs else None,
                "decision_hit_rate": v["expert"]["decision_hit_rate"], "threshold": v["threshold"]}
    return evaluate_cell
