"""A closed WORM file is immutable -- including across a same-hour restart."""

from __future__ import annotations

from nat2.core.clock import now_ns, parse_window
from nat2.io.worm import WormWriter, read_manifest, read_records
from nat2.validate.audit_feed import audit


def test_restart_within_the_hour_opens_a_new_part(tmp_path):
    for run in range(3):
        with WormWriter(tmp_path, "hl.trades") as writer:
            writer.write({"run": run}, now_ns())

    entries = read_manifest(tmp_path, "hl.trades")
    assert len(entries) == 3
    # Three distinct files, each manifested exactly once: appending to an
    # already-hashed file would make a restart indistinguishable from tampering.
    assert len({e.path for e in entries}) == 3
    assert [r["payload"]["run"] for r in read_records(tmp_path, "hl.trades")] == [0, 1, 2]
    assert [r["seq"] for r in read_records(tmp_path, "hl.trades")] == [0, 1, 2]

    result = audit(tmp_path, ["hl.trades"], parse_window("1h"))
    assert {c.name for c in result.failures} == set(), [c.detail for c in result.failures]


def test_unterminated_tail_of_an_open_file_is_skipped_not_fatal(tmp_path):
    # The capture daemon writes while readers read. A complete record always
    # ends in a newline, so a half-written one is incomplete, not corrupt --
    # and it will be read whole on the next pass.
    import json
    import zstandard

    from nat2.io.worm import SUFFIX, read_records

    directory = tmp_path / "hl.trades" / "2026-08-08"
    directory.mkdir(parents=True)
    path = directory / f"hl.trades-20260808T00-00{SUFFIX}"
    good = json.dumps({"seq": 0, "stream": "hl.trades", "t_ingest": 1,
                       "t_event": None, "payload": {"ok": True}})
    partial = '{"seq": 1, "stream": "hl.trades", "t_ing'
    with path.open("wb") as fh:
        fh.write(zstandard.ZstdCompressor().compress((good + "\n" + partial).encode()))

    records = list(read_records(tmp_path, "hl.trades"))
    assert [r["seq"] for r in records] == [0]


def test_a_corrupt_terminated_line_still_raises(tmp_path):
    # Mid-file damage is real corruption and must not be silently skipped.
    import json
    import zstandard

    import pytest

    from nat2.io.worm import SUFFIX, read_records

    directory = tmp_path / "hl.trades" / "2026-08-08"
    directory.mkdir(parents=True)
    path = directory / f"hl.trades-20260808T00-00{SUFFIX}"
    body = '{"seq": 0, "t_ingest": 1, "payload": BROKEN}\n' + json.dumps(
        {"seq": 1, "stream": "hl.trades", "t_ingest": 2, "t_event": None, "payload": {}}
    ) + "\n"
    with path.open("wb") as fh:
        fh.write(zstandard.ZstdCompressor().compress(body.encode()))

    with pytest.raises(json.JSONDecodeError):
        list(read_records(tmp_path, "hl.trades"))


def test_an_empty_flush_leaves_the_file_untouched(tmp_path):
    """A flush with nothing to write must not touch the file: the gapwatch heartbeat is the
    mtime, and a 9-byte empty frame every 30 s made a silent websocket look alive."""
    import os
    import time
    from nat2.io.worm import WormWriter
    with WormWriter(tmp_path, "hl.trades") as w:
        w.write({"x": 1}, t_event=None, t_ingest=1)
        w.flush()
        f = next((tmp_path / "hl.trades").glob("*/*.zst"))
        os.utime(f, (1_000_000, 1_000_000))
        size = f.stat().st_size
        w.flush()
        assert f.stat().st_size == size and f.stat().st_mtime == 1_000_000
        w.write({"x": 2}, t_event=None, t_ingest=2)
        w.flush()
        assert f.stat().st_size > size and f.stat().st_mtime > 1_000_000


def test_since_skips_parts_that_ended_before_it_and_yields_the_same_records(tmp_path):
    """TASK_2/14 read-side: `since_ns` must not decompress the whole tape; output is unchanged."""
    from nat2.io.worm import _file_end_ns
    H = 3600 * 1_000_000_000
    t0 = 1_787_320_800_000_000_000                      # 2026-08-21T14:00:00Z, an hour boundary
    with WormWriter(tmp_path, "hl.trades") as w:
        for i, t in enumerate((t0 - H + 5, t0 - 1, t0 + 7, t0 + H + 9)):   # hours 13, 13, 14, 15
            w.write({"i": i}, t_event=None, t_ingest=t)
    parts = sorted((tmp_path / "hl.trades").glob("*/*.zst"))
    assert [p.name.rsplit("-", 2)[-2] for p in parts] == ["20260821T13", "20260821T14", "20260821T15"]
    assert _file_end_ns(parts[0]) == t0 and _file_end_ns(tmp_path / "weird.ndjson.zst") == 1 << 63
    everything = [r["payload"]["i"] for r in read_records(tmp_path, "hl.trades")]
    assert everything == [0, 1, 2, 3]
    assert [r["payload"]["i"] for r in read_records(tmp_path, "hl.trades", since_ns=t0)] == [2, 3]
    assert [r["payload"]["i"] for r in read_records(tmp_path, "hl.trades", since_ns=t0 - 1)] == [1, 2, 3]
    assert [r["payload"]["i"] for r in read_records(tmp_path, "hl.trades", since_ns=t0 + H + 9)] == [3]


def test_resuming_seq_reads_the_manifest_once_and_never_reopens_a_manifested_part(tmp_path, monkeypatch):
    """Every map snapshot opens its own part, so `nat2.liqmap` reached 2,554 of them. The
    membership test was `any(...)` over the manifest per file -- quadratic -- and opening
    one writer came to dominate the cycle pass (3m44s), which is what starved `mapsnap`
    and left the map stale. Pinned as behaviour, not as a timing."""
    import nat2.io.worm as worm

    for i in range(40):                                  # 40 parts in the same hour, all manifested
        with WormWriter(tmp_path, "nat2.liqmap") as w:
            w.write({"i": i}, t_event=None, t_ingest=1_787_000_000_000_000_000 + i)
    assert len(read_manifest(tmp_path, "nat2.liqmap")) == 40

    opened, manifests = [], []
    real_tail, real_manifest = worm._tail_seqs, worm.read_manifest
    monkeypatch.setattr(worm, "_tail_seqs", lambda p: opened.append(p) or real_tail(p))
    monkeypatch.setattr(worm, "read_manifest", lambda *a, **k: manifests.append(a) or real_manifest(*a, **k))

    writer = WormWriter(tmp_path, "nat2.liqmap")
    assert writer.seq == 40 and opened == [] and len(manifests) == 1   # nothing re-decompressed
    # An unmanifested part -- a hard kill between the last write and close -- is still read,
    # because only it can hold seqs the manifest has never seen.
    with WormWriter(tmp_path, "nat2.liqmap") as w:
        w.write({"i": 40}, t_event=None, t_ingest=1_787_000_000_000_000_040)
    manifest = tmp_path / "_manifest.jsonl"
    manifest.write_text("".join(manifest.read_text().splitlines(keepends=True)[:-1]))   # forget the newest
    assert WormWriter(tmp_path, "nat2.liqmap").seq == 41 and len(opened) == 1   # seq 40 recovered from the orphan
