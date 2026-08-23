"""HYPOTHESIS_2 (ledger seq 191): the touch, the sign, and the two ways this claim dies quietly.

The unit checks are on hand-built snapshots; the world tests are at the horizon's scale, per the
standing rule, and they exist because this hypothesis has a confound Stage A does not: the sample
is conditioned on a price move, so a momentum world must NOT be credited to the map, and the
permutation placebo cannot tell the difference on its own.
"""

import math
import random

import pytest

from nat2.core.clock import NS
from nat2.core.costs import Costs
from nat2.experts.magnet_b import FuelBaseline, MagnetB, build_dataset
from nat2.features.bars import Bar
from nat2.features.frame import build as build_frame
from nat2.gates import accelerator as gate
from nat2.labels.touch import BANDS, Touch, fuel_and_brake, shell_mass, shell_of, touches
from nat2.ledger.chain import Ledger
from nat2.validate.evaluate import evaluate

MIN, HOUR = 60 * NS, 3600 * NS
PREREG = {"name": "accelerator_stage_b", "min_resolved_touches": 2000, "min_distinct_days": 30,
          "horizons": ["15m", "1h"], "k": [1, 2], "min_coverage": 0.25}


def _snap(t, mark=100.0, up=(0.0, 2e5, 0.0, 7e5), down=(0.0, 0.0, 1e5, 3e5)):
    """Cumulative bands from per-shell masses, as io.mapsnap persists them."""
    cu, cd, U, D = 0.0, 0.0, {}, {}
    for band, u, d in zip(("0.005", "0.01", "0.02", "0.05"), up, down):
        cu, cd = cu + u, cd + d
        U[band], D[band] = cu, cd
    return {"t_ingest": t, "coin": "BTC", "mark": mark, "up": U, "down": D, "coverage": 0.3,
            "published_frac": 0.9, "imb": {}, "imb_cross": {}, "near": {}}


# --- the touch ------------------------------------------------------------

def test_a_touch_needs_mass_a_prior_map_and_a_fresh_one():
    snap = _snap(0)
    assert shell_of(0.008) == ("up", 0.01) and shell_of(-0.03) == ("down", 0.05) and shell_of(0.09) is None
    assert shell_mass(snap, "up", 0.01) == 2e5 and shell_mass(snap, "up", 0.005) == 0.0
    assert fuel_and_brake(snap, "up", 0.01) == (7e5, 4e5)      # ahead beyond the shell; behind, all of it
    path = [(1, 100.0), (2, 100.8), (3, 100.9), (4, 100.2), (5, 97.0)]
    found = touches(path, [snap], "BTC")
    assert [(t.t, t.side, t.band, t.sweep) for t in found] == [(2, "up", 0.01, 1), (5, "down", 0.05, -1)]
    assert found[0].fuel == 7e5 and found[0].brake == 4e5 and found[0].f == pytest.approx(3 / 11)
    assert found[1].fuel == 0.0                                 # outermost shell: the map's span truncates it
    # A snapshot at the same nanosecond may already contain the touch's consequences.
    assert touches([(0, 100.8)], [_snap(0)], "BTC") == []
    # Stale beyond the limit, and an empty shell, are both "not an observation".
    assert touches([(10 * MIN, 100.8)], [_snap(0)], "BTC") == []
    assert touches([(2, 100.2)], [_snap(0)], "BTC") == []       # 0.2% shell carries nothing


def test_the_same_shell_is_one_touch_per_hour():
    snaps = [_snap(0)]
    path = [(2, 100.8), (3, 100.0), (4, 100.8), (2 * HOUR, 100.0), (2 * HOUR + 1, 100.8)]
    assert [t.t for t in touches(path, snaps, "BTC", max_age_ns=3 * HOUR)] == [2, 2 * HOUR + 1]


# --- the sign trap --------------------------------------------------------

def _bars(n=200, seed=1, drift=0.0):
    rng = random.Random(seed)
    out, price = [], 100.0
    for i in range(n):
        close = price * math.exp(drift + 0.002 * rng.gauss(0, 1))
        out.append(Bar("BTC", i * MIN, (i + 1) * MIN, open=price, high=max(price, close),
                       low=min(price, close), close=close, volume=1.0, notional=close, prints=2,
                       available_at=(i + 1) * MIN))
        price = close
    return out


def test_y_is_continuation_not_snap_back():
    """`fade` returns +1 for the snap-back; H2 is about continuation, so y must be its negation."""
    snaps = [_snap(0)]
    rows, _ = build_frame(_bars(), [], snaps, coin="BTC")
    up = Touch(t=100 * MIN, px=100.0, coin="BTC", side="up", band=0.01, sweep=1,
               mass=2e5, fuel=7e5, brake=4e5, t_snap=0)
    kept_going = [(100 * MIN + 1, 100.0), (100 * MIN + 2 * MIN, 106.0)]      # swept up, kept going up
    data, _ = build_dataset([up], rows, {"BTC": kept_going}, HOUR, MagnetB.features, bar_ns=MIN, k=1.0)
    assert data.y == [1], "a sweep that continued must be y = 1"
    reversed_ = [(100 * MIN + 1, 100.0), (100 * MIN + 2 * MIN, 94.0)]
    data, _ = build_dataset([up], rows, {"BTC": reversed_}, HOUR, MagnetB.features, bar_ns=MIN, k=1.0)
    assert data.y == [0], "a sweep that snapped back must be y = 0"


def test_the_baseline_is_not_negated_and_the_ablation_drops_exactly_the_map():
    assert FuelBaseline().predict([{"imb_fuel": 1.0}, {"imb_fuel": -1.0}, {"imb_fuel": None}]) == [1.0, 0.0, 0.5]
    blind = MagnetB(HOUR).without_map().features
    # The ablation drops the MASS and nothing else: touch_shell and touch_sweep are geometry,
    # and stripping them takes the confound out along with the hypothesis (2026-08-23).
    assert set(blind) == {"touch_shell", "touch_sweep", "ret", "sigma", "sigma_regime",
                          "range_frac", "tau", "liq_flow"}
    assert not {"fuel", "brake", "imb_fuel"} & set(blind)
    assert MagnetB(HOUR).baseline().name == "sign_fuel"


# --- the gate's rule ------------------------------------------------------

def _cell(beats=True, map_earned=True, p=0.001, hit=0.62, thr=0.55, **kw):
    return {"coin": "BTC", "horizon": "1h", "k": 1.0, "n": 3000, "beats_baseline": beats,
            "delta_z": 3.0, "beats_without_map": map_earned, "placebo_p": p,
            "decision_hit_rate": hit, "threshold": thr, **kw}


def test_a_momentum_world_fails_criterion_two_however_good_it_looks():
    """The placebo shuffles map mass and leaves the pre-touch move untouched, so an edge that is
    really momentum passes criteria 1, 3 and 4. Only the map-blind ablation catches it."""
    momentum = [_cell(map_earned=False) for _ in range(4)]
    d = gate.decide(momentum)
    assert d["criteria"]["1_beats_baseline"] and d["criteria"]["3_placebo_collapses"] and d["criteria"]["4_clears_cost"]
    assert not d["criteria"]["2_map_earns_its_place"] and not d["passed"] and d["cells_map_earned"] == 0
    real = [_cell() for _ in range(4)]
    assert gate.decide(real)["passed"] and gate.decide(real)["cells_map_earned"] == 4
    half = [_cell(), _cell(), _cell(map_earned=False), _cell(map_earned=False)]
    assert not gate.decide(half)["criteria"]["2_map_earns_its_place"], "a majority, not a tie"


def test_a_surviving_placebo_or_a_missed_cost_voids_the_result():
    assert not gate.decide([_cell(p=0.3) for _ in range(4)])["criteria"]["3_placebo_collapses"]
    assert not gate.decide([_cell(p=None) for _ in range(4)])["criteria"]["3_placebo_collapses"]
    assert not gate.decide([_cell(hit=0.54) for _ in range(4)])["criteria"]["4_clears_cost"]
    two_of_four = [_cell(), _cell(), _cell(beats=False), _cell(beats=False)]     # 50% < 2/3
    assert not gate.decide(two_of_four)["criteria"]["1_beats_baseline"]
    assert not gate.decide([])["passed"]


def _ledger(tmp_path, *, prereg=PREREG, map_verdict="pass"):
    chain = Ledger(tmp_path / "l.jsonl")
    if prereg:
        chain.append("preregistration", prereg)
    if map_verdict:
        chain.append("gate", {"gate": "map", "passed": map_verdict == "pass",
                              "detail": {"verdict": map_verdict, "coverage": {"BTC": 0.3}}})
    return chain


def never(*a):
    raise AssertionError("cells must not be evaluated on a refusal")


@pytest.mark.parametrize("kw, reason", [
    (dict(prereg=None), "preregistration_missing"),
    (dict(prereg={**PREREG, "min_resolved_touches": 500}), "preregistration_missing"),
    (dict(prereg={**PREREG, "horizons": ["1h", "4h"]}), "preregistration_missing"),
    (dict(map_verdict=None), "upstream_map"),
    (dict(map_verdict="refused"), "upstream_map"),
])
def test_it_refuses_before_counting(tmp_path, kw, reason):
    v = gate.run(_ledger(tmp_path, **kw), [10**18] * 10**4, {"BTC": 0.3}, never, tmp_path)
    assert not v.passed and v.detail["verdict"] == "refused" and v.detail["reason"] == reason


def test_below_the_floors_it_refuses_and_says_how_far_off(tmp_path):
    chain = _ledger(tmp_path)
    day = 86400 * NS
    ts = [chain.entries()[0].ts + i * day for i in range(1, 6)]      # 5 touches, 5 days
    v = gate.run(chain, ts, {"BTC": 0.3, "DOGE": 0.1}, never, tmp_path)
    assert v.detail["reason"] == "insufficient_resolved_touches"
    assert v.detail["window"]["resolved"] == 5 and v.detail["window"]["days"] == 5
    assert "5/2000 touches, 5/30 days" in v.detail["progress"] and v.detail["judged_against"] == [0]


def test_when_runnable_it_evaluates_the_covered_universe_only(tmp_path, monkeypatch):
    chain = _ledger(tmp_path)
    monkeypatch.setattr(gate, "accrual", lambda ts, since: {"resolved": 2500, "need": 2000,
                                                            "days": 31, "need_days": 30, "since_ts": since})
    calls = []
    v = gate.run(chain, [], {"BTC": 0.3, "ETH": 0.26, "DOGE": 0.1},
                 lambda c, h, k: calls.append((c, h, k)) or _cell(coin=c, horizon=h, k=k), tmp_path)
    assert calls == [(c, h, k) for c in ("BTC", "ETH") for h in ("15m", "1h") for k in (1.0, 2.0)]
    assert v.passed and v.detail["verdict"] == "pass" and v.detail["universe"] == ["BTC", "ETH"]


# --- worlds with a known truth, at the horizon's scale ---------------------

def _world(*, mode, n_bars=40000, sigma=0.002, beta=1.4, seed=5, bar_ns=MIN):
    """Bars, tick path and map history where the truth is planted and known.

    The plant is deliberately *not* `sign(F)`: a world whose truth the baseline already
    states perfectly cannot show an expert beating it, which says nothing about either.

    - `"mass"`: the sweep continues in proportion to `F`, **but only at the two inner
      shells**. `sign(F)` is equally confident everywhere and so is wrong half the time on
      the outer ones; an expert that can read `touch_shell` has something real to learn.
      The gate is the shell rather than the fuel magnitude because fuel and `F` are nearly
      collinear -- low fuel *means* `F < 0` -- so gating on fuel would hide the contrast.
    - `"momentum"`: continuation depends on the volatility of the touch bar and not on mass
      at all. Deliberately not the distance travelled: that is proxied by which shell price
      reached, so a map-aware model would "beat" the ablation by reading the confound
      through `touch_shell`, and the world would test nothing.
    - `"null"`: nothing follows a touch.
    """
    rng = random.Random(seed)
    price, snaps, bars_, path, drift, left = 100.0, [], [], [], 0.0, 0
    cur, last = None, {}
    for i in range(n_bars):
        t0 = i * bar_ns
        if i % 10 == 0:
            shells = lambda: [rng.choice((0.0, 0.0, 1.0)) * rng.uniform(1, 4) * 1e6 for _ in range(4)]
            snaps.append(_snap(t0, mark=price, up=tuple(shells()), down=tuple(shells())))
        snap = snaps[-1]
        open_ = price
        for step in range(2):                              # two prints per bar, so bars have range
            price *= math.exp(drift + sigma * rng.gauss(0, 1))
            path.append((t0 + step * NS, price))
        bars_.append(Bar("BTC", t0, t0 + bar_ns, open=open_, high=max(open_, price),
                         low=min(open_, price), close=price, volume=1.0, notional=price,
                         prints=2, available_at=t0 + bar_ns))
        left = max(0, left - 1)
        if left == 0:
            drift = 0.0
        here = shell_of(price / snap["mark"] - 1)
        if here != cur:
            cur = here
            if here and shell_mass(snap, *here) >= 5e4 and t0 - last.get(here, -HOUR) >= HOUR:
                last[here] = t0
                fuel, brake = fuel_and_brake(snap, *here)
                f = (fuel - brake) / (fuel + brake) if fuel + brake else 0.0
                sweep = 1 if here[0] == "up" else -1
                inner = BANDS.index(here[1]) <= 1
                loud = bars_[-1].range_frac > 1.2 * sigma
                signal = {"mass": sweep * f if inner else 0.0,
                          "momentum": sweep * (1.0 if loud else -1.0),
                          "null": 0.0}[mode]
                drift, left = beta * signal * sigma, 30
    return bars_, path, snaps


def _cell_result(mode, horizon_ns=15 * MIN, k=1.0, placebo=0, seed=5):
    """One cell through the real pipeline: expert, ablation, and the covariate-only placebo."""
    from dataclasses import replace

    from nat2.experts.magnet_b import touch_features
    from nat2.features.frame import rebuild_map_columns
    from nat2.validate.evaluate import Comparison
    from nat2.validate.placebo import PlaceboResult, permute_series

    bars_, path, snaps = _world(mode=mode, seed=seed)
    rows, _ = build_frame(bars_, [], snaps, coin="BTC")
    found = touches(path, snaps, "BTC")
    expert = MagnetB(horizon_ns=horizon_ns, min_rows=50)
    data, stats = build_dataset(found, rows, {"BTC": path}, horizon_ns, expert.features, bar_ns=MIN, k=k)
    real = evaluate(expert, data, horizon_ns, Costs(), n_splits=5)
    ablation = expert.without_map()
    blind = evaluate(ablation, data.select(ablation.features), horizon_ns, Costs(), n_splits=5)
    earned = Comparison(expert=real.expert, baseline=blind.expert, threshold=real.threshold,
                        folds=real.folds, costs=real.costs)
    zs = []
    kept = {t.t: t for t in found}
    for i in range(placebo):
        shuffled = permute_series({"BTC": snaps}, seed + i)["BTC"]
        by_ingest = {s["t_ingest"]: s for s in shuffled}
        fake = []
        for row in rebuild_map_columns(data.rows, shuffled):
            touch = kept.get(row["t_decision"])
            snap = by_ingest.get(touch.t_snap) if touch else None
            if snap is None:
                fake.append(dict(row))
                continue
            fuel, brake = fuel_and_brake(snap, touch.side, touch.band)
            fake.append({**row, **touch_features(replace(touch, fuel=fuel, brake=brake))})
        shuffled_data = data.with_rows(fake)
        full = evaluate(expert, shuffled_data, horizon_ns, Costs(), n_splits=5)
        null = evaluate(ablation, shuffled_data.select(ablation.features), horizon_ns, Costs(), n_splits=5)
        zs.append(Comparison(expert=full.expert, baseline=null.expert, threshold=full.threshold,
                             folds={}, costs={}).delta_z)
    return {"touches": len(found), "labelled": stats.labelled, "beats_baseline": real.beats_baseline,
            "delta_z": real.delta_z, "beats_without_map": earned.beats_baseline,
            "z_vs_blind": earned.delta_z,
            "placebo": PlaceboResult(earned.delta_z, zs) if zs else None,
            "placebo_p": PlaceboResult(earned.delta_z, zs).p_value if zs else None}


def _decides(r, n=4, p=None):
    """The gate's own rule on four cells that all read like this one."""
    cell = _cell(beats=r["beats_baseline"], map_earned=r["beats_without_map"],
                 p=p if p is not None else (r["placebo_p"] if r["placebo_p"] is not None else 1.0))
    return gate.decide([cell] * n)


def test_a_planted_mass_world_is_found_and_the_map_earns_its_place():
    r = _cell_result("mass", placebo=40)
    assert r["labelled"] > 200, r
    assert r["beats_baseline"] and r["delta_z"] >= 2.0, r      # criterion 1
    assert r["beats_without_map"] and r["z_vs_blind"] >= 2.0, r  # criterion 2: the mass carries it
    # Criterion 3: no permutation of the mass reproduces that margin. Production runs 200
    # replications, where zero exceedances is p = 0.005; 40 is what a test can afford.
    assert r["placebo"].exceeded == 0, r["placebo"].summary()
    # 40 replications cannot report better than p = 1/41, so the gate is asked the question at
    # the registered count: zero exceedances in 200 is p = 0.005, which clears 0.01.
    assert _decides(r, p=1 / (gate.PLACEBO_REPLICATIONS + 1))["passed"], r


def test_a_momentum_world_is_stopped_by_the_placebo_even_though_it_looks_good():
    """Continuation follows the touch bar's volatility, not the mass. Criterion 1 passes at
    z +9.8 -- that is the point of the world -- and criterion 2 is only marginal, because the
    mass columns still correlate with how far price travelled. The conjunction is what saves
    the claim: shuffling the mass does not destroy that margin, so criterion 3 refuses."""
    r = _cell_result("momentum", placebo=40)
    assert r["labelled"] > 200 and r["beats_baseline"], r
    assert r["placebo_p"] > gate.PLACEBO_ALPHA, r["placebo"].summary()
    assert not _decides(r)["passed"], r
    assert not _decides(r)["criteria"]["3_placebo_collapses"], r


def test_a_null_world_yields_nothing():
    r = _cell_result("null", placebo=0)
    assert r["labelled"] > 200 and not r["beats_baseline"] and not r["beats_without_map"], r
    assert not _decides(r)["passed"]
