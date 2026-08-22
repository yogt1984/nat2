"""TASK_2/12: two tapes compared from their manifests alone, and the installer's host profiles."""

import importlib.util
import json
from pathlib import Path

import pytest

from nat2.io.tape import compare, hours


def _manifest(root: Path, parts):
    root.mkdir(parents=True, exist_ok=True)
    lines = [{"stream": s, "path": f"{s}/2026-08-21/{s}-{hour}-{part:02d}.ndjson.zst", "lines": n, "bytes": 1,
              "sha256": "0" * 64, "first_seq": 0, "last_seq": n, "first_ingest": 1, "last_ingest": 2, "closed_at": 3}
             for s, hour, part, n in parts]
    (root / "_manifest.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return root


def test_hours_sum_parts_within_an_hour(tmp_path):
    root = _manifest(tmp_path / "a", [("hl.trades", "20260821T13", 0, 10), ("hl.trades", "20260821T13", 1, 5),
                                       ("hl.trades", "20260821T14", 0, 7), ("hl.l2book", "20260821T13", 0, 3)])
    assert hours(root) == {"hl.trades": {"20260821T13": 15, "20260821T14": 7}, "hl.l2book": {"20260821T13": 3}}
    assert hours(tmp_path / "missing") == {}


def test_compare_names_the_differing_hour_and_the_one_sided_hours(tmp_path):
    ours = _manifest(tmp_path / "a", [("hl.trades", "20260821T13", 0, 100), ("hl.trades", "20260821T14", 0, 100),
                                       ("hl.trades", "20260821T15", 0, 100)])
    theirs = _manifest(tmp_path / "b", [("hl.trades", "20260821T13", 0, 60), ("hl.trades", "20260821T13", 1, 40),
                                         ("hl.trades", "20260821T14", 0, 90), ("hl.trades", "20260821T16", 0, 100)])
    (c,) = compare(ours, theirs)
    assert c.stream == "hl.trades" and c.same == 1 and c.differ == [("20260821T14", 100, 90)]
    assert c.only_ours == ["20260821T15"] and c.only_theirs == ["20260821T16"] and c.overlapping == 2
    (tolerant,) = compare(ours, theirs, tolerance=0.1)
    assert tolerant.same == 2 and tolerant.differ == []


def test_profiles_enable_exactly_their_units(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("units", Path(__file__).resolve().parent.parent / "deploy" / "systemd_units.py")
    units = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(units)
    assert set(units.PROFILES["secondary"]) < set(units.PROFILES["primary"])
    calls = []
    monkeypatch.setattr(units.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(units.Path, "home", classmethod(lambda cls: tmp_path))
    units.install("secondary")
    enabled = next(c for c in calls if c[:4] == ["systemctl", "--user", "enable", "--now"])[4:]
    disabled = next(c for c in calls if c[:4] == ["systemctl", "--user", "disable", "--now"])[4:]
    assert enabled == list(units.PROFILES["secondary"]) and set(disabled) == set(units.PROFILES["primary"]) - set(enabled)
    with pytest.raises(SystemExit):
        units.install("bogus")
