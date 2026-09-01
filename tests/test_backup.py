"""The backup, and the three ways it could have looked like one without being one.

There was no backup code at all before this, for 4.6 GB of tape the venue will
not replay and a registry holding 12,596 liquidations that cannot be re-fetched
(`liqscan` reads `userFills`, capped at 2,000 per wallet). The failure that
matters here is not "the backup did not run" -- that is loud. It is a backup
that runs, reports success, and restores something subtly wrong.

Loaded by path like the other deploy tools: they must work when the venv does
not, which is one of the days you most need a restore.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import random
import shutil
import sqlite3
from pathlib import Path

import pytest

from nat2.io.worm import WormWriter
from nat2.ledger.chain import Ledger

spec = importlib.util.spec_from_file_location(
    "backup", Path(__file__).resolve().parent.parent / "deploy" / "backup.py"
)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)

NS = 1_000_000_000
T0 = 1_755_000_000 * NS


def _store(tmp_path: Path) -> Path:
    """A small store with the real skew: many tiny snapshot parts, a few fat
    websocket ones. The skew is the whole reason the sample is stratified."""
    data = tmp_path / "data"
    raw = data / "raw"
    for i in range(40):
        with WormWriter(raw, "nat2.liqmap") as w:
            w.write({"i": i}, t_event=None, t_ingest=T0 + i * 60 * NS)
    # Incompressible payloads on purpose. A repetitive pad compresses to almost
    # nothing and inverts the very skew this fixture exists to reproduce -- the
    # first version of it made nat2.liqmap the heaviest stream at 32% of bytes,
    # so the stratifier did the right thing with the wrong data and the test
    # failed for a reason that was not a defect.
    noise = random.Random(7)
    # Enough parts per stream that the byte-weighting has somewhere to go. With
    # only three, the stratifier takes all three and the remainder must fall to
    # the sparse stream -- correct behaviour, but a fixture in which the
    # weighting cannot express itself and so cannot be tested.
    for stream in ("hl.trades", "hl.l2book", "hl.assetctxs"):
        for i in range(8):
            with WormWriter(raw, stream) as w:
                for j in range(120):
                    w.write({"i": j, "pad": "%064x" % noise.getrandbits(256)},
                            t_event=None, t_ingest=T0 + (i * 3600 + j) * NS)
    ledger = Ledger(data / "ledger.jsonl")
    ledger.append("observation", {"name": "seed"})
    ledger.append("gate", {"gate": "feed", "passed": True, "detail": {}})
    return data


# --- the policy gate --------------------------------------------------------

def test_it_refuses_until_retention_is_on_the_ledger(tmp_path, capsys, monkeypatch):
    """Retention and spot-check depth are claims about how the record is kept.
    A number chosen in the source would look measured without ever having been
    decided."""
    ledger = tmp_path / "ledger.jsonl"
    assert backup.preregistered_policy(ledger) is None

    monkeypatch.setattr(backup, "LEDGER", ledger)
    assert backup.main(["stage"]) == 2
    assert "refusing" in capsys.readouterr().err


def test_the_newest_preregistration_wins(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(json.dumps({"seq": i, "kind": "preregistration", "payload": p})
                                for i, p in enumerate([
        {"name": "backup_v1", "keep_daily": 30, "spot_check_n": 5},
        {"name": "other", "keep_daily": 1, "spot_check_n": 1},
        {"name": "backup_v1", "keep_daily": 90, "spot_check_n": 20},
    ])) + "\n")
    assert backup.preregistered_policy(ledger) == {"keep_daily": 90, "spot_check_n": 20}


def test_a_partial_policy_is_not_a_policy(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps({"seq": 0, "kind": "preregistration",
                                  "payload": {"name": "backup_v1", "keep_daily": 90}}) + "\n")
    assert backup.preregistered_policy(ledger) is None


# --- staging: the headline hazard -------------------------------------------

def test_a_raw_copy_of_the_wal_registry_loses_the_schema_and_staging_does_not(tmp_path):
    """`cp` of a live WAL database does not give a stale database -- it gives
    one with NO positions table, because the schema is still in the -wal. A
    restore from that reports zero positions as though that were the answer,
    which is exactly the lie `io/snapshot.py` exists to prevent."""
    data = tmp_path / "data"
    data.mkdir()
    live = data / "registry.sqlite"
    conn = sqlite3.connect(live)
    conn.execute("PRAGMA journal_mode=WAL").fetchone()
    conn.execute("CREATE TABLE positions (address TEXT, coin TEXT)")
    conn.executemany("INSERT INTO positions VALUES (?, ?)",
                     [(f"0x{i}", "BTC") for i in range(2000)])
    conn.commit()                                        # committed, still open

    shutil.copy(live, tmp_path / "naive.sqlite")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        sqlite3.connect(tmp_path / "naive.sqlite").execute("SELECT COUNT(*) FROM positions")

    staged = backup.stage(data=data, into=tmp_path / "staged")
    assert "registry.sqlite" in staged
    rows = sqlite3.connect(tmp_path / "staged" / "registry.sqlite").execute(
        "SELECT COUNT(*) FROM positions").fetchone()[0]
    assert rows == 2000
    conn.close()


def test_staging_records_a_checksum_so_the_copy_can_be_checked(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    sqlite3.connect(data / "registry.sqlite").execute("CREATE TABLE t (a)")
    staged = backup.stage(data=data, into=tmp_path / "staged")
    info = staged["registry.sqlite"]
    assert info["sha256"] == backup.sha256_of(tmp_path / "staged" / "registry.sqlite")
    assert info["bytes"] > 0


# --- the sample -------------------------------------------------------------

def test_every_stream_is_represented_however_sparse(tmp_path):
    """The websocket streams are 4.7% of parts and 90.3% of the bytes, so a
    uniform draw of twenty misses at least one of them 98.3% of the time --
    and those are the streams the tape is actually made of."""
    entries = backup.read_manifest(_store(tmp_path) / "raw")
    streams = {e["stream"] for e in entries}
    assert len(streams) == 4

    for seed in range(12):                               # not a lucky draw
        sample = backup.stratified(entries, 20, random.Random(seed))
        drawn = {e["stream"] for e in sample}
        assert drawn == streams, f"seed {seed} missed {streams - drawn}"
        assert len(sample) == 20


def test_the_heavy_streams_get_more_than_the_floor(tmp_path):
    entries = backup.read_manifest(_store(tmp_path) / "raw")
    counts: dict[str, int] = {}
    for e in backup.stratified(entries, 20, random.Random(0)):
        counts[e["stream"]] = counts.get(e["stream"], 0) + 1
    assert all(n >= backup.FLOOR_PER_STREAM for n in counts.values())
    assert max(counts, key=counts.get).startswith("hl."), counts


def test_a_uniform_draw_would_not_do_this():
    """The comparison that justifies the stratifier existing at all.

    Built from the live store's real proportions rather than a small fixture:
    22,736 parts of which the three websocket streams are 4.7% by count and
    90.3% by bytes. A fixture small enough to write to disk cannot reproduce
    that ratio -- at 37% websocket parts a uniform draw almost never misses,
    which is exactly why the earlier version of this test failed for a reason
    that was not a defect.
    """
    entries = (
        [{"stream": "nat2.liqmap", "bytes": 13, "path": f"a{i}"} for i in range(12048)]
        + [{"stream": "nat2.liqmap2", "bytes": 30, "path": f"b{i}"} for i in range(9608)]
        + [{"stream": "hl.assetctxs", "bytes": 2_100_000, "path": f"c{i}"} for i in range(389)]
        + [{"stream": "hl.trades", "bytes": 4_800_000, "path": f"d{i}"} for i in range(387)]
        + [{"stream": "hl.l2book", "bytes": 4_900_000, "path": f"e{i}"} for i in range(304)]
    )
    every = {e["stream"] for e in entries}

    uniform_misses = sum(
        {e["stream"] for e in random.Random(seed).sample(entries, 20)} != every
        for seed in range(200))
    assert uniform_misses > 180, f"uniform missed only {uniform_misses}/200"

    for seed in range(50):
        drawn = backup.stratified(entries, 20, random.Random(seed))
        assert {e["stream"] for e in drawn} == every, f"stratified missed at seed {seed}"
        assert len(drawn) == 20


# --- the restore-verify -----------------------------------------------------

def test_a_real_copy_round_trips(tmp_path):
    data = _store(tmp_path)
    restored = tmp_path / "restored"
    shutil.copytree(data, restored)                      # a real restore, by copy

    result = backup.restore_verify(restored, n=12, seed=3)
    assert result["ok"] and result["mismatched"] == [] and result["missing"] == []
    assert result["matched"] == result["sampled"] == 12
    assert result["chain_ok"] and result["chain"] == "chain intact"


def test_a_corrupted_part_is_caught(tmp_path):
    data = _store(tmp_path)
    restored = tmp_path / "restored"
    shutil.copytree(data, restored)
    entries = backup.read_manifest(restored / "raw")
    victim = restored / "raw" / entries[0]["path"]
    victim.write_bytes(victim.read_bytes() + b"tampered")

    result = backup.restore_verify(restored, n=len(entries), seed=0)
    assert not result["ok"] and entries[0]["path"] in result["mismatched"]


def test_a_missing_part_is_caught(tmp_path):
    data = _store(tmp_path)
    restored = tmp_path / "restored"
    shutil.copytree(data, restored)
    entries = backup.read_manifest(restored / "raw")
    (restored / "raw" / entries[-1]["path"]).unlink()

    result = backup.restore_verify(restored, n=len(entries), seed=0)
    assert not result["ok"] and entries[-1]["path"] in result["missing"]


def test_an_edited_ledger_fails_the_chain(tmp_path):
    """A restore that returns the tape but a doctored ledger is the worst
    outcome available: the evidence is intact and the record of what was
    concluded from it is not."""
    data = _store(tmp_path)
    restored = tmp_path / "restored"
    shutil.copytree(data, restored)
    path = restored / "ledger.jsonl"
    lines = path.read_text().splitlines()
    first = json.loads(lines[0])
    first["payload"]["name"] = "edited"
    path.write_text("\n".join([json.dumps(first, separators=(",", ":")), *lines[1:]]) + "\n")

    result = backup.restore_verify(restored, n=4, seed=0)
    assert not result["ok"] and not result["chain_ok"]
    assert "entry 0" in result["chain"]


def test_the_chain_check_matches_the_real_one(tmp_path):
    """`verify_chain` is a stdlib copy of ledger/chain.py, so it must agree
    with it -- a copy that drifts is worse than no copy."""
    data = _store(tmp_path)
    mine = backup.verify_chain(data / "ledger.jsonl")
    theirs = Ledger(data / "ledger.jsonl").verify()
    assert mine == theirs


def test_an_empty_sample_is_not_a_pass(tmp_path):
    # Nothing checked must never report ok, or a backup of nothing verifies.
    empty = tmp_path / "empty"
    (empty / "raw").mkdir(parents=True)
    assert backup.restore_verify(empty, n=20, seed=0)["ok"] is False


# --- snapshot and state -----------------------------------------------------

def test_snapshot_refuses_rather_than_pretending_when_restic_is_absent(monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda _: None)
    result = backup.snapshot(keep_daily=90)
    assert result["ok"] is False and "restic" in result["reason"]


def test_the_state_file_carries_an_age(tmp_path):
    state = tmp_path / "backup_state.json"
    backup.write_state({"ok": True, "matched": 20}, path=state)
    blob = json.loads(state.read_text())
    assert blob["ok"] is True and blob["matched"] == 20 and blob["t_ingest"] > 0
