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
