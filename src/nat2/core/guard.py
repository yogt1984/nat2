"""Gate enforcement.

Gates before models: every command downstream of a gate calls ``require`` and
refuses to run when the gate is missing, stale or FAIL.  The refusal is the
product -- it is the only thing standing between a plausible backtest and a
result you would actually act on.
"""

from __future__ import annotations

from dataclasses import dataclass

from nat2.core.clock import NS, now_ns
from nat2.ledger.chain import Ledger

# A gate verdict describes the data as it was when the gate ran.  Past this
# age the verdict says nothing about the data you are about to use.
DEFAULT_MAX_AGE_NS = 24 * 3600 * NS


class GateRefusal(RuntimeError):
    pass


@dataclass(frozen=True)
class Verdict:
    gate: str
    passed: bool
    detail: dict
    ts: int

    @property
    def age_s(self) -> float:
        return (now_ns() - self.ts) / NS


def record(ledger: Ledger, gate: str, passed: bool, detail: dict) -> Verdict:
    entry = ledger.append("gate", {"gate": gate, "passed": passed, "detail": detail})
    _action(ledger, {"gate": gate, "passed": passed, "verdict": detail.get("verdict", "pass" if passed else "fail"),
                     "reason": detail.get("reason"), "seq": entry.seq})
    return Verdict(gate, passed, detail, entry.ts)


def _action(ledger: Ledger, payload: dict) -> None:
    """L2 action next to the ledger it describes; a scratch ledger logs into its own scratch home."""
    from nat2.io.actions import append
    root = ledger.path.parent.parent if ledger.path.parent.name == "data" else ledger.path.parent
    append("L2", "gate", payload, root=root)


def latest(ledger: Ledger, gate: str) -> Verdict | None:
    entry = ledger.latest("gate", gate=gate)
    if entry is None:
        return None
    p = entry.payload
    return Verdict(gate, bool(p["passed"]), p.get("detail", {}), entry.ts)


def require(ledger: Ledger, gate: str, max_age_ns: int = DEFAULT_MAX_AGE_NS) -> Verdict:
    verdict = latest(ledger, gate)
    if verdict is None:
        raise GateRefusal(f"gate '{gate}' has never run -- run `nat2 gate {gate}` first")
    if not verdict.passed:
        raise GateRefusal(
            f"gate '{gate}' FAILED {verdict.age_s / 3600:.1f}h ago; "
            f"everything downstream of it is void until it passes"
        )
    if now_ns() - verdict.ts > max_age_ns:
        raise GateRefusal(
            f"gate '{gate}' last passed {verdict.age_s / 3600:.1f}h ago, "
            f"older than the {max_age_ns / 3600 / NS:.0f}h freshness limit -- re-run it"
        )
    return verdict
