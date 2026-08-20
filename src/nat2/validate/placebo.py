"""The permutation placebo — geometry masquerading as mass.

`HYPOTHESIS_1.md` §5 names the confound that will hand us a false positive:
liquidation clusters sit where price has already been. Longs are liquidated
below where longs entered, and entries cluster at support, at prior-day ranges,
at round numbers, at high-volume nodes. Cluster location is therefore
downstream of price history, so a naive test finds "price moves toward
clusters" partly because clusters mark levels where mean reversion already
operates. **A positive result is expected under the null and means nothing on
its own.**

The placebo separates the two. Rebuild the map with the masses **shuffled
across the observed locations**, preserving both distributions and breaking
only their pairing. Location structure survives untouched; mass structure is
destroyed. Anything that survives was geometry.

Two properties make this cheap here, and both are consequences of earlier
decisions rather than luck:

*Labels are invariant.* Barriers are placed at `±k·σ`, independent of the map
(§3), so a permutation cannot move a barrier. The same labels are reused across
every replication, and only the features are rebuilt.

*Snapshots are stored.* The map as believed at each decision time is on disk,
so a replication permutes recorded history rather than re-deriving it.

**Granularity, stated plainly.** §5 specifies shuffling across cluster
locations. What is persisted is per-band aggregates, not individual buckets, so
the shuffle here is across the eight `(side, band)` slots rather than across
individual clusters. That is coarser than the specification, and it preserves
less of the fine location structure. It is a weaker placebo than the one
specified, not a stronger one, and a result that survives it has cleared a
lower bar than §5 intends.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

BANDS = ("0.005", "0.01", "0.02", "0.05")


def permute_snapshot(snap: dict, rng: random.Random) -> dict:
    """One snapshot with its band masses shuffled across the (side, band) slots.

    The multiset of masses is preserved exactly, as is the set of slots. Only
    which mass sits in which slot changes.
    """
    up = dict(snap.get("up") or {})
    down = dict(snap.get("down") or {})
    slots = [("up", b) for b in BANDS] + [("down", b) for b in BANDS]
    masses = [
        (up if side == "up" else down).get(band, 0.0) or 0.0 for side, band in slots
    ]
    rng.shuffle(masses)

    new_up, new_down = {}, {}
    for (side, band), mass in zip(slots, masses):
        (new_up if side == "up" else new_down)[band] = mass

    near = dict(snap.get("near") or {})
    if rng.random() < 0.5:
        # Swap which side the nearest clusters sit on, so the nearest-cluster
        # columns are permuted on the same principle as the bands.
        near = {
            "up_px": near.get("up_px"), "down_px": near.get("down_px"),
            "up_dist": near.get("up_dist"), "down_dist": near.get("down_dist"),
            "up_notional": near.get("down_notional"),
            "down_notional": near.get("up_notional"),
        }

    permuted = dict(snap)
    permuted["up"] = new_up
    permuted["down"] = new_down
    permuted["near"] = near
    # Imbalance is derived from the bands, so it must be recomputed rather than
    # carried over -- a stale imb would leak the real map straight through.
    permuted["imb"] = {b: _imbalance(new_down.get(b, 0.0), new_up.get(b, 0.0)) for b in BANDS}
    permuted["imb_cross"] = dict(snap.get("imb_cross") or {})
    return permuted


def _imbalance(down: float, up: float) -> float:
    total = down + up
    return (down - up) / total if total else 0.0


def permute_series(
    series_by_coin: dict[str, list[dict]], seed: int
) -> dict[str, list[dict]]:
    """A whole map history, permuted snapshot by snapshot under one seed."""
    rng = random.Random(seed)
    return {
        coin: [permute_snapshot(snap, rng) for snap in snaps]
        for coin, snaps in series_by_coin.items()
    }


@dataclass
class PlaceboResult:
    """The real effect against the distribution of permuted ones."""

    real_z: float
    placebo_z: list[float]

    @property
    def replications(self) -> int:
        return len(self.placebo_z)

    @property
    def exceeded(self) -> int:
        """Placebos that matched or beat the real effect."""
        return sum(1 for z in self.placebo_z if z >= self.real_z)

    @property
    def p_value(self) -> float:
        """Add-one estimate, so a zero count never reports p = 0."""
        if not self.placebo_z:
            return 1.0
        return (self.exceeded + 1) / (self.replications + 1)

    @property
    def mean_placebo_z(self) -> float:
        return sum(self.placebo_z) / len(self.placebo_z) if self.placebo_z else 0.0

    def collapses(self, alpha: float = 0.01) -> bool:
        """Did the effect vanish when mass was shuffled? That is the pass."""
        return self.p_value <= alpha

    def summary(self) -> dict:
        return {
            "real_z": self.real_z,
            "replications": self.replications,
            "exceeded": self.exceeded,
            "p_value": self.p_value,
            "mean_placebo_z": self.mean_placebo_z,
            "max_placebo_z": max(self.placebo_z) if self.placebo_z else 0.0,
        }
