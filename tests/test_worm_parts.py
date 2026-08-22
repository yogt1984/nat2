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
