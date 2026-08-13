"""The permutation placebo, and the ways it could fail to be a control.

Its whole job is to destroy mass structure while leaving location structure
intact. A placebo that leaks the real map through some derived column is worse
than no placebo, because it would make a geometric artefact look like it had
survived a test.
"""

from __future__ import annotations

import random

import pytest

from nat2.validate.placebo import (
    BANDS,
    PlaceboResult,
    permute_series,
    permute_snapshot,
)


def _snap(up=None, down=None, coin="BTC", t=1) -> dict:
    up = up or {"0.005": 1.0, "0.01": 2.0, "0.02": 3.0, "0.05": 4.0}
    down = down or {"0.005": 10.0, "0.01": 20.0, "0.02": 30.0, "0.05": 40.0}
    total_up = sum(up.values())
    return {
        "t_ingest": t, "coin": coin, "mark": 100.0,
        "up": dict(up), "down": dict(down),
        "imb": {b: 0.9 for b in BANDS},
        "imb_cross": {b: 0.5 for b in BANDS},
        "near": {"up_px": 101.0, "down_px": 99.0, "up_dist": 0.01,
                 "down_dist": -0.01, "up_notional": total_up, "down_notional": 5.0},
        "coverage": 0.3,
    }


# --- what must be preserved -----------------------------------------------

def test_the_multiset_of_masses_is_preserved_exactly():
    # Shuffling must break the pairing, never change the sizes: the size
    # distribution is half of what §5 requires be held fixed.
    snap = _snap()
    before = sorted(list(snap["up"].values()) + list(snap["down"].values()))
    out = permute_snapshot(snap, random.Random(0))
    after = sorted(list(out["up"].values()) + list(out["down"].values()))
    assert before == after


def test_every_slot_still_exists():
    out = permute_snapshot(_snap(), random.Random(0))
    assert set(out["up"]) == set(BANDS) and set(out["down"]) == set(BANDS)


def test_the_locations_are_untouched():
    # Location structure is the thing the placebo must NOT destroy -- it is the
    # confound being controlled for, not the thing being tested.
    out = permute_snapshot(_snap(), random.Random(0))
    assert out["near"]["up_px"] == 101.0 and out["near"]["down_px"] == 99.0
    assert out["near"]["up_dist"] == 0.01 and out["near"]["down_dist"] == -0.01
    assert out["mark"] == 100.0 and out["coverage"] == 0.3


# --- what must be destroyed -----------------------------------------------

def test_the_pairing_actually_changes():
    snap = _snap()
    outs = [permute_snapshot(snap, random.Random(s)) for s in range(20)]
    assert any(o["up"] != snap["up"] or o["down"] != snap["down"] for o in outs)


def test_imbalance_is_recomputed_not_carried_over():
    # A stale imb would leak the real map straight through the placebo and
    # make a geometric artefact look like it survived the control.
    out = permute_snapshot(_snap(), random.Random(1))
    for band in BANDS:
        up, down = out["up"][band], out["down"][band]
        expected = (down - up) / (down + up) if (down + up) else 0.0
        assert out["imb"][band] == pytest.approx(expected)
    assert out["imb"] != {b: 0.9 for b in BANDS}


def test_a_band_with_no_mass_gives_zero_imbalance_not_a_division_error():
    out = permute_snapshot(_snap(up={b: 0.0 for b in BANDS},
                                 down={b: 0.0 for b in BANDS}), random.Random(0))
    assert all(out["imb"][b] == 0.0 for b in BANDS)


# --- determinism -----------------------------------------------------------

def test_the_same_seed_gives_the_same_permutation():
    # Replications have to be reproducible or the p-value is not checkable.
    a = permute_series({"BTC": [_snap(t=1), _snap(t=2)]}, seed=7)
    b = permute_series({"BTC": [_snap(t=1), _snap(t=2)]}, seed=7)
    assert a == b


def test_different_seeds_give_different_permutations():
    a = permute_series({"BTC": [_snap(t=i) for i in range(10)]}, seed=1)
    b = permute_series({"BTC": [_snap(t=i) for i in range(10)]}, seed=2)
    assert a != b


def test_each_snapshot_is_permuted_independently():
    out = permute_series({"BTC": [_snap(t=i) for i in range(30)]}, seed=3)["BTC"]
    assert len({tuple(sorted(s["up"].items())) for s in out}) > 1


def test_the_original_series_is_not_mutated():
    original = {"BTC": [_snap()]}
    snapshot_of_input = {"up": dict(original["BTC"][0]["up"]),
                         "imb": dict(original["BTC"][0]["imb"])}
    permute_series(original, seed=5)
    assert original["BTC"][0]["up"] == snapshot_of_input["up"]
    assert original["BTC"][0]["imb"] == snapshot_of_input["imb"]


def test_an_empty_history_permutes_to_an_empty_history():
    assert permute_series({}, seed=0) == {}
    assert permute_series({"BTC": []}, seed=0) == {"BTC": []}


def test_a_snapshot_missing_its_bands_does_not_crash():
    out = permute_snapshot({"t_ingest": 1, "coin": "BTC"}, random.Random(0))
    assert set(out["up"]) == set(BANDS)


# --- the verdict -----------------------------------------------------------

def test_an_effect_that_survives_the_placebo_does_not_collapse():
    # Every replication matched the real effect: it was geometry.
    result = PlaceboResult(real_z=3.0, placebo_z=[3.1] * 99)
    assert result.exceeded == 99
    assert not result.collapses()


def test_an_effect_that_vanishes_under_the_placebo_collapses():
    result = PlaceboResult(real_z=4.0, placebo_z=[0.1] * 99)
    assert result.exceeded == 0
    assert result.p_value == pytest.approx(0.01)
    assert result.collapses(alpha=0.01)


def test_the_p_value_never_reports_zero():
    # An add-one estimate, because "no placebo beat it" out of 99 is not
    # evidence of an impossible event.
    result = PlaceboResult(real_z=9.0, placebo_z=[0.0] * 9)
    assert result.p_value == pytest.approx(0.1)
    assert not result.collapses(alpha=0.01), "ten replications cannot reach p<0.01"


def test_no_replications_means_no_verdict():
    result = PlaceboResult(real_z=5.0, placebo_z=[])
    assert result.p_value == 1.0 and not result.collapses()


def test_ties_count_against_the_effect():
    # A placebo that merely matches is not evidence for the hypothesis.
    result = PlaceboResult(real_z=2.0, placebo_z=[2.0] * 9)
    assert result.exceeded == 9


def test_the_summary_carries_the_distribution_not_just_the_verdict():
    result = PlaceboResult(real_z=3.0, placebo_z=[0.0, 1.0, 2.0])
    summary = result.summary()
    assert summary["max_placebo_z"] == 2.0
    assert summary["mean_placebo_z"] == pytest.approx(1.0)
    assert summary["replications"] == 3
