"""gate map judged against the pre-registered bars, and the band permutation null."""
from __future__ import annotations

import json

from nat2.features.liquidations import LiquidationEvent, band_null, score_clusters
from nat2.gates import map as gate
from nat2.ledger.chain import Ledger

BANDS = (0.005, 0.01, 0.02, 0.05)
NS = 1_000_000_000


def _snap(t_ingest, mark, up, down, coin="BTC"):
    return {"t_ingest": t_ingest, "coin": coin, "mark": mark,
            "up": {str(b): up.get(b, 0.0) for b in BANDS}, "down": {str(b): down.get(b, 0.0) for b in BANDS}}


def _event(t_event, mark_px, coin="BTC", tid=1, user="0xw"):
    return LiquidationEvent(tid=tid, t_event=t_event, coin=coin, liquidated_user=user, mark_px=mark_px,
                            method="market", px=mark_px, sz=1.0, observer="0xo", source="counterparty")


# --- golden: 2 snapshots, 10 liquidations, hand-computed --------------------

def _golden():
    # snapshot A: mass only in the 0.5% band below the mark; B: mass only 1% above.
    a = _snap(100, 100.0, up={}, down={0.005: 5e6})
    b = _snap(200, 100.0, up={0.01: 5e6}, down={})
    events = [
        *[_event(150 + i, 99.7, tid=i) for i in range(4)],          # A, down 0.5%: hit
        _event(160, 100.3, tid=4),                                   # A, up 0.5%: miss
        *[_event(250 + i, 100.8, tid=10 + i) for i in range(3)],    # B, up 1%: hit
        _event(260, 99.2, tid=13),                                   # B, down 1%: miss
        _event(270, 101.5, tid=14),                                  # B, up 2%: miss
    ]
    return events, {"BTC": [a, b]}


def test_golden_band_hit_rate_and_seeded_null():
    events, series = _golden()
    scored = score_clusters(events, series, BANDS)
    assert scored.scored == 10 and scored.band_hits == 7
    null = band_null(events, series, permutations=200, seed=118)
    # Each snapshot has one massive slot of eight; a random placement hits an
    # event's slot with p = 1/8, so the null mean sits near 0.125 and the
    # observed 0.7 is far above it.
    golden = {"band_hit_rate": 0.7, "permutations": 200, "informative": True}
    assert {k: null.summary()[k] for k in golden} == golden
    assert 0.08 < null.null_mean < 0.18 and null.z > 3
    assert json.dumps(null.summary())  # serialisable into the ledger


def test_null_is_degenerate_when_every_slot_carries_mass():
    full = {b: 1e6 for b in BANDS}
    series = {"BTC": [_snap(100, 100.0, up=full, down=full)]}
    null = band_null([_event(150, 99.7)], series, permutations=50)
    assert null.observed == 1.0 and null.informative is False and null.z == 0.0


def test_null_is_none_with_nothing_scoreable():
    assert band_null([_event(50, 99.7)], {"BTC": [_snap(100, 100.0, {}, {})]}) is None


# --- the gate against the chain ----------------------------------------------

class _Registry:
    def __init__(self, events):
        self._events = events

    def positions_ts(self):
        return 1

    def position_age_ns(self):
        return 0

    def positions(self):
        return []

    def addresses(self):
        return []

    def liquidations(self, since_ns=None):
        return self._events


def _prereg(ledger: Ledger, window_n=1000):
    ledger.append("preregistration", {"name": "map_per_position_threshold", "pass_if": ">= 0.60",
                                      "window": {"n": window_n}})
    ledger.append("preregistration", {"name": "map_cluster_threshold",
                                      "pass_if": {"side_hit_rate": ">= 0.60 with z >= 3 vs 0.50",
                                                  "band_hit_rate": "exceeds permutation-null mean by z >= 3"},
                                      "window": {"n": window_n}})
    return ledger.append("preregistration", {"name": "magnet_runnable_when", "min_scoreable_events": 2000})


def _run(tmp_path, events, series, prereg=True, window_n=1000):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    start = _prereg(ledger, window_n).ts if prereg else 0
    # forward events must postdate the last pre-registration entry
    shifted = [LiquidationEvent(**{**e.__dict__, "t_event": e.t_event + start}) for e in events]
    series = {c: [{**s, "t_ingest": s["t_ingest"] + start} for s in rows] for c, rows in series.items()}
    verdict, checks = gate.run(_Registry(shifted), [], ledger, map_series=series)
    return verdict, {c.name: c for c in checks}, ledger


def test_refuses_without_preregistration(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "validate", lambda _: {"n": 1, "exact_frac": 1.0, "median": 0.0})
    events, series = _golden()
    verdict, checks, ledger = _run(tmp_path, events, series, prereg=False)
    assert verdict.passed is False and verdict.detail["verdict"] == "refused"
    assert verdict.detail["reason"] == "preregistration_missing"


def test_refuses_until_the_window_is_filled_and_cites_seqs(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "validate", lambda _: {"n": 1, "exact_frac": 1.0, "median": 0.0})
    events, series = _golden()
    verdict, checks, ledger = _run(tmp_path, events, series)
    d = verdict.detail
    assert verdict.passed is False and d["verdict"] == "refused" and d["reason"] == "insufficient_forward_events"
    assert d["judged_against"] == [0, 1, 2] and d["window"]["scored"] == 10 and d["window"]["need"] == 1000
    assert d["cumulative"]["predictive"]["scored"] == 10  # descriptive, still reported
    assert ledger.verify()[0]


def test_cluster_component_can_carry_the_verdict_once_filled(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "validate", lambda _: {"n": 1, "exact_frac": 1.0, "median": 0.0})
    monkeypatch.setattr(gate, "WINDOW_EVENTS", 10)
    monkeypatch.setattr(gate, "PERMUTATIONS", 200)
    events, series = _golden()
    # golden: side hits = 7/10 = 0.70 >= 0.60 but z = (0.7-0.5)/sqrt(.25/10) = 1.26 < 3 -> cluster FAILs,
    # per-position has no positions -> 0.0 -> FAIL; overall fail, recorded as such.
    verdict, checks, _ = _run(tmp_path, events, series, window_n=10)
    assert verdict.detail["verdict"] == "fail" and checks["cluster"].passed is False
    assert checks["per_position"].passed is False
    # Now 40x the events: same rates, z grows past 3 -> cluster passes -> gate passes on one component.
    many = [LiquidationEvent(**{**e.__dict__, "tid": i * 100 + e.tid}) for i in range(40) for e in events]
    verdict, checks, _ = _run(tmp_path, many, series, window_n=10)
    assert checks["cluster"].passed is True and checks["per_position"].passed is False
    assert verdict.passed is True and verdict.detail["verdict"] == "pass"


def test_code_constants_must_match_the_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "validate", lambda _: {"n": 1, "exact_frac": 1.0, "median": 0.0})
    monkeypatch.setattr(gate, "WINDOW_EVENTS", 10)  # chain still says 1000
    events, series = _golden()
    verdict, _, _ = _run(tmp_path, events, series)
    assert verdict.detail["reason"] == "preregistration_missing"
