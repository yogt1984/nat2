"""gate feed -- is the captured data intact, complete and causal?

The first gate, and the one every other gate depends on.  Its verdict goes to
the hash-chained ledger, so a FAIL cannot be quietly re-run away: the failed
verdict stays on the record next to whatever passed later.
"""

from __future__ import annotations

from pathlib import Path

from nat2.core.guard import Verdict, record
from nat2.ledger.chain import Ledger
from nat2.validate.audit_feed import AuditResult, audit

NAME = "feed"


def run(root: Path, streams: list[str], window_ns: int, ledger: Ledger) -> tuple[Verdict, AuditResult]:
    result = audit(root, streams, window_ns)
    detail = result.summary()
    detail["streams"] = streams
    verdict = record(ledger, NAME, result.passed, detail)
    return verdict, result
