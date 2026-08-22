"""The pair roster: which coins are observed, which enter the map universe, and why.

Until now the universe was implicit -- `--all --min-volume` at capture start, a
coverage floor inside `gate map`, and a census note that builder-deployed perps
are excluded "by default". This module makes it one declared object: a spec in
`pairs.toml`, an evaluation against the venue's latest cross-section, and a
ledger entry of kind `roster` whenever the result changes, so the universe that
stood at any verdict can be read back rather than reconstructed.

Two rosters, deliberately. The A-roster is the observed set and the only source
of the map universe. The B-roster is the builder-deployed perps (`dex:COIN`),
observed -- the census found the venue's largest cascades there -- but never in
the map universe until a pre-registered admission rule exists. None does yet,
and this module promotes nothing on its own.

Coverage is read from the latest `gate map` verdict and never guessed: a coin
that has not been through the map gate has no coverage, so it is not in the map
universe, which is the refusal-over-defaults rule applied to the universe itself.
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from nat2.ledger.chain import Entry, Ledger

KIND = "roster"


def is_builder_deployed(coin: str) -> bool:
    """Builder-deployed perps are namespaced `dex:COIN`; the venue's own are bare."""
    return ":" in coin


@dataclass(frozen=True)
class RosterSpec:
    top_n: int = 18
    min_volume: float = 5e6
    pin: tuple[str, ...] = ("BTC", "ETH", "SOL")
    map_min_coverage: float = 0.25
    b_min_volume: float = 5e6

    @classmethod
    def load(cls, path: Path) -> "RosterSpec":
        raw = tomllib.loads(Path(path).read_text())
        inc, mp, b = raw.get("include", {}), raw.get("map_universe", {}), raw.get("b_roster", {})
        return cls(
            top_n=int(inc.get("top_n", cls.top_n)),
            min_volume=float(inc.get("min_volume", cls.min_volume)),
            pin=tuple(inc.get("pin", cls.pin)),
            map_min_coverage=float(mp.get("min_coverage", cls.map_min_coverage)),
            b_min_volume=float(b.get("min_volume", cls.b_min_volume)),
        )


@dataclass(frozen=True)
class Roster:
    observed: tuple[str, ...]       # A-roster: captured, scanned, reported
    b_roster: tuple[str, ...]       # builder-deployed, observed, outside the map universe
    map_universe: tuple[str, ...]   # observed coins at or above the coverage floor
    spec: RosterSpec

    @property
    def captured(self) -> tuple[str, ...]:
        """Everything the capture subscribes to."""
        return tuple(sorted(set(self.observed) | set(self.b_roster)))

    def payload(self) -> dict:
        return {"name": KIND, "observed": list(self.observed), "b_roster": list(self.b_roster),
                "map_universe": list(self.map_universe), "spec": asdict(self.spec)}


def evaluate(spec: RosterSpec, volumes: dict[str, float], coverage: dict[str, float]) -> Roster:
    """`volumes` is the venue's latest 24 h notional per coin; `coverage` the latest map verdict's."""
    ranked = sorted(volumes, key=lambda c: (-volumes[c], c))
    venue = [c for c in ranked if not is_builder_deployed(c) and volumes[c] >= spec.min_volume][: spec.top_n]
    observed = tuple(sorted(set(venue) | set(spec.pin)))
    b_roster = tuple(sorted(c for c in ranked if is_builder_deployed(c) and volumes[c] >= spec.b_min_volume))
    universe = tuple(c for c in observed if coverage.get(c, 0.0) >= spec.map_min_coverage)
    return Roster(observed, b_roster, universe, spec)


def diff(previous: Entry | None, roster: Roster) -> dict[str, list[str]]:
    """What changed against the last ledgered roster; everything is 'added' the first time."""
    old = previous.payload if previous else {}
    out: dict[str, list[str]] = {}
    for key in ("observed", "b_roster", "map_universe"):
        before, after = set(old.get(key, [])), set(getattr(roster, key))
        if added := sorted(after - before):
            out[f"{key}_added"] = added
        if removed := sorted(before - after):
            out[f"{key}_removed"] = removed
    return out


def apply(ledger: Ledger, roster: Roster) -> tuple[Entry | None, dict[str, list[str]]]:
    """Append a `roster` entry when the roster differs from the last one; otherwise append nothing."""
    previous = ledger.latest(KIND, name=KIND)
    changes = diff(previous, roster)
    if not changes:
        return None, changes
    return ledger.append(KIND, {**roster.payload(), "changes": changes}), changes
