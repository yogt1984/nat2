"""TASK_2/13: the roster is one declared object, ledgered on change; the action log never blurs levels."""

from pathlib import Path

import pytest

from nat2.core.guard import record
from nat2.core.roster import KIND, Roster, RosterSpec, apply, diff, evaluate, is_builder_deployed
from nat2.io import actions
from nat2.ledger.chain import Ledger

SPEC = RosterSpec(top_n=3, min_volume=100.0, pin=("BTC", "PIN"), map_min_coverage=0.25, b_min_volume=50.0)
VOLUMES = {"BTC": 900.0, "ETH": 800.0, "SOL": 700.0, "DOGE": 600.0, "PIN": 1.0, "xyz:OIL": 60.0, "xyz:GAS": 10.0}
COVERAGE = {"BTC": 0.31, "ETH": 0.25, "SOL": 0.2, "DOGE": 0.9, "xyz:OIL": 0.9}


def test_roster_top_n_pins_b_roster_and_coverage_floor():
    r = evaluate(SPEC, VOLUMES, COVERAGE)
    assert r.observed == ("BTC", "ETH", "PIN", "SOL")            # top 3 by volume + pins, even at volume 1
    assert r.b_roster == ("xyz:OIL",) and is_builder_deployed("xyz:OIL") and not is_builder_deployed("BTC")
    assert r.map_universe == ("BTC", "ETH")                       # SOL below the floor, PIN unknown -> out
    assert "xyz:OIL" not in r.map_universe                        # coverage 0.9 but B-roster: never promoted here
    assert r.captured == ("BTC", "ETH", "PIN", "SOL", "xyz:OIL")
    assert evaluate(SPEC, VOLUMES, {}).map_universe == ()         # no map verdict yet: refuse, never guess


def test_spec_loads_from_the_repo_toml():
    spec = RosterSpec.load(Path(__file__).resolve().parent.parent / "pairs.toml")
    assert spec.top_n == 18 and spec.min_volume == 5e6 and spec.pin == ("BTC", "ETH", "SOL")
    assert spec.map_min_coverage == 0.25


def test_apply_ledgers_exactly_the_changes_and_nothing_when_unchanged(tmp_path):
    chain = Ledger(tmp_path / "data" / "ledger.jsonl")
    r = evaluate(SPEC, VOLUMES, COVERAGE)
    entry, changes = apply(chain, r)
    assert entry.kind == KIND and entry.seq == 0 and changes["observed_added"] == list(r.observed)
    assert apply(chain, r) == (None, {}) and len(chain.entries()) == 1
    later = evaluate(SPEC, {**VOLUMES, "DOGE": 950.0}, COVERAGE)  # DOGE climbs into the top 3, SOL drops out
    entry, changes = apply(chain, later)
    assert entry.seq == 1 and changes == {"observed_added": ["DOGE"], "observed_removed": ["SOL"], "map_universe_added": ["DOGE"]}
    assert diff(chain.latest(KIND, name=KIND), later) == {}


def test_action_log_levels_and_filters(tmp_path):
    actions.append("L1", "cycle:scan", {"events": 3}, root=tmp_path, t_ingest=10)
    actions.append("L2", "gate", {"gate": "map"}, root=tmp_path, t_ingest=20)
    with pytest.raises(ValueError):
        actions.append("L9", "x", {}, root=tmp_path)
    assert [r["kind"] for r in actions.read(tmp_path)] == ["cycle:scan", "gate"]
    assert [r["t_ingest"] for r in actions.read(tmp_path, since_ns=15)] == [20]
    assert actions.read(tmp_path, level="L3") == [] and actions.read(tmp_path / "nowhere") == []
    assert actions.path(tmp_path) == tmp_path / "data" / "actions.jsonl"


def test_a_gate_verdict_is_an_l2_action_next_to_its_ledger(tmp_path):
    chain = Ledger(tmp_path / "data" / "ledger.jsonl")
    record(chain, "map", False, {"verdict": "refused", "reason": "insufficient_forward_events"})
    (a,) = actions.read(tmp_path, level="L2")
    assert a["kind"] == "gate" and a["payload"] == {"gate": "map", "passed": False, "verdict": "refused",
                                                     "reason": "insufficient_forward_events", "seq": 0}
