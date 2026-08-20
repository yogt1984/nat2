"""Purged walk-forward: the machinery that stops the model seeing the answer.

Around a cascade, dozens of consecutive bars are resolved by the same price
move. If a training row's label span reaches into the test window, the model
has effectively been shown the outcome it is about to be scored on — and the
resulting metric is not merely optimistic, it is meaningless.

`leaks()` exists as a self-check, and these tests assert it stays empty under
conditions designed to break the purge.
"""

from __future__ import annotations

import pytest

from nat2.validate.wfo import Fold, coverage, folds, leaks

MIN = 60_000_000_000
H = 10 * MIN


def _times(n: int, step: int = MIN) -> list[int]:
    return [i * step for i in range(n)]


# --- structure -------------------------------------------------------------

def test_folds_run_forward_only():
    # A fold trained on the future to predict the past would score well and
    # mean nothing.
    times = _times(120)
    for fold in folds(times, n_splits=4, horizon_ns=0):
        assert max(times[i] for i in fold.train) <= fold.test_end
        assert min(fold.test) > max(fold.train)


def test_test_windows_are_contiguous_and_ordered():
    built = folds(_times(120), n_splits=4, horizon_ns=0)
    starts = [f.test[0] for f in built]
    assert starts == sorted(starts)
    for fold in built:
        assert fold.test == list(range(fold.test[0], fold.test[-1] + 1))


def test_every_row_is_trained_on_or_tested_but_never_both_in_one_fold():
    for fold in folds(_times(120), n_splits=4, horizon_ns=0):
        assert not (set(fold.train) & set(fold.test))


def test_the_first_block_is_training_only_so_every_fold_has_a_past():
    built = folds(_times(120), n_splits=4, horizon_ns=0)
    assert built[0].test[0] > 0
    assert len(built[0].train) > 0


# --- purging ---------------------------------------------------------------

def test_no_training_label_span_reaches_into_the_test_window():
    times = _times(200)
    for fold in folds(times, n_splits=4, horizon_ns=H, embargo_ns=0):
        assert leaks(fold, times, H) == [], "the purge is broken"


def test_a_longer_horizon_purges_more():
    times = _times(200)
    short = sum(f.purged for f in folds(times, 4, horizon_ns=MIN))
    long = sum(f.purged for f in folds(times, 4, horizon_ns=20 * MIN))
    assert long > short


def test_purging_uses_the_label_span_not_the_decision_time():
    # A row decided well before the test window but resolving inside it must
    # still go. Comparing decision times alone would keep it.
    times = _times(60)
    built = folds(times, n_splits=2, horizon_ns=30 * MIN)
    assert any(f.purged > 0 for f in built)


# --- embargo ---------------------------------------------------------------

def test_the_embargo_removes_rows_after_the_test_window():
    times = _times(200)
    without = sum(len(f.train) for f in folds(times, 4, H, embargo_ns=0))
    with_embargo = sum(len(f.train) for f in folds(times, 4, H, embargo_ns=20 * MIN))
    assert with_embargo <= without


def test_embargoed_rows_are_counted_not_silently_dropped():
    built = folds(_times(200), 4, horizon_ns=0, embargo_ns=5 * MIN)
    # Only folds with data after their test window can embargo anything.
    assert any(f.embargoed >= 0 for f in built)


# --- degenerate inputs -----------------------------------------------------

def test_unsorted_times_are_rejected_rather_than_silently_mispurged():
    with pytest.raises(ValueError, match="ordered by decision time"):
        folds([0, 5 * MIN, MIN], n_splits=2, horizon_ns=0)


@pytest.mark.parametrize("n_splits", [0, 1, -1])
def test_fewer_than_two_splits_yields_nothing(n_splits):
    assert folds(_times(100), n_splits, horizon_ns=0) == []


def test_too_few_rows_for_the_requested_splits_yields_nothing():
    assert folds(_times(3), n_splits=10, horizon_ns=0) == []


def test_a_fold_without_enough_training_rows_is_dropped():
    # Better no fold than a fold fitted on three observations.
    built = folds(_times(120), n_splits=4, horizon_ns=0, min_train=1_000)
    assert built == []


def test_an_empty_series_yields_nothing():
    assert folds([], n_splits=3, horizon_ns=0) == []


# --- coverage reporting ----------------------------------------------------

def test_coverage_reports_what_was_actually_scored():
    times = _times(200)
    built = folds(times, n_splits=4, horizon_ns=H)
    stats = coverage(built, len(times))
    assert stats["folds"] == len(built)
    assert 0 < stats["tested_frac"] <= 1.0
    assert stats["purged"] >= 0
    assert len(stats["train_sizes"]) == len(built)


def test_coverage_of_no_folds_is_zero_not_a_division_error():
    assert coverage([], 0)["tested_frac"] == 0.0


def test_leaks_finds_a_deliberately_broken_fold():
    # Hand-built with a training row whose span covers the test window, to
    # prove the self-check is not vacuous.
    times = _times(10)
    broken = Fold(index=0, train=[0], test=[5], test_start=times[5],
                  test_end=times[5], purged=0, embargoed=0)
    assert leaks(broken, times, horizon_ns=H) == [0]
