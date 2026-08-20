"""Purged walk-forward with an embargo.

A plain time split leaks. A label decided at time `t` is not resolved until
`t + h`, so a training row whose *span* reaches into the test window was
partly determined by the same price move the test row is being scored on.
Around a cascade, where dozens of consecutive bars are resolved by one sweep,
that overlap is not a rounding error — it is most of the sample.

Two removals, and they do different jobs:

*Purge* drops training rows whose label span `[t, t + h]` intersects the test
window. Without it the model has effectively seen the answer.

*Embargo* drops training rows for a further interval **after** the test window.
Purging handles labels that reach forward into the test period; the embargo
handles serial correlation reaching backward out of it — features are
autocorrelated, so a row immediately after the test window still carries the
test window's information even though its label does not overlap.

Folds run forward only. A fold trained on the future to predict the past would
score well and mean nothing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Fold:
    index: int
    train: list[int]
    test: list[int]
    test_start: int
    test_end: int
    purged: int      # training rows removed for overlapping the test window
    embargoed: int   # training rows removed by the embargo

    def __len__(self) -> int:
        return len(self.test)


def _check_sorted(times: list[int]) -> None:
    for earlier, later in zip(times, times[1:]):
        if later < earlier:
            raise ValueError(
                "rows must be ordered by decision time; an unsorted frame makes "
                "'before' and 'after' meaningless and the purge silently wrong"
            )


def folds(
    times: list[int],
    n_splits: int,
    horizon_ns: int,
    embargo_ns: int = 0,
    min_train: int = 1,
) -> list[Fold]:
    """Contiguous forward folds, purged and embargoed.

    `times` are decision times, ascending. Training rows always precede their
    test window — no fold ever trains on the future.
    """
    if n_splits < 2 or len(times) < n_splits:
        return []
    _check_sorted(times)

    total = len(times)
    size = total // (n_splits + 1)
    if size < 1:
        return []

    built = []
    for k in range(n_splits):
        # The first block is training-only, so every fold has a past.
        start = (k + 1) * size
        end = total if k == n_splits - 1 else (k + 2) * size
        test = list(range(start, end))
        if not test:
            continue
        test_start, test_end = times[test[0]], times[test[-1]]

        train, purged, embargoed = [], 0, 0
        for i in range(total):
            if start <= i < end:
                continue
            t = times[i]
            if t + horizon_ns >= test_start and t <= test_end:
                # Label span reaches into the test window.
                purged += 1
                continue
            if test_end < t <= test_end + embargo_ns:
                embargoed += 1
                continue
            if t > test_end:
                # Strictly future data. Never trained on, and not an anomaly
                # worth counting -- it simply does not exist yet at this point
                # in the walk.
                continue
            train.append(i)

        if len(train) < min_train:
            continue
        built.append(Fold(k, train, test, test_start, test_end, purged, embargoed))
    return built


def coverage(folds_: list[Fold], total: int) -> dict:
    """How much of the sample was actually scored out of sample."""
    tested = sorted({i for f in folds_ for i in f.test})
    return {
        "folds": len(folds_),
        "rows": total,
        "tested": len(tested),
        "tested_frac": len(tested) / total if total else 0.0,
        "purged": sum(f.purged for f in folds_),
        "embargoed": sum(f.embargoed for f in folds_),
        "train_sizes": [len(f.train) for f in folds_],
    }


def leaks(fold: Fold, times: list[int], horizon_ns: int) -> list[int]:
    """Training rows whose label span still touches the test window.

    A self-check rather than a filter: if this ever returns anything, the purge
    is broken and every metric downstream is optimistic.
    """
    return [
        i for i in fold.train
        if times[i] + horizon_ns >= fold.test_start and times[i] <= fold.test_end
    ]
