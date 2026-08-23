"""Touch detection: when did price actually reach a cluster, and what lay beyond it?

`HYPOTHESIS_2.md` §1.2, bound to the ledger by seq 191. Stage A asks whether price is
*pulled* toward mass; this module supplies the events Stage B asks its question about --
the moments price arrived.

Three rules carry the whole file, and each exists because its absence manufactures
observations that are not observations.

*The map must predate the print.* The as-of snapshot is the newest that arrived strictly
earlier and is no more than `MAX_MAP_AGE_NS` old -- `features.liquidations.match_slots`'
rule, reused rather than restated. A print with no such snapshot is not a touch and is not
a miss; nothing is known about it, so it is not an observation.

*A shell with no mass is not a cluster.* Entry into an empty shell is price moving, which
is not the event. `CLUSTER_MIN_NOTIONAL` is the same $50k threshold `nearest()` uses.

*One touch per shell per hour.* Price oscillating on a shell edge would otherwise produce
dozens of "arrivals" that one move resolves, and the uniqueness weights downstream would be
correcting for something detection should never have created.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from nat2.core.clock import NS
from nat2.features.liquidations import MAX_MAP_AGE_NS
from nat2.io.mapsnap import CLUSTER_MIN_NOTIONAL

BANDS = (0.005, 0.01, 0.02, 0.05)
DEBOUNCE_NS = 3600 * NS
OUTER = str(BANDS[-1])


@dataclass(frozen=True)
class Touch:
    t: int
    px: float
    coin: str
    side: str            # "up" | "down": which side of the mark the touched shell is on
    band: float          # the shell's outer edge; the shell is (previous band, band]
    sweep: int           # +1 price swept up into the shell, -1 down
    mass: float          # notional in the touched shell itself
    fuel: float          # mass ahead of the sweep, beyond the touched shell, out to 5%
    brake: float         # mass behind the mark, which fires only on a reversal
    t_snap: int          # the as-of snapshot's arrival, so staleness is auditable per event

    @property
    def f(self) -> float:
        """`F` of seq 191 §1.3: positive means continuation is predicted."""
        total = self.fuel + self.brake
        return (self.fuel - self.brake) / total if total > 0 else 0.0

    @property
    def shell_index(self) -> int:
        return BANDS.index(self.band)


def shell_of(offset: float) -> tuple[str, float] | None:
    """`(side, band)` of a price offset from the mark, or None beyond the map's span."""
    distance = abs(offset)
    for band in BANDS:
        if distance <= band:
            return ("up" if offset >= 0 else "down"), band
    return None


def shell_mass(snap: dict, side: str, band: float) -> float:
    """Mass in the shell `(B_{i-1}, B_i]`, differenced from the persisted cumulative bands."""
    cumulative = snap.get("up" if side == "up" else "down") or {}
    index = BANDS.index(band)
    inner = cumulative.get(str(BANDS[index - 1]), 0.0) or 0.0 if index else 0.0
    return max(0.0, (cumulative.get(str(band)) or 0.0) - inner)


def fuel_and_brake(snap: dict, side: str, band: float) -> tuple[float, float]:
    """Mass ahead of the sweep beyond the touched shell, and mass behind the mark.

    Both stop at 5% because that is where the persisted map stops -- stated as a deviation
    in seq 191 §1.3, not silently truncated here.
    """
    ahead = snap.get("up" if side == "up" else "down") or {}
    behind = snap.get("down" if side == "up" else "up") or {}
    fuel = max(0.0, (ahead.get(OUTER) or 0.0) - (ahead.get(str(band)) or 0.0))
    return fuel, max(0.0, behind.get(OUTER) or 0.0)


def touches(path, snaps: list[dict], coin: str, max_age_ns: int = MAX_MAP_AGE_NS,
            min_notional: float = CLUSTER_MIN_NOTIONAL,
            debounce_ns: int = DEBOUNCE_NS) -> list[Touch]:
    """Every touch on one coin's tick path, in order. `path` and `snaps` are time-sorted."""
    keys = [s["t_ingest"] for s in snaps]
    current: tuple[str, float] | None = None
    last: dict[tuple[str, float], int] = {}
    found: list[Touch] = []
    for t, px in path:
        # Strictly earlier: a snapshot taken at this nanosecond may already contain the
        # consequences of this print.
        i = bisect.bisect_left(keys, t) - 1
        if i < 0:
            continue
        snap = snaps[i]
        mark = snap.get("mark") or 0.0
        if t - snap["t_ingest"] > max_age_ns or mark <= 0:
            # Stale or unusable: the shell price sits in is unknown, so the walk forgets
            # where it was rather than pretending the old reading still holds.
            current = None
            continue
        here = shell_of(px / mark - 1)
        if here == current:
            continue
        current = here
        if here is None:
            continue
        mass = shell_mass(snap, *here)
        if mass < min_notional or t - last.get(here, -debounce_ns) < debounce_ns:
            continue
        last[here] = t
        fuel, brake = fuel_and_brake(snap, *here)
        found.append(Touch(t=t, px=px, coin=coin, side=here[0], band=here[1],
                           sweep=1 if here[0] == "up" else -1, mass=mass,
                           fuel=fuel, brake=brake, t_snap=snap["t_ingest"]))
    return found
