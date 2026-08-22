"""Synthetic worlds with a known truth, pushed through the real pipeline over weeks of bars.

The unit tests on hand-built snapshots check arithmetic. These check power and restraint at
scale: frame -> race labels -> purged walk-forward -> isotonic -> permutation placebo ->
`decide()`, on histories long enough for the horizon -- four weeks of 15-minute bars at the
1h/4h cells, four years of daily bars at a one-week horizon. A planted magnet must be found
and must collapse under permutation; a driftless world must yield nothing; and the weekly
horizon must show what a +-5% map can and cannot resolve at that scale.

Placebo replications reuse the labels: barriers are placed from volatility alone, so a
permuted map changes features and nothing else. `test_feature_rebuild_equals_the_full_frame`
pins that shortcut to the production frame, so it is a check on the frame, not a bypass.
"""

import math
import random

import pytest

from nat2.core.clock import NS
from nat2.core.costs import Costs
from nat2.experts.base import Dataset, to_matrix
from nat2.experts.magnet_a import MagnetA, build_dataset
from nat2.experts.magnet_alpha import D_MIN, MagnetAlpha, asymmetry, shell_distances
from nat2.features.bars import Bar
from nat2.features.frame import _map_features, build
from nat2.gates import magnet as gate
from nat2.validate.evaluate import evaluate
from nat2.validate.placebo import PlaceboResult, permute_series

MIN, HOUR = 60 * NS, 3600 * NS
DAY, WEEK = 24 * 3600 * NS, 7 * 24 * 3600 * NS
BANDS = ("0.005", "0.01", "0.02", "0.05")
COIN = "BTC"


# --- the world ---------------------------------------------------------------

def _snapshot(t_ingest, shells_up, shells_dn) -> dict:
    """A persisted-form map: cumulative bands per side, as io/mapsnap.summarise writes them."""
    up, dn, cu, cd = {}, {}, 0.0, 0.0
    for band, mu, md in zip(BANDS, shells_up, shells_dn):
        cu, cd = cu + mu, cd + md
        up[band], dn[band] = cu, cd
    imb = {b: (dn[b] - up[b]) / (dn[b] + up[b]) if dn[b] + up[b] else 0.0 for b in BANDS}
    return {"t_ingest": t_ingest, "coin": COIN, "coverage": 0.3, "published_frac": 0.9, "mark": 100.0,
            "up": up, "down": dn, "imb": imb, "imb_cross": dict(imb),
            "near": {"up_dist": 0.005, "down_dist": -0.005}}


class World:
    """Bars, tick path and map history; price drifts toward the planted kernel's heavier side.

    `beta` is the pull per bar in units of sigma: the return is `-beta * A_true * sigma + noise`,
    sign matching imbalance() (positive A = more mass below = pull down). `beta = 0` is the null.
    """

    def __init__(self, *, bar_ns, n_bars, sigma, beta, pull_horizon_ns, snap_every, seed, alpha_true=2.0):
        rng = random.Random(seed)
        dist = shell_distances(sigma, pull_horizon_ns, bar_ns)
        self.bar_ns, self.bars, self.path, self.maps = bar_ns, [], [], []
        price, shells_up, shells_dn = 100.0, None, None
        for i in range(n_bars):
            t_open, t_close = i * bar_ns, (i + 1) * bar_ns
            if i % snap_every == 0:
                draw = lambda: [rng.choice((0.0, 0.0, 1.0)) * rng.uniform(0.5, 3.0) * 1e6 for _ in BANDS]
                shells_up, shells_dn = draw(), draw()
                self.maps.append(_snapshot(t_open, shells_up, shells_dn))
            pull = asymmetry(shells_dn, shells_up, dist, alpha_true)
            close = price * math.exp(-beta * pull * sigma + sigma * rng.gauss(0.0, 1.0))
            self.bars.append(Bar(COIN, t_open, t_close, open=price, high=max(price, close), low=min(price, close),
                                 close=close, volume=1.0, notional=close, prints=1, available_at=t_close))
            self.path.append((t_close, close))
            price = close


def _labelled(world, horizon_ns, k):
    rows, _ = build(world.bars, [], world.maps, coin=COIN)
    data, _ = build_dataset(rows, {COIN: world.path}, horizon_ns, MagnetA.features, bar_ns=world.bar_ns, k=k)
    return rows, data


def _snap_index(rows, maps):
    """For each frame row, the index of the snapshot the as-of join picked. Stable under permutation."""
    out, j = [], -1
    for row in rows:
        while j + 1 < len(maps) and maps[j + 1]["t_ingest"] <= row["t_decision"]:
            j += 1
        out.append(j)
    return out


def _rebuilt(rows, data, maps, snap_index):
    """`data` with only its map columns recomputed from `maps` -- labels and weights untouched."""
    at = {row["t_decision"]: i for i, row in enumerate(rows)}
    kept = []
    for row in data.rows:
        i = at[row["t_decision"]]
        r = dict(rows[i])
        r.update(_map_features(maps[snap_index[i]], r["t_decision"], r["sigma"], r.get("day_volume")))
        kept.append(r)
    return Dataset(data.columns, to_matrix(kept, data.columns), data.y, data.weight, kept)


def _cell(world, horizon, k, placebo=0, seed=0):
    """One cell in the shape `gates.magnet.cell_evaluator` returns, alpha kernels only."""
    h = gate.parse_window(horizon)
    rows, data = _labelled(world, h, k)
    experts = [MagnetAlpha(a, h, world.bar_ns) for a in gate.ALPHAS]
    real = [evaluate(e, data, h, Costs(), n_splits=5) for e in experts]
    index = _snap_index(rows, world.maps)
    zs = [[] for _ in experts]
    for i in range(placebo):
        fake = _rebuilt(rows, data, permute_series({COIN: world.maps}, seed + i)[COIN], index)
        for e, z in zip(experts, zs):
            z.append(evaluate(e, fake, h, Costs(), n_splits=5).delta_z)

    def row(r, z, **head):
        v = r.verdict()
        return {**head, "n": v["expert"]["n"], "beats_baseline": r.beats_baseline, "delta_z": r.delta_z,
                "placebo_p": PlaceboResult(r.delta_z, z).p_value if z else None,
                "decision_hit_rate": v["expert"]["decision_hit_rate"], "threshold": v["threshold"],
                "placebo_mean_z": sum(z) / len(z) if z else None, "log_loss": v["expert"]["log_loss"],
                "baseline_log_loss": v["baseline"]["log_loss"], "constant": v["constant_log_loss"]}
    # The MagnetA slot is filled with the alpha = 2 result: these tests are about criterion 2.
    return {**row(real[1], zs[1], coin=COIN, horizon=horizon, k=k),
            "alpha": [row(r, z, alpha=a) for r, z, a in zip(real, zs, gate.ALPHAS)]}


@pytest.fixture(scope="module")
def planted():
    # Four weeks of 15-minute bars; a new map every 4h; a strong pull toward the alpha = 2 kernel.
    return World(bar_ns=15 * MIN, n_bars=28 * 96, sigma=0.002, beta=0.6, pull_horizon_ns=HOUR, snap_every=16, seed=7)


@pytest.fixture(scope="module")
def null():
    return World(bar_ns=15 * MIN, n_bars=28 * 96, sigma=0.002, beta=0.0, pull_horizon_ns=HOUR, snap_every=16, seed=7)


# --- the frame shortcut is the frame ------------------------------------------

def test_feature_rebuild_equals_the_full_frame(planted):
    rows, data = _labelled(planted, HOUR, 1.0)
    again = _rebuilt(rows, data, planted.maps, _snap_index(rows, planted.maps))
    assert again.rows == data.rows and again.y is data.y and len(again.X) == len(data.X)   # X is rows, via to_matrix
    permuted = permute_series({COIN: planted.maps}, 1)[COIN]
    full, _ = build(planted.bars, [], permuted, coin=COIN)
    full_data, _ = build_dataset(full, {COIN: planted.path}, HOUR, MagnetA.features, bar_ns=planted.bar_ns, k=1.0)
    fast = _rebuilt(rows, data, permuted, _snap_index(rows, planted.maps))
    assert fast.rows == full_data.rows and fast.y == full_data.y and fast.weight == full_data.weight


# --- power: a planted magnet is found, and it is mass, not geometry ----------

def test_planted_magnet_is_found_at_both_gating_horizons_and_collapses_under_permutation(planted):
    cells = [_cell(planted, h, 1.0, placebo=100, seed=11) for h in gate.CELLS_T]
    for c in cells:
        two = next(a for a in c["alpha"] if a["alpha"] == 2.0)
        assert two["beats_baseline"] and two["delta_z"] >= 2.0, (c["horizon"], two)
        assert two["log_loss"] < two["baseline_log_loss"] < two["constant"] + 0.01, (c["horizon"], two)
        assert two["placebo_p"] <= gate.PLACEBO_ALPHA and abs(two["placebo_mean_z"]) < 1.5, (c["horizon"], two)
        assert two["decision_hit_rate"] > two["threshold"]
    d = gate.decide(cells)
    assert d["criteria"]["2_alpha_kernel"] and d["alpha"]["winning_alpha"] == 2.0 and d["alpha"]["clears_cost"]
    assert d["alpha"]["wins_by_alpha"]["2"] == 2


def test_planted_magnet_ranks_the_true_kernel_above_the_other(planted):
    c = _cell(planted, "1h", 1.0)
    one, two = (next(a for a in c["alpha"] if a["alpha"] == x) for x in (1.0, 2.0))
    assert two["log_loss"] < one["log_loss"]


# --- restraint: a driftless world yields nothing ------------------------------

def test_driftless_world_passes_no_criterion(null):
    cells = [_cell(null, h, k) for h in gate.CELLS_T for k in gate.CELLS_K]
    results = [a for c in cells for a in c["alpha"]]
    # No systematic edge: mean z near zero, log loss at the constant floor -- *slightly below* it,
    # because validate.evaluate fits the isotonic map on the same OOS rows it then scores. That
    # optimism (~0.01-0.02 here) is a property of the evaluator, pinned so that a cross-fitted
    # calibration has to move this bound on purpose. `beats_constant` is not a floor until it does.
    assert abs(sum(a["delta_z"] for a in results) / len(results)) < 1.0
    assert -0.025 < sum(a["log_loss"] - a["constant"] for a in results) / len(results) < 0.005
    # A chance win at z ~ 2 in one cell of eight is expected; a majority for one alpha is not.
    for alpha in gate.ALPHAS:
        assert sum(1 for a in results if a["alpha"] == alpha and a["beats_baseline"]) <= 1
    d = gate.decide(cells)
    assert not d["criteria"]["2_alpha_kernel"] and not d["passed"] and d["alpha"]["winning_alpha"] is None


def test_a_chance_win_in_a_driftless_world_does_not_survive_the_placebo(null):
    # Seed 7's null world hands alpha = 2 a z of ~2.0 at (1h, k=1) by luck. The permutation null
    # reproduces that size of z often enough that it cannot count as a win at p <= 0.01.
    c = _cell(null, "1h", 1.0, placebo=100, seed=5)
    two = next(a for a in c["alpha"] if a["alpha"] == 2.0)
    assert two["beats_baseline"] and 1.5 < two["delta_z"] < 2.5, two        # the chance win, reproduced
    assert two["placebo_p"] > gate.PLACEBO_ALPHA, two
    assert gate.decide([c])["alpha"]["wins_by_alpha"] == {"1": 0, "2": 0}


# --- weeks: what the map can say at a one-week horizon ----------------------

def test_a_week_horizon_is_readable_when_the_map_span_covers_sigma_sqrt_T():
    # Four years of daily bars at 0.5%/day: sigma*sqrt(7) = 1.3%, so the four shells stay distinct.
    world = World(bar_ns=DAY, n_bars=4 * 365, sigma=0.005, beta=0.6, pull_horizon_ns=WEEK, snap_every=7, seed=3)
    assert shell_distances(0.005, WEEK, DAY)[1:] == pytest.approx([0.567, 1.134, 2.646], abs=1e-3)
    c = _cell(world, "168h", 1.0)
    two = next(a for a in c["alpha"] if a["alpha"] == 2.0)
    assert two["beats_baseline"] and two["delta_z"] >= 2.0, two
    assert two["n"] < gate.MIN_EVENTS      # and still below the pre-registered floor: years are not enough


def test_at_btc_volatility_a_week_horizon_erases_the_map_resolution():
    # 3%/day -> sigma*sqrt(7) ~ 8%: the inner three shells all sit at d_min, so 0.25% away reads the
    # same as 1.5% away and alpha cannot separate them. Weeks need a wider persisted span, not a new alpha.
    d = shell_distances(0.03, WEEK, DAY)
    assert d[:3] == [D_MIN] * 3 and d[3] < 0.5
    masses = {"m_dn_0005": 5e6, "m_dn_001": 0.0, "m_dn_002": 0.0, "m_dn_005": 0.0,
              "m_up_0005": 0.0, "m_up_001": 0.0, "m_up_002": 4e6, "m_up_005": 0.0}
    daily = {"sigma": 0.03, **masses}                      # 3% per daily bar, one-week horizon
    assert MagnetAlpha(1, WEEK, DAY).score(daily) == MagnetAlpha(2, WEEK, DAY).score(daily) == pytest.approx(1e6 / 9e6)
    minute = {"sigma": 0.0005, **masses}                   # 5bp per minute bar, one-hour horizon: shells resolve
    assert MagnetAlpha(2, HOUR, MIN).score(minute) > MagnetAlpha(1, HOUR, MIN).score(minute) > 1e6 / 9e6


# --- the kernel's shape: larger and closer count for more --------------------

@pytest.mark.parametrize("alpha", [1.0, 2.0])
def test_closer_mass_pulls_harder_and_larger_mass_pulls_harder(alpha):
    d = [0.25, 0.75, 1.5, 3.5]
    near = asymmetry([1e6, 0, 0, 0], [0, 0, 0, 2e6], d, alpha)
    far = asymmetry([0, 0, 0, 1e6], [0, 0, 0, 2e6], d, alpha)
    assert near > 0 > far                      # the same mass wins when near and loses when far
    assert asymmetry([2e6, 0, 0, 0], [0, 0, 0, 2e6], d, alpha) > near
    assert asymmetry([1e6, 0, 0, 0], [0, 0, 0, 2e6], d, 0.0) == pytest.approx(-1 / 3)   # alpha = 0 is blind to distance


def test_sign_convention_mass_below_pulls_down():
    e = MagnetAlpha(2, HOUR, MIN)
    below = {"sigma": 0.001, **{f"m_dn_{s}": 1e6 for s in ("0005", "001", "002", "005")}, **{f"m_up_{s}": 0.0 for s in ("0005", "001", "002", "005")}}
    assert e.score(below) == 1.0 and e.predict([below]) == [0.0]      # p(up-barrier first) = 0
