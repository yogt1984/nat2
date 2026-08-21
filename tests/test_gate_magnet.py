"""TASK_2/08: the refusal is the product. Then the pre-registered rule on synthetic cells,
with criterion 2 read from the alpha-kernel cells of TASK_2/09 (seq 153)."""

import pytest

from nat2.gates import magnet as gate
from nat2.ledger.chain import Ledger

PREREG = {"name": "magnet_runnable_when", "min_scoreable_events": 2000, "min_distinct_days": 30,
          "cells_that_gate": ["1h", "4h"]}
ALPHA_PREREG = {"name": "magnet_alpha_kernel", "alpha": [1, 2], "k": [1, 2], "cells_that_gate": ["1h", "4h"],
                "definition": {"d_min": 0.25}}


def ledger_with(tmp_path, *, prereg=True, alpha_prereg=True, map_verdict="pass"):
    chain = Ledger(tmp_path / "ledger.jsonl")
    if prereg:
        chain.append("preregistration", PREREG)
    if alpha_prereg:
        chain.append("preregistration", ALPHA_PREREG)
    if map_verdict:
        chain.append("gate", {"gate": "map", "passed": map_verdict == "pass",
                              "detail": {"verdict": map_verdict, "coverage": {"BTC": 0.3}}})
    return chain


def fake_accrual(scored, days):
    return lambda events, series, since: {"scored": scored, "need": 2000, "days": days, "need_days": 30, "since_ts": since}


def never_called(*a):
    raise AssertionError("cells must not be evaluated on a refusal")


@pytest.mark.parametrize("setup, reason", [
    (dict(prereg=False), "preregistration_missing"),
    (dict(alpha_prereg=False), "preregistration_missing"),
    (dict(map_verdict=None), "upstream_map"),
    (dict(map_verdict="fail"), "upstream_map"),
    (dict(map_verdict="refused"), "upstream_map"),
])
def test_refuses_before_counting(tmp_path, monkeypatch, setup, reason):
    monkeypatch.setattr(gate, "accrual", fake_accrual(10**6, 10**3))
    chain = ledger_with(tmp_path, **setup)
    v = gate.run(chain, [], {}, {"BTC": 0.3}, never_called, tmp_path)
    assert not v.passed and v.detail["verdict"] == "refused" and v.detail["reason"] == reason
    assert chain.latest("gate", gate="magnet").payload["detail"]["reason"] == reason


@pytest.mark.parametrize("scored, days", [(1999, 60), (5000, 29), (0, 0)])
def test_refuses_below_event_or_day_floor(tmp_path, monkeypatch, scored, days):
    monkeypatch.setattr(gate, "accrual", fake_accrual(scored, days))
    v = gate.run(ledger_with(tmp_path), [], {}, {"BTC": 0.3}, never_called, tmp_path)
    assert v.detail["reason"] == "insufficient_forward_events" and v.detail["judged_against"] == [0, 1]
    assert v.detail["window"] == {"scored": scored, "need": 2000, "days": days, "need_days": 30, "since_ts": v.detail["window"]["since_ts"]}


def test_prereg_numbers_must_match_code(tmp_path, monkeypatch):
    chain = Ledger(tmp_path / "l.jsonl")
    chain.append("preregistration", {**PREREG, "min_scoreable_events": 1000})
    assert gate.preregistered(chain) is None
    chain.append("preregistration", {**ALPHA_PREREG, "definition": {"d_min": 0.3}})
    assert gate.alpha_preregistered(chain) is None
    chain.append("preregistration", {**ALPHA_PREREG, "alpha": [1, 2, 3]})
    assert gate.alpha_preregistered(chain) is None


def arow(alpha, beats=True, p=0.001, hit=0.62, thr=0.55):
    return {"alpha": alpha, "n": 3000, "beats_baseline": beats, "delta_z": 3.0, "placebo_p": p,
            "decision_hit_rate": hit, "threshold": thr}


def cell(coin="BTC", h="1h", k=1.0, beats=True, p=0.001, hit=0.62, thr=0.55, alpha=None):
    """A synthetic cell; by default alpha = 1 wins it and alpha = 2 does not."""
    return {"coin": coin, "horizon": h, "k": k, "n": 3000, "beats_baseline": beats, "delta_z": 3.0,
            "placebo_p": p, "decision_hit_rate": hit, "threshold": thr,
            "alpha": [arow(1.0), arow(2.0, beats=False)] if alpha is None else alpha}


def test_decide_known_positive_and_negative():
    positive = [cell(h=h, k=k) for h in ("1h", "4h") for k in (1.0, 2.0)]
    d = gate.decide(positive)
    assert d["criteria"]["1_beats_baseline"] and d["criteria"]["3_placebo_collapses"] and d["criteria"]["4_clears_cost"]
    assert d["would_pass_on_1_3_4"] and d["criteria"]["2_alpha_kernel"] and d["passed"]
    assert d["alpha"] == {"wins_by_alpha": {"1": 4, "2": 0}, "winning_alpha": 1.0, "clears_cost": True}
    negative = [cell(h=h, k=k, beats=False) for h in ("1h", "4h") for k in (1.0, 2.0)]
    assert not gate.decide(negative)["criteria"]["1_beats_baseline"]
    assert not gate.decide([])["would_pass_on_1_3_4"]
    two_of_four = positive[:2] + negative[2:]          # 50% < 12/18
    assert not gate.decide(two_of_four)["criteria"]["1_beats_baseline"]


def test_placebo_survival_or_cost_voids_a_win():
    leaky = [cell(h=h, k=k, p=0.3) for h in ("1h", "4h") for k in (1.0, 2.0)]    # effect survives shuffling
    d = gate.decide(leaky)
    assert d["criteria"]["1_beats_baseline"] and not d["criteria"]["3_placebo_collapses"] and not d["would_pass_on_1_3_4"]
    costly = [cell(h=h, k=k, hit=0.54) for h in ("1h", "4h") for k in (1.0, 2.0)]   # beats baseline, not the fee
    assert not gate.decide(costly)["criteria"]["4_clears_cost"]
    unplaceboed = [cell(p=None)]
    assert not gate.decide(unplaceboed)["criteria"]["3_placebo_collapses"]


def test_runnable_path_records_cells_and_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "accrual", fake_accrual(2500, 31))
    calls = []
    v = gate.run(ledger_with(tmp_path), [], {}, {"BTC": 0.3, "DOGE": 0.1}, lambda c, h, k: calls.append((c, h, k)) or cell(c, h, k), tmp_path)
    assert calls == [("BTC", h, k) for h in ("1h", "4h") for k in (1.0, 2.0)]      # DOGE below the coverage floor
    assert v.detail["verdict"] == "pass" and v.passed and v.detail["universe"] == ["BTC"]
    assert v.detail["judged_against"] == [0, 1] and v.detail["criteria"]["2_alpha_kernel"]
    assert set(v.detail["provenance"]) == {"commit", "clean_tree"}
