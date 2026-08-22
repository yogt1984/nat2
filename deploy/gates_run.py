"""nat2 gates runner — the daily gate pass, with a flip alert (TASK_2/14, C2).

Runs `gate feed`, `gate map`, `gate magnet` through the repo venv's CLI; each refuses
until it is runnable and every run is a ledger entry and an L2 action (core/guard.record).
Then the ledger is read back: a gate whose verdict differs from its previous entry is a
**flip**, and a flip is the one research event that must reach the operator immediately.
Stdlib-only and read-only itself, like gapwatch; the gates do the writing.

CLI: ``gates_run.py [--gates feed,map,magnet]``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy"))
from gapwatch import notify  # noqa: E402

NAT2 = ROOT / ".venv" / "bin" / "nat2"
LEDGER = ROOT / "data" / "ledger.jsonl"
GATES = ("feed", "map", "magnet")


def verdicts(ledger: Path = LEDGER) -> dict[str, list[str]]:
    """{gate: [verdict, ...]} in ledger order; a refusal is its own verdict, not a FAIL."""
    out: dict[str, list[str]] = {}
    if not ledger.exists():
        return out
    for line in ledger.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("kind") != "gate":
            continue
        p = e.get("payload") or {}
        detail = p.get("detail") or {}
        out.setdefault(p.get("gate", "?"), []).append(
            "pass" if p.get("passed") else str(detail.get("verdict") or "fail") + (f":{detail['reason']}" if detail.get("reason") else ""))
    return out


def flips(before: dict[str, list[str]], after: dict[str, list[str]]) -> list[tuple[str, str, str]]:
    """(gate, previous, current) for every gate whose newest verdict differs from the one before it."""
    out = []
    for gate, seq in after.items():
        if len(seq) > len(before.get(gate, [])) and len(seq) >= 2 and seq[-1] != seq[-2]:
            out.append((gate, seq[-2], seq[-1]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gates", default=",".join(GATES))
    a = ap.parse_args()
    before = verdicts()
    for gate in [g.strip() for g in a.gates.split(",") if g.strip()]:
        result = subprocess.run([str(NAT2), "gate", gate], capture_output=True, text=True, timeout=6 * 3600)
        print(f"gate {gate}: exit {result.returncode}; {result.stdout.strip().splitlines()[-1][:160] if result.stdout.strip() else result.stderr.strip()[-160:]}")
    after = verdicts()
    for gate, prev, cur in flips(before, after):
        notify(f"GATE FLIP {gate}: {prev} -> {cur}", priority="high")
        print(f"flip {gate}: {prev} -> {cur}")


if __name__ == "__main__":
    main()
