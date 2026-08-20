"""Append the 2026-08-13 capture-outage incident to the ledger.

Idempotent: refuses to append a second entry with the same name+from_ts, so a
re-run cannot pollute the chain. Downstream analyses must be able to see the
hole in the point-in-time record; this entry is how.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from nat2.core.clock import now_ns  # noqa: E402
from nat2.ledger.chain import Ledger  # noqa: E402

LEDGER = Path(__file__).resolve().parent.parent / "data" / "ledger.jsonl"
FROM_TS = 1786643042518243494  # ledger seq 109, last pre-outage entry (2026-08-13 19:44 CEST)

ledger = Ledger(LEDGER)
if ledger.latest("incident", name="capture_outage", from_ts=FROM_TS) is not None:
    sys.exit("incident already recorded; nothing to do")
entry = ledger.append(
    "incident",
    {
        "name": "capture_outage",
        "from_ts": FROM_TS,
        "to_ts": now_ns(),
        "cause": "unsupervised process death (pre-systemd); confirmed not deliberate",
        "action": "capture+cycle moved under systemd --user units (deploy/systemd_units.py)",
    },
)
print(f"appended incident as seq {entry.seq}")
