"""Hash-chained append-only ledger.

Gate verdicts, spec freezes and run records land here.  Each entry commits to
the previous one, so removing or editing a past entry breaks every hash after
it and ``nat2 log verify`` says exactly where.  The point is not security
against an attacker -- it is that a failed test you'd rather forget cannot
quietly leave the record.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from nat2.core.clock import now_ns

GENESIS = "0" * 64


@dataclass(frozen=True)
class Entry:
    seq: int
    ts: int
    kind: str
    payload: dict
    prev_hash: str
    hash: str


def _digest(seq: int, ts: int, kind: str, payload: dict, prev_hash: str) -> str:
    body = json.dumps(
        {"seq": seq, "ts": ts, "kind": kind, "payload": payload, "prev_hash": prev_hash},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def entries(self) -> list[Entry]:
        if not self.path.exists():
            return []
        return [
            Entry(**json.loads(line))
            for line in self.path.read_text().splitlines()
            if line.strip()
        ]

    def append(self, kind: str, payload: dict) -> Entry:
        existing = self.entries()
        seq = len(existing)
        prev = existing[-1].hash if existing else GENESIS
        ts = now_ns()
        entry = Entry(seq, ts, kind, payload, prev, _digest(seq, ts, kind, payload, prev))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps(asdict(entry), separators=(",", ":")) + "\n")
        return entry

    def verify(self) -> tuple[bool, str]:
        prev = GENESIS
        for i, entry in enumerate(self.entries()):
            if entry.seq != i:
                return False, f"entry {i}: seq is {entry.seq}"
            if entry.prev_hash != prev:
                return False, f"entry {i}: prev_hash does not match entry {i - 1}"
            expect = _digest(entry.seq, entry.ts, entry.kind, entry.payload, entry.prev_hash)
            if entry.hash != expect:
                return False, f"entry {i}: content does not match its hash (edited)"
            prev = entry.hash
        return True, "chain intact"

    def latest(self, kind: str, **match) -> Entry | None:
        for entry in reversed(self.entries()):
            if entry.kind != kind:
                continue
            if all(entry.payload.get(k) == v for k, v in match.items()):
                return entry
        return None
