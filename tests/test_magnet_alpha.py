"""TASK_2/09: the alpha kernel, tested against the text of ledger seq 153 -- not the other way round.
Golden values are hand-computed from §1.2 on a two-shell-per-side snapshot; frame shells are checked as
differences of the cumulative bands; `decide()` is checked for the trap the entry names: best-alpha-per-cell."""

import pytest

from nat2.core.clock import NS
from nat2.experts.magnet_alpha import D_MIN, MagnetAlpha, asymmetry, shell_distances
from nat2.features.frame import _map_features
from nat2.features.spec import undeclared
from nat2.gates import magnet as gate
from test_gate_magnet import arow, cell  # synthetic-cell helpers; tests/ is on sys.path under pytest

H, BAR = 3600 * NS, 60 * NS
SIGMA = 0.01 / 60 ** 0.5           # sigma * sqrt(60 bars) = 1%  ->  d = (0.25, 0.75, 1.5, 3.5)
# Cumulative bands: below has 100 in the 0.25% shell and 50 in the 1.5% shell; above has 200 in the
# 0.75% shell and 1000 in the 3.5% shell. Near-and-small below, far-and-large above.
SNAP = {"t_ingest": 0, "up": {"0.005": 0.0, "0.01": 200.0, "0.02": 200.0, "0.05": 1200.0},
        "down": {"0.005": 100.0, "0.01": 100.0, "0.02": 150.0, "0.05": 150.0},
        "imb": {"0.05": (150.0 - 1200.0) / 1350.0}}


def row(snap=SNAP, sigma=SIGMA) -> dict:
    return {"sigma": sigma, **_map_features(snap, 0, sigma, None)}


def test_frame_shells_are_differences_of_the_cumulative_bands_floored_at_zero():
    r = row()
    assert [r[f"m_up_{s}"] for s in ("0005", "001", "002", "005")] == [0.0, 200.0, 0.0, 1000.0]
    assert [r[f"m_dn_{s}"] for s in ("0005", "001", "002", "005")] == [100.0, 0.0, 50.0, 0.0]
    assert undeclared(r) == set()
    shuffled = {**SNAP, "up": {"0.005": 500.0, "0.01": 100.0, "0.02": 100.0, "0.05": 100.0}}  # a placebo can do this
    assert row(shuffled)["m_up_001"] == 0.0 and row(shuffled)["m_up_0005"] == 500.0
    partial = {**SNAP, "up": {"0.02": 200.0}}
    assert row(partial)["m_up_0005"] is None and row(partial)["m_up_002"] is None


def test_alpha_zero_is_the_five_percent_imbalance_and_alpha_two_flips_it():
    r = row()
    assert shell_distances(SIGMA, H, BAR) == pytest.approx([0.25, 0.75, 1.5, 3.5])
    assert MagnetAlpha(0, H, BAR).score(r) == pytest.approx(SNAP["imb"]["0.05"]) and SNAP["imb"]["0.05"] < 0
    # alpha = 2: below 100/0.25^2 + 50/1.5^2 = 1622.2; above 200/0.75^2 + 1000/3.5^2 = 437.2
    assert MagnetAlpha(2, H, BAR).score(r) == pytest.approx((1622.2222 - 437.1882) / (1622.2222 + 437.1882), abs=1e-4)
    assert MagnetAlpha(2, H, BAR).predict([r]) == [pytest.approx(0.5 * (1 - 0.5754), abs=1e-3)]
    assert MagnetAlpha(1, H, BAR).score(r) == pytest.approx(asymmetry([100, 0, 50, 0], [0, 200, 0, 1000], [0.25, 0.75, 1.5, 3.5], 1))


def test_the_floor_engages_and_erases_alpha_when_everything_is_within_d_min():
    assert shell_distances(SIGMA * 100, H, BAR) == [D_MIN] * 4
    r = row(sigma=SIGMA * 100)
    assert MagnetAlpha(1, H, BAR).score(r) == MagnetAlpha(2, H, BAR).score(r) == pytest.approx(SNAP["imb"]["0.05"])
    assert shell_distances(SIGMA, 4 * H, BAR)[0] == D_MIN and shell_distances(SIGMA, 4 * H, BAR)[1] < 0.75


def test_abstains_at_one_half_on_an_empty_map_no_map_or_no_sigma():
    empty = {**SNAP, "up": {b: 0.0 for b in SNAP["up"]}, "down": {b: 0.0 for b in SNAP["down"]}}
    assert MagnetAlpha(2, H, BAR).predict([row(empty)]) == [0.5]
    assert MagnetAlpha(2, H, BAR).predict([{"sigma": SIGMA}, {**row(), "sigma": None}, {**row(), "m_dn_002": None}]) == [0.5] * 3
    assert asymmetry([0, 0, 0, 0], [0, 0, 0, 0], [1, 1, 1, 1], 2) == 0.0


def test_protocol_nothing_to_fit_declared_features_mandated_baseline():
    e = MagnetAlpha(1, H, BAR)
    assert e.fit(None) is e and e.baseline().name == "neg_imbalance" and e.lookback() == 30
    assert e.name == "magnet_alpha:1" and undeclared(e.features) == set() and "sigma" in e.features
    with pytest.raises(ValueError):
        MagnetAlpha(-1, H, BAR)


def cells_with(*alpha_rows_per_cell):
    return [cell(h=h, k=k, alpha=list(rows)) for (h, k), rows in zip([(h, k) for h in ("1h", "4h") for k in (1.0, 2.0)], alpha_rows_per_cell)]


def test_criterion_two_needs_one_alpha_in_a_strict_majority():
    win, lose = arow(1.0), arow(2.0, beats=False)
    three_of_four = cells_with((win, lose), (win, lose), (win, lose), (arow(1.0, beats=False), lose))
    d = gate.decide(three_of_four)
    assert d["criteria"]["2_alpha_kernel"] and d["alpha"]["winning_alpha"] == 1.0 and d["passed"]
    two_of_four = cells_with((win, lose), (win, lose), (arow(1.0, beats=False), lose), (arow(1.0, beats=False), lose))
    assert not gate.decide(two_of_four)["criteria"]["2_alpha_kernel"]


def test_best_alpha_per_cell_is_selection_not_a_win():
    a1, a2 = arow(1.0), arow(2.0)
    n1, n2 = arow(1.0, beats=False), arow(2.0, beats=False)
    split = cells_with((a1, n2), (a1, n2), (n1, a2), (n1, a2))   # every cell has *a* winning alpha
    d = gate.decide(split)
    assert d["alpha"]["wins_by_alpha"] == {"1": 2, "2": 2} and not d["criteria"]["2_alpha_kernel"] and not d["passed"]
    assert d["would_pass_on_1_3_4"]                                 # criteria 1, 3, 4 are fine; 2 is the blocker


def test_an_alpha_win_that_survives_the_placebo_or_misses_cost_does_not_count():
    leaky = cells_with(*[(arow(1.0, p=0.3), arow(2.0, beats=False))] * 4)
    assert gate.decide(leaky)["alpha"]["wins_by_alpha"] == {"1": 0, "2": 0}
    unplaceboed = cells_with(*[(arow(1.0, p=None), arow(2.0, beats=False))] * 4)
    assert not gate.decide(unplaceboed)["criteria"]["2_alpha_kernel"]
    costly = cells_with(*[(arow(1.0, hit=0.54), arow(2.0, beats=False))] * 4)
    d = gate.decide(costly)
    assert d["criteria"]["2_alpha_kernel"] and not d["alpha"]["clears_cost"] and not d["passed"]
