"""TASK_2/14: the digest renders real numbers with their as-of times from a synthetic home, says
"no data" on an empty one, the gates runner sees a flip, the status page renders the sidecar, and
the read side refuses a pruned raw root."""

import importlib.util
import json
import shutil
from pathlib import Path

from nat2.core.clock import NS
from nat2.core.registry import Registry
from nat2.features.liquidations import LiquidationEvent
from nat2.io import actions
from nat2.io.compact import raw_covers_parquet
from nat2.io.worm import WormWriter
from nat2.ledger.chain import Ledger

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NOW = 1_787_400_000 * NS          # 2026-08-22T12:26:40Z
MIN = 60 * NS


def _home(tmp_path) -> Path:
    home = tmp_path
    raw = home / "data" / "raw"
    shutil.copy(ROOT / "pairs.toml", home / "pairs.toml")
    meta = {"universe": [{"name": "BTC"}, {"name": "ETH"}, {"name": "xyz:OIL"}]}
    ctxs = [{"markPx": "100", "oraclePx": "100", "funding": "0.0001", "openInterest": "10", "dayNtlVlm": "9e6"},
            {"markPx": "10", "oraclePx": "10", "funding": "0.0001", "openInterest": "5", "dayNtlVlm": "8e6"},
            {"markPx": "1", "oraclePx": "1", "funding": "0", "openInterest": "1", "dayNtlVlm": "7e6"}]
    with WormWriter(raw, "hl.assetctxs") as w:
        w.write([meta, ctxs], t_event=None, t_ingest=NOW - 5 * MIN)
    with WormWriter(raw, "hl.trades") as w:
        for i in range(120):                     # two hours of one-minute BTC bars: open 100, close alternating
            t = NOW - (120 - i) * MIN
            for k, px in enumerate((100.0, 100 + (i % 2) * 0.1)):
                w.write([{"coin": "BTC", "px": str(px), "sz": "1", "time": (t + k * NS) // 1_000_000,
                          "users": ["0xa", "0xb"]}], t_event=t + k * NS, t_ingest=t + k * NS)
    with WormWriter(raw, "nat2.liqmap2") as w:      # v2: wide bands, sparse buckets (TASK_2/17)
        w.write({"coins": [{"coin": "BTC", "coverage": 0.3, "mark": 100.0, "span": 0.30, "bucket_pct": 0.0025,
                            "up": {"0.05": 1e6, "0.1": 3e6, "0.2": 4e6, "0.3": 4e6},
                            "down": {"0.05": 2e6, "0.1": 2e6, "0.2": 9e6, "0.3": 9e6},
                            "imb": {"0.05": 0.33, "0.1": -0.2}, "outside_span": 7,
                            "buckets": [[-0.01, 2e6, 0.0, 1], [0.12, 3e6, 0.0, 2]]}]},
                t_event=None, t_ingest=NOW - 10 * MIN)
    with WormWriter(raw, "nat2.liqmap") as w:
        w.write({"coins": [{"coin": "BTC", "coverage": 0.3, "imb": {"0.02": -0.5, "0.05": -0.2},
                            "near": {"up_dist": 0.01, "down_dist": -0.02}, "up": {}, "down": {}}]},
                t_event=None, t_ingest=NOW - 10 * MIN)
    Registry(home / "data" / "registry.sqlite").record_liquidations([
        LiquidationEvent(1, NOW - 30 * MIN, "BTC", "0xv", 100.0, "market", 100.0, 2.0, "0xo", "counterparty"),
        LiquidationEvent(2, NOW - 30 * MIN + 5 * NS, "BTC", "0xw", 100.0, "market", 100.0, 3.0, "0xo", "counterparty")])
    chain = Ledger(home / "data" / "ledger.jsonl")
    chain.append("gate", {"gate": "map", "passed": False, "detail": {"verdict": "refused", "reason": "insufficient_forward_events",
                                                                    "coverage": {"BTC": 0.31}, "window": {"scored": 412, "need": 1000}}})
    chain.append("incident", {"name": "capture_hole", "cause": "test"})
    (home / "data" / "ops").mkdir(parents=True, exist_ok=True)
    (home / "data" / "ops" / "gapwatch_state.json").write_text(json.dumps(
        {"week": "2026-W34", "gap_minutes": {"stream:hl.trades:hole": 55.0}, "open": {}, "units": {"nat2-capture.service": "active"},
         "holes": [{"from_s": NOW / NS - 7200, "to_s": NOW / NS - 3900, "minutes": 55.0}]}))
    (home / "data" / "events").mkdir()
    (home / "data" / "events" / "2026-08-22.ndjson").write_text(json.dumps(
        {"schema": 1, "class": "scheduled", "source": "cpi", "event_id": "cpi:test", "source_ts": NOW + 3600 * NS}) + "\n")
    actions.append("L1", "cycle:scan", {"events": 2}, root=home, t_ingest=NOW - MIN)
    return home


def test_daily_digest_renders_real_numbers_with_as_of_times(tmp_path):
    report = _load("report")
    d = report.collect(_home(tmp_path), NOW, weekly=False)
    rows = {r["coin"]: r for r in d["pairs"]["rows"]}
    assert set(rows) == {"BTC", "ETH", "SOL", "xyz:OIL"} and d["pairs"]["source"].startswith("roster evaluated")   # A + pins + B
    btc = rows["BTC"]
    assert btc["mark"] == 100.0 and btc["coverage"] == 0.31 and btc["imb_002"] == -0.5 and btc["near_dn"] == -0.02
    assert btc["bars"] == 120 and btc["sigma_1m"] and abs(btc["sigma_1h"] - btc["sigma_1m"] * 60 ** 0.5) < 1e-12
    assert btc["liq_n"] == 2 and btc["liq_notional"] == 500.0 and btc["liq_max_minute"] == 500.0
    assert rows["SOL"]["mark"] is None and rows["SOL"]["liq_n"] == 0                # missing stays missing
    assert d["accrual"][0]["bars"] == [{"label": "scored", "have": 412, "need": 1000}] and d["accrual"][1]["note"] == "never run"
    assert [e["event_id"] for e in d["events_ahead"]] == ["cpi:test"] and len(d["incidents"]) == 1 and len(d["actions"]) == 1
    page = report.render(d)
    assert page.count('class="asof"') > 40 and "—" in page and "2026-08-22" in page and "<script" not in page
    assert "cycle:scan" in page and "capture_hole" in page and "hole 55m" in page and "412" in page
    # v2 wide map: descriptive, and it must show the mass v1 cannot address.
    (wm,) = d["wide_map"]["rows"]
    assert wm["coin"] == "BTC" and wm["up"]["0.3"] == 4e6 and wm["outside_span"] == 7
    assert wm["beyond_5pct"] == 3e6 and wm["sigma_1w"] > wm["sigma_1d"] > btc["sigma_1h"]
    assert "Wide map" in page and "3.0M" in page and "beyond ±5%" in page


def test_empty_home_says_no_data_and_weekly_adds_the_series(tmp_path):
    report = _load("report")
    shutil.copy(ROOT / "pairs.toml", tmp_path / "pairs.toml")
    d = report.collect(tmp_path, NOW, weekly=True)
    page = report.render(d)
    assert d["pairs"]["rows"] == [] and "no pairs" in page and "never run" in page and "Observation series" in page
    assert d["wide_map"]["rows"] == [] and "no v2 snapshot in the last 24h" in page
    assert "none in the last 7 days" in page and "ledger seq <span" in page and "seq none" not in page
    assert d["ledger_seq"] is None and "Next dates" in page


def test_gates_runner_sees_a_flip_only_when_the_newest_verdict_changed(tmp_path):
    runner = _load("gates_run")
    chain = Ledger(tmp_path / "l.jsonl")
    chain.append("gate", {"gate": "map", "passed": False, "detail": {"verdict": "refused", "reason": "x"}})
    before = runner.verdicts(tmp_path / "l.jsonl")
    chain.append("gate", {"gate": "map", "passed": False, "detail": {"verdict": "refused", "reason": "x"}})
    assert runner.flips(before, runner.verdicts(tmp_path / "l.jsonl")) == []
    chain.append("gate", {"gate": "map", "passed": True, "detail": {"verdict": "pass"}})
    assert runner.flips(before, runner.verdicts(tmp_path / "l.jsonl")) == [("map", "refused:x", "pass")]


def test_status_page_renders_the_sidecar_and_survives_its_absence(tmp_path):
    sp = _load("statuspage")
    paths = sp.Paths(ledger=tmp_path / "l.jsonl", gapwatch_state=tmp_path / "g.json", manifest=tmp_path / "m.jsonl",
                     events_dir=tmp_path, report_state=tmp_path / "r.json", nat_data=tmp_path, nat_results=tmp_path)
    assert "no report sidecar yet" in sp.render(sp.collect(NOW / NS, paths))
    (tmp_path / "r.json").write_text(json.dumps({"generated_s": NOW / NS, "pairs": {"rows": [{"coin": "BTC", "mark": 100.0, "sigma_1h": 0.005, "coverage": 0.31, "imb_002": -0.5, "liq_n": 2}]},
                                                 "accrual": [{"gate": "map", "bars": [{"label": "scored", "have": 412, "need": 1000}]}]}))
    page = sp.render(sp.collect(NOW / NS, paths))
    assert "BTC" in page and "0.50%" in page and "412 / 1,000" in page


def test_read_side_refuses_when_a_compacted_part_lost_its_raw_file(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "parquet"
    (raw / "hl.trades" / "2026-08-21").mkdir(parents=True)
    (raw / "hl.trades" / "2026-08-21" / "hl.trades-20260821T13-00.ndjson.zst").write_bytes(b"x")
    (out / "hl.trades").mkdir(parents=True)
    (out / "hl.trades" / "hl.trades-20260821T13-00.parquet").write_bytes(b"x")
    (out / "frame_btc.parquet").write_bytes(b"x")                                      # derived, not a part
    assert raw_covers_parquet(raw, out) == []
    (out / "hl.trades" / "hl.trades-20260821T14-00.parquet").write_bytes(b"x")
    assert raw_covers_parquet(raw, out) == ["hl.trades/hl.trades-20260821T14-00.ndjson.zst"]
