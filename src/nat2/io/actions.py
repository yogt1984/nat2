"""The action log: what the system did, at four levels that are never blurred.

`data/actions.jsonl` is derived and append-only -- not a ledger entry, so it
carries no hash chain; the ledger stays the record of evidence, and this file is
the record of activity that the daily digest (TASK_2/14) renders. The level is
a field on every record so a reader can never mistake one for another:

    L0  ops          what the system did to itself: restarts, holes, backups
    L1  observation  what it saw: sweeps, scans, map snapshots, roster changes
    L2  research     what it decided about evidence: pre-registrations, gate runs, models
    L3  signal       what a *validated* model would have done -- a shadow book, never an order

L3 is empty until a gate PASS and stays a simulation afterwards (task 15).
"""

from __future__ import annotations

import json
from pathlib import Path

from nat2.core.clock import now_ns
from nat2.core.paths import home

LEVELS = ("L0", "L1", "L2", "L3")
FILENAME = Path("data") / "actions.jsonl"


def path(root: Path | None = None) -> Path:
    return (root or home()) / FILENAME


def append(level: str, kind: str, payload: dict, root: Path | None = None, t_ingest: int | None = None) -> dict:
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}, not {level!r}")
    record = {"t_ingest": t_ingest if t_ingest is not None else now_ns(), "level": level, "kind": kind,
              "payload": payload}
    target = path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a") as fh:
        fh.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
    return record


def read(root: Path | None = None, since_ns: int | None = None, level: str | None = None) -> list[dict]:
    target = path(root)
    if not target.exists():
        return []
    out = []
    for line in target.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if since_ns is not None and record["t_ingest"] < since_ns:
            continue
        if level is not None and record["level"] != level:
            continue
        out.append(record)
    return out
